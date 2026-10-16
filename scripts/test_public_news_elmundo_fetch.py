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
    return m.collect_elmundo_source(SOURCE,POLICY,documents[m.LISTING],now,COUNTRIES,previous or {},records or {},f,fixtures=True,**kw),f


class ElMundoFetchTests(unittest.TestCase):
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
