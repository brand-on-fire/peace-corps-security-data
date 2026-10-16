"""Offline HTML collection transaction tests; no provider requests."""
import copy
import datetime as dt
import html
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import public_news_elmundo_fetch as m
from test_public_news_elmundo import SOURCE,COUNTRIES,NOW,article,collect,URL

POLICY=dict(sourceId=m.SOURCE_ID,adapter='sv-elmundo',mode='official',cadenceMinutes=30,url=m.LISTING)


def listing(urls,when='jueves 8, octubre 2026 - 10:00 a. m.'):
    return ('<main>'+''.join(f'<article><div class="article-title"><a href="{u}">Reporte local</a></div><div class="article-date">{when}</div></article>' for u in urls)+'</main>').encode()


class Fetch:
    def __init__(self,documents):self.documents=documents;self.calls=[]
    def __call__(self,policy,*args):
        url=policy['url'];self.calls.append(url)
        value=self.documents.get(url)
        return dict(httpStatus=200 if value is not None else 503),value


def run(documents,previous=None,records=None,now=NOW,**kw):
    f=Fetch(documents)
    return m.collect_elmundo_source(SOURCE,kw.pop('policy',POLICY),documents[m.LISTING],now,COUNTRIES,previous or {},records or {},f,fixtures=True,**kw),f


def archive_checkpoint(anchor=URL,page=3):
    return dict(version=1,startedAt=NOW,**{'from':'2025-10-16T01:06:00Z','to':NOW},status='scanning',
        anchor=dict(url=anchor,publishedAt='2026-10-08T16:00:00Z'),page=page,pending=[],pages=[],
        checkedArticles=0,gaps=[],reachedLowerBound=False,listingScanComplete=False,allYearBackfill=False)


class ElMundoFetchTests(unittest.TestCase):
    def test_archive_opt_in_and_fixed_lower_bound_survive_later_runs(self):
        docs={m.LISTING:listing([URL]),URL:article()['raw']}
        normal,_=run(docs);self.assertNotIn('elmundoArchive',normal['state'])
        first,_=run(docs,policy=dict(POLICY,archiveBackfillDays=365))
        archive=first['state']['elmundoArchive']
        self.assertEqual(archive['from'],'2025-10-16T01:06:00Z');self.assertFalse(archive['listingScanComplete'])
        second,_=run(docs,first['state'],{r['id']:r for r in first['incidents']},now='2026-10-17T01:06:00Z',policy=dict(POLICY,archiveBackfillDays=365))
        later=second['state']['elmundoArchive']
        self.assertEqual(later['from'],archive['from']);self.assertEqual(later['to'],NOW)
        self.assertEqual(later['status'],'blocked')
        self.assertEqual(later['gaps'][-1]['reason'],'publisher_listing_ended_before_requested_year')
        for value in [True,30,366,'365']:
            with self.subTest(value=value),self.assertRaises(ValueError):run(docs,policy=dict(POLICY,archiveBackfillDays=value))

    def test_archive_url_anchor_budget_fairness_and_six_corrections(self):
        live=[URL+f'-live-{i}' for i in range(12)];old=[URL+f'-archive-{i}' for i in range(12)]
        anchor_rows=[URL+f'-anchor-{i}' for i in range(11)]+[URL]
        docs={m.LISTING:listing(live),m.endpoint(3):listing(anchor_rows),m.endpoint(4):listing(old,when='miércoles 7, octubre 2026 - 10:00 a. m.')}
        before={}
        for i in range(6):
            url=URL+f'-correction-{i}';row=collect([article(url=url,status=str(2210000000000000000+i))])['incidents'][0]
            before[row['id']]=row;docs[url]=article(url=url,status=str(2210000000000000000+i))['raw']
        for i,url in enumerate(live+old):docs[url]=article(url=url,status=str(2310000000000000000+i))['raw']
        previous=dict(elmundoRecentUrls=live,elmundoArchive=archive_checkpoint())
        result,f=run(docs,previous,before,policy=dict(POLICY,archiveBackfillDays=365))
        self.assertEqual(len(result['evidence']),19);self.assertEqual(len(f.calls),18)
        self.assertEqual(sum(u in live for u in f.calls),8);self.assertEqual(sum(u in old for u in f.calls),2)
        self.assertEqual(sum('correction-' in u for u in f.calls),6)
        archive=result['state']['elmundoArchive'];self.assertEqual(archive['page'],4)
        self.assertEqual(len(archive['pending']),10);self.assertFalse(archive['listingScanComplete'])
        self.assertEqual(archive['pages'][0]['after']['url'],URL)
        self.assertEqual(archive['pages'][0]['sha256'],m.hashlib.sha256(docs[m.endpoint(4)]).hexdigest())
        self.assertEqual(previous['elmundoArchive']['pages'],[])

    def test_archive_missing_or_changed_anchor_holds_progress_but_live_flows(self):
        fresh=URL+'-fresh';other=URL+'-replacement'
        for anchor_page,reason in [(listing([other]),'archive_anchor_missing_or_ambiguous'),
             (listing([URL],when='viernes 9, octubre 2026 - 10:00 a. m.'),'archive_anchor_publication_changed')]:
            docs={m.LISTING:listing([fresh]),fresh:article(url=fresh)['raw'],m.endpoint(3):anchor_page}
            previous=dict(elmundoRecentUrls=[fresh],elmundoArchive=archive_checkpoint())
            result,_=run(docs,previous,policy=dict(POLICY,archiveBackfillDays=365))
            self.assertEqual(len(result['incidents']),1)
            self.assertEqual(result['state']['elmundoArchive']['status'],'blocked')
            self.assertEqual(result['state']['elmundoArchive']['gaps'][-1]['reason'],reason)
            self.assertEqual(result['state']['elmundoArchive']['anchor'],previous['elmundoArchive']['anchor'])
            self.assertFalse(result['state']['elmundoArchive']['listingScanComplete'])
            self.assertEqual(result['sourceStatus'],'healthy')

    def test_archive_offset_drift_is_recorded_and_exact_anchor_can_recover(self):
        fresh=URL+'-fresh';old=URL+'-archive'
        rows=[URL+f'-shift-{i}' for i in range(12)]
        docs={m.LISTING:listing([fresh]),fresh:article(url=fresh)['raw'],
            m.endpoint(3):listing(rows,when='viernes 9, octubre 2026 - 10:00 a. m.'),
            m.endpoint(4):listing([URL,old]),old:article(url=old,status='2110000000000000002')['raw']}
        result,f=run(docs,dict(elmundoRecentUrls=[fresh],elmundoArchive=archive_checkpoint()),policy=dict(POLICY,archiveBackfillDays=365))
        archive=result['state']['elmundoArchive']
        self.assertEqual(archive['anchor']['url'],old);self.assertEqual(archive['page'],4)
        self.assertEqual(archive['gaps'][0]['reason'],'archive_anchor_offset_changed')
        self.assertEqual(archive['gaps'][0]['resolvedAt'],NOW)
        self.assertEqual(archive['pending'],[])
        self.assertEqual(f.calls[:2],[m.endpoint(3),m.endpoint(4)])

    def test_live_listing_gap_uses_all_slots_before_archive_and_history_queue_does_not_reset(self):
        urls=[URL+f'-new-{i}' for i in range(36)]
        docs={m.endpoint(p):listing(urls[(p-1)*12:p*12]) for p in range(1,4)}
        docs.update({u:article(url=u,status=str(2410000000000000000+i))['raw'] for i,u in enumerate(urls)})
        previous=dict(elmundoRecentUrls=[URL+'-absent'],elmundoArchive=archive_checkpoint(page=400))
        result,f=run(docs,previous,policy=dict(POLICY,archiveBackfillDays=365))
        self.assertNotIn(m.endpoint(400),f.calls)
        self.assertEqual(result['state']['elmundoArchive'],previous['elmundoArchive'])
        self.assertEqual(len(result['state']['elmundoPending']),26)
        self.assertEqual(len(result['state']['elmundoCoverageGaps']),1)

    def test_archive_complete_observed_chain_never_claims_all_crime_coverage(self):
        fresh=URL+'-fresh';older=URL+'-older'
        initial=archive_checkpoint();initial['anchor']['publishedAt']='2025-10-17T16:00:00Z'
        rows=[URL+f'-a{i}' for i in range(11)]+[URL]
        docs={m.LISTING:listing([fresh]),fresh:article(url=fresh)['raw'],
            m.endpoint(3):listing(rows,when='viernes 17, octubre 2025 - 10:00 a. m.'),
            m.endpoint(4):listing([older],when='miércoles 15, octubre 2025 - 10:00 a. m.')}
        result,f=run(docs,dict(elmundoRecentUrls=[fresh],elmundoArchive=initial),policy=dict(POLICY,archiveBackfillDays=365))
        archive=result['state']['elmundoArchive']
        self.assertTrue(archive['listingScanComplete']);self.assertTrue(archive['reachedLowerBound'])
        self.assertFalse(archive['allYearBackfill']);self.assertFalse(result['state']['elmundoWindow']['allYearBackfill'])
        self.assertNotIn(older,f.calls);self.assertEqual(archive['pending'],[])

    def test_archive_detail_failure_is_explicit_and_does_not_block_next_live_work(self):
        fresh=URL+'-fresh';failed=URL+'-failed'
        initial=archive_checkpoint();initial['reachedLowerBound']=True
        initial['pending']=[dict(url=failed,title='Older report',publishedAt='2026-01-01T12:00:00Z')]
        docs={m.LISTING:listing([fresh]),fresh:article(url=fresh)['raw'],failed:None}
        result,_=run(docs,dict(elmundoRecentUrls=[fresh],elmundoArchive=initial),policy=dict(POLICY,archiveBackfillDays=365))
        self.assertEqual(len(result['incidents']),1);self.assertIn(failed,result['state']['elmundoQuarantine'])
        self.assertFalse(result['state']['elmundoArchive']['listingScanComplete'])
        self.assertEqual(result['state']['elmundoArchive']['pending'],initial['pending'])
        self.assertEqual(result['sourceStatus'],'unavailable')

    def test_single_archive_article_returns_unused_slot_to_live_queue(self):
        live=[URL+f'-fresh-{i}' for i in range(12)];old=URL+'-old'
        initial=archive_checkpoint();initial['reachedLowerBound']=True
        initial['pending']=[dict(url=old,title='Older report',publishedAt='2026-01-01T12:00:00Z')]
        docs={m.LISTING:listing(live)}
        docs.update({u:article(url=u,status=str(2510000000000000000+i))['raw'] for i,u in enumerate(live+[old])})
        result,f=run(docs,dict(elmundoRecentUrls=live,elmundoArchive=initial),policy=dict(POLICY,archiveBackfillDays=365))
        self.assertEqual(sum(u in live for u in f.calls),9);self.assertIn(old,f.calls)
        self.assertEqual(len(result['discoveries']),10);self.assertEqual(len(result['state']['elmundoPending']),3)
        self.assertFalse(result['state']['elmundoArchive']['listingScanComplete'])

    def test_historical_page_failure_retains_cursor_and_live_progress_and_error_on_noop(self):
        fresh=URL+'-fresh';docs={m.LISTING:listing([fresh]),fresh:article(url=fresh)['raw'],m.endpoint(3):b'<html>Unavailable</html>'}
        previous=dict(elmundoRecentUrls=[fresh],elmundoArchive=archive_checkpoint());copy_before=copy.deepcopy(previous)
        result,f=run(docs,previous,policy=dict(POLICY,archiveBackfillDays=365))
        archive=result['state']['elmundoArchive']
        self.assertEqual(len(result['incidents']),1);self.assertEqual(result['sourceStatus'],'healthy')
        self.assertEqual(archive['anchor'],previous['elmundoArchive']['anchor'])
        self.assertEqual(archive['page'],previous['elmundoArchive']['page'])
        self.assertEqual(archive['pages'],previous['elmundoArchive']['pages'])
        self.assertEqual(archive['status'],'retrying-page');self.assertFalse(archive['listingScanComplete'])
        self.assertEqual(archive['pageFailures'][0]['response']['sha256'],m.hashlib.sha256(docs[m.endpoint(3)]).hexdigest())
        repeat,f=run(docs,result['state'],{r['id']:r for r in result['incidents']},now='2026-10-16T01:36:00Z',policy=dict(POLICY,archiveBackfillDays=365))
        self.assertEqual(f.calls,[]);self.assertEqual(repeat['state']['elmundoArchive'],archive)
        self.assertEqual(previous,copy_before)
        docs[m.endpoint(3)]=None
        retry,f=run(docs,result['state'],{r['id']:r for r in result['incidents']},now='2026-10-16T02:06:00Z',policy=dict(POLICY,archiveBackfillDays=365))
        self.assertEqual(f.calls,[m.endpoint(3)]);self.assertEqual(len(retry['state']['elmundoArchive']['pageFailures']),2)
        with patch.object(m,'MAX_BATCH_BYTES',len(docs[m.LISTING])-1),self.assertRaises(ValueError):run(docs,previous,policy=dict(POLICY,archiveBackfillDays=365))
        requests=m.Requests(POLICY,Fetch({}),NOW,True)
        for url in [m.endpoint(2001),m.LISTING+'?page=0',m.LISTING+'?page=3&limit=2000']:
            with self.subTest(url=url),self.assertRaises(ValueError):requests.get(url)

    def test_bounded_listing_and_new_article_backlog_drains_without_coverage_claim(self):
        urls=[URL+f'-{i}' for i in range(15)]
        docs={m.LISTING:listing(urls[:12]),m.endpoint(2):listing(urls[12:])}
        docs.update({u:article(url=u,status=str(2110000000000000000+i))['raw'] for i,u in enumerate(urls)})
        first,f=run(docs)
        self.assertEqual(len(first['incidents']),10);self.assertEqual(len(first['state']['elmundoPending']),5)
        self.assertEqual(len(f.calls),11);self.assertFalse(first['state']['elmundoWindow']['allYearBackfill'])
        previous=dict(first['state'],lastSuccessAt=NOW);known={r['id']:r for r in first['incidents']}
        second,f=run(docs,previous,known,now='2026-10-16T01:36:00Z')
        self.assertEqual(len(second['incidents']),5);self.assertEqual(second['state']['elmundoPending'],[])
        self.assertTrue(second['state']['elmundoWindow']['priorListingOverlap']);self.assertEqual(len(f.calls),5)
        self.assertEqual(first['state']['elmundoPending'],previous['elmundoPending'])

    def test_old_accepted_article_rechecked_even_after_leaving_listing(self):
        before=collect()['incidents'][0];future=URL+'-new'
        docs={m.LISTING:listing([future]),future:article(url=future,status='2110000000000000002')['raw'],URL:article(extra='No ocurrió este hecho.')['raw']}
        first,f=run(docs,records={before['id']:before})
        changed=next(r for r in first['incidents'] if r['id']==before['id'])
        self.assertEqual(changed['state'],'expired');self.assertEqual(changed['evidenceStatus'],'unverified')
        self.assertEqual(len(changed['reports']),2);self.assertIn(URL,f.calls)
        self.assertEqual(changed['updatedAt'],NOW)

    def test_failed_complete_detail_quarantines_without_mutating_prior_state_or_records(self):
        before=collect()['incidents'][0];previous={'lastSuccessAt':'2026-10-15T00:00:00Z','elmundoRecentUrls':[URL]}
        state_copy=copy.deepcopy(previous);known={before['id']:before};known_copy=copy.deepcopy(known)
        docs={m.LISTING:listing([URL]),URL:b'<html>Article currently unavailable</html>'}
        result,_=run(docs,previous,known)
        self.assertEqual(result['incidents'],[]);self.assertEqual(result['sourceStatus'],'unavailable')
        self.assertIn(URL,result['state']['elmundoQuarantine'])
        self.assertEqual(previous,state_copy);self.assertEqual(known,known_copy)
        docs[URL]=None
        result,_=run(docs,previous,known)
        self.assertIn('HTTP 503',result['state']['elmundoQuarantine'][URL]['reason'])
        self.assertEqual(previous,state_copy)

    def test_failed_old_detail_does_not_block_new_crime_and_retry_backoff_is_explicit(self):
        before=collect()['incidents'][0];new_url=URL+'-new'
        docs={m.LISTING:listing([new_url]),new_url:article(url=new_url,status='2110000000000000002')['raw'],URL:None}
        known={before['id']:before};prior={'lastSuccessAt':'2026-10-15T00:00:00Z'}
        first,f=run(docs,prior,known)
        self.assertEqual(len(first['incidents']),1);self.assertNotEqual(first['incidents'][0]['id'],before['id'])
        self.assertEqual(first['sourceStatus'],'unavailable');self.assertEqual(known[before['id']],before)
        self.assertEqual(first['state']['elmundoQuarantine'][URL]['nextRetryAt'],'2026-10-16T02:06:00Z')
        state=dict(prior,**first['state']);known.update({r['id']:r for r in first['incidents']})
        early,f=run(docs,state,known,now='2026-10-16T01:36:00Z')
        self.assertEqual(f.calls,[]);self.assertEqual(early['sourceStatus'],'unavailable')
        retry,f=run(docs,state,known,now='2026-10-16T02:06:00Z')
        self.assertEqual(f.calls,[URL]);self.assertEqual(retry['state']['elmundoQuarantine'][URL]['attempts'],2)
        self.assertEqual(retry['state']['elmundoQuarantine'][URL]['nextRetryAt'],'2026-10-16T04:06:00Z')
        docs[URL]=article()['raw']
        repaired,f=run(docs,dict(prior,**retry['state']),known,now='2026-10-16T04:06:00Z')
        self.assertEqual(f.calls,[URL]);self.assertEqual(repaired['sourceStatus'],'healthy')
        self.assertEqual(repaired['state']['elmundoQuarantine'],{})
        self.assertEqual(repaired['incidents'],[])

    def test_new_failed_detail_stays_queued_but_other_new_details_progress(self):
        good=URL+'-good'
        docs={m.LISTING:listing([URL,good]),URL:b'<html>Malformed detail</html>',good:article(url=good)['raw']}
        result,_=run(docs)
        self.assertEqual(len(result['incidents']),1);self.assertEqual(result['sourceStatus'],'unavailable')
        self.assertEqual([r['url'] for r in result['state']['elmundoPending']],[URL])
        self.assertNotIn(URL,result['state']['elmundoChecked']);self.assertIn(good,result['state']['elmundoChecked'])

    def test_oversized_individual_detail_is_quarantined_while_other_articles_progress(self):
        before=collect()['incidents'][0];good=URL+'-good'
        docs={m.LISTING:listing([good]),URL:b'x'*(m.MAX_BYTES+1),good:article(url=good,status='2110000000000000002')['raw']}
        result,_=run(docs,records={before['id']:before})
        self.assertEqual(len(result['incidents']),1);self.assertNotEqual(result['incidents'][0]['id'],before['id'])
        self.assertEqual(result['sourceStatus'],'unavailable')
        self.assertIn('individual detail exceeds 2MB',result['state']['elmundoQuarantine'][URL]['reason'])
        self.assertEqual(result['state']['elmundoQuarantine'][URL]['sourceResponse']['bytes'],m.MAX_BYTES+1)
        with self.assertRaises(ValueError):run({m.LISTING:b'x'*(m.MAX_BYTES+1)})

    def test_bounded_no_overlap_gap_is_preserved_on_later_success(self):
        urls=[URL+f'-{i}' for i in range(36)];docs={m.endpoint(p):listing(urls[(p-1)*12:p*12]) for p in range(1,4)}
        docs.update({u:article(url=u,status=str(2110000000000000000+i))['raw'] for i,u in enumerate(urls)})
        previous={'elmundoRecentUrls':[URL+'-older'],'lastSuccessAt':'2026-10-01T00:00:00Z'}
        first,_=run(docs,previous)
        gap=first['state']['elmundoCoverageGaps'];self.assertEqual(len(gap),1);self.assertEqual(gap[0]['from'],previous['lastSuccessAt'])
        second,_=run(docs,dict(first['state'],lastSuccessAt=NOW),{r['id']:r for r in first['incidents']},now='2026-10-16T01:36:00Z')
        self.assertEqual(second['state']['elmundoCoverageGaps'],gap)

    def test_malformed_or_shifted_listing_fails_before_details(self):
        for raw in [b'<html>Soft404</html>',listing([URL],when='viernes 8, octubre 2026 - 10:00 a. m.'),listing([URL,URL])]:
            with self.subTest(raw=raw),self.assertRaises(ValueError):run({m.LISTING:raw,URL:article()['raw']})
        urls=[URL+f'-{i}' for i in range(12)]
        with self.assertRaisesRegex(ValueError,'shifted/duplicated'):
            run({m.LISTING:listing(urls),m.endpoint(2):listing([urls[-1]])})

    def test_complete_byte_ceiling_and_conditional_listing_removal(self):
        with patch.object(m,'MAX_BATCH_BYTES',200),self.assertRaises(ValueError):run({m.LISTING:listing([URL]),URL:article()['raw']})
        previous={'etag':'a','lastModified':'b','lastSuccessAt':NOW}
        self.assertEqual(m.request_state(POLICY,previous),{'lastSuccessAt':NOW});self.assertIn('etag',previous)

    def test_inactive_country_and_manual_review_never_admit(self):
        f=Fetch({URL:article()['raw']})
        with self.assertRaisesRegex(ValueError,'active-country'):
            m.collect_elmundo_source(SOURCE,POLICY,listing([URL]),NOW,{'SV':dict(COUNTRIES['SV'],status='suspended')},{},{},f)
        self.assertEqual(f.calls,[])
        result,_=run({m.LISTING:listing([URL]),URL:article()['raw']},reviews=[dict(sourceId=m.SOURCE_ID,url=URL)])
        self.assertEqual(result['incidents'],[])

    def test_recent_held_article_is_rechecked_after_six_hours(self):
        old=article(quote='La Policía informó de una detención.')
        first,_=run({m.LISTING:listing([URL]),URL:old['raw']})
        self.assertEqual(first['incidents'],[])
        early,f=run({m.LISTING:listing([URL]),URL:article()['raw']},dict(first['state'],lastSuccessAt=NOW),now='2026-10-16T01:36:00Z')
        self.assertEqual(f.calls,[])
        later,f=run({m.LISTING:listing([URL]),URL:article()['raw']},dict(first['state'],lastSuccessAt=NOW),now='2026-10-16T07:36:00Z')
        self.assertEqual(len(later['incidents']),1);self.assertEqual(f.calls,[URL])

    def test_history_bound_and_unqualified_discovery_do_not_claim_an_admitted_local_crime(self):
        docs={m.LISTING:listing([URL]),URL:article()['raw']}
        result,_=run(docs,history_from='2026-10-15')
        self.assertEqual(result['incidents'],[])
        discovery=result['discoveries'][0]
        self.assertEqual(discovery['note'],'outside-configured-history-window')
        self.assertEqual(discovery['status'],'needs-review');self.assertNotIn('incidentId',discovery)
        self.assertEqual(discovery['categoryCandidates'],[]);self.assertEqual(discovery['mentionedCountryCodes'],[])
        docs[URL]=article(quote='La Policía informó de una actividad comunitaria.')['raw']
        result,_=run(docs)
        self.assertEqual(result['discoveries'][0]['locationStatus'],'unverified')
        self.assertEqual(result['discoveries'][0]['categoryCandidates'],[])

    def test_actual_saved_complete_listings_and_crime_sources_when_local_evidence_exists(self):
        root=Path(__file__).resolve().parents[1]/'artifacts/mvp-live-20261015/spanish-crime'
        if not (root/'accepted-records.json').exists():self.skipTest('Local complete copyrighted source evidence is not bundled')
        for n in range(1,13):
            path=root/('mundo-national'+(f'-{n}' if n>1 else '')+'.response')
            self.assertEqual(len(m.parse_listing(path.read_bytes(),NOW)),12)
        corpus=json.loads((root/'selected-corpus.json').read_text())
        docs=[dict(url=r['url'],raw=(root/(r['capture']+'.response')).read_bytes()) for r in corpus]
        result=m.collect_elmundo(SOURCE,docs,NOW,COUNTRIES)
        self.assertEqual(len(result['incidents']),7)
        self.assertEqual(len(result['decisions']),31)


if __name__=='__main__':unittest.main()
