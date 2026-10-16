"""Complete-response offline lifecycle tests. No provider requests."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import public_news_caribe_fetch as m
from test_public_news_caribe import post, SOURCE as PARSER_SOURCE, COUNTRIES, NOW, run as parse

SOURCE=dict(PARSER_SOURCE,enabled=True,kind='rss',access='public-feed',feedUrl=m.LATEST_URL)
POLICY=dict(sourceId=m.SOURCE_ID,adapter='cr-caribe',mode='official',cadenceMinutes=30,url=m.LATEST_URL)


def raw(value):return json.dumps(value,ensure_ascii=False).encode()


class Fetch:
    def __init__(self,documents):self.documents=documents;self.calls=[]
    def __call__(self,policy,previous,now,fixtures):
        self.calls.append((copy.deepcopy(policy),copy.deepcopy(previous)))
        value=self.documents.get(policy['url'])
        if isinstance(value,tuple):return value
        return dict(httpStatus=200 if value else 503,finalUrl=policy['url']),value


def run(rows=(),records=None,previous=None,documents=None,now=NOW,**kw):
    fetcher=Fetch(documents or {})
    result=m.collect_caribe_source(kw.pop('source',SOURCE),kw.pop('policy',POLICY),raw(rows),now,
        kw.pop('countries',COUNTRIES),previous or {},records or {},fetcher,**kw)
    return result,fetcher


def known(count=1):
    records={};documents={}
    for index in range(count):
        item=post(81001+index);row=parse([item])['incidents'][0]
        records[row['id']]=row;documents[m.endpoint(str(item['id']))]=raw(item)
    return records,documents


def merge(records,result):return dict(records,**{r['id']:r for r in result['incidents']})


class CaribeFetchTests(unittest.TestCase):
    def test_complete_latest_response_publishes_facts_without_child_requests(self):
        result,f=run([post()]);self.assertFalse(f.calls)
        self.assertEqual(len(result['incidents']),1);self.assertEqual(result['sourceStatus'],'healthy')
        self.assertEqual(result['incidents'][0]['evidenceStatus'],'reported')
        self.assertEqual(result['incidents'][0]['reports'][0]['language'],'es')
        self.assertEqual(result['state']['caribeListingContentHash'],hashlib.sha256(raw([post()])).hexdigest())
        self.assertEqual(result['state']['caribeValidatedRepresentations'],1)
        self.assertFalse(result['state']['caribeAllYearBackfill'])

    def test_unadmitted_discovery_is_not_labeled_as_an_established_crime_category(self):
        article=post(title='Pronóstico del tiempo',body='<p>El tiempo será variable esta semana.</p>')
        result,_=run([article])
        self.assertFalse(result['incidents'])
        self.assertEqual(result['discoveries'][0]['categoryCandidates'],[])
        self.assertEqual(result['discoveries'][0]['locationStatus'],'unverified')
        self.assertEqual(result['discoveries'][0]['contentHash'],m.parse_posts(raw([article]),NOW)[0]['contentHash'])

    def test_fair_retained_rechecks_drain_three_at_a_time_and_skip_recent_feed(self):
        records,docs=known(7);original=copy.deepcopy(records)
        first,f=run([post(81007)],records,documents=docs)
        self.assertEqual(len(f.calls),3);self.assertEqual(first['state']['caribePendingRechecks'],3)
        self.assertEqual(first['sourceStatus'],'unavailable')
        self.assertEqual([p['url'] for p,_ in f.calls],[m.endpoint(str(i)) for i in range(81001,81004)])
        second,g=run([post(81007)],records,first['state'],docs,now='2026-10-16T05:00:00Z')
        self.assertEqual([p['url'] for p,_ in g.calls],[m.endpoint(str(i)) for i in range(81004,81007)])
        self.assertEqual(second['sourceStatus'],'healthy');self.assertFalse(second['state']['caribePendingRechecks'])
        self.assertEqual(records,original)

    def test_same_timestamp_changed_complete_detail_holds_original_and_preserves_dates(self):
        records,docs=known();before=next(iter(records.values()));changed=post()
        changed['content']['rendered']+='<p>El informe anterior era falso.</p>'
        docs[m.endpoint('81001')]=raw(changed)
        result,_=run(records=records,documents=docs);row=result['incidents'][0]
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
        self.assertEqual(row['occurredAt'],before['occurredAt']);self.assertEqual(row['firstSeenAt'],before['firstSeenAt'])
        self.assertEqual(row['reports'][1],before['reports'][0]);self.assertEqual(row['updatedAt'],NOW)
        again,_=run(records=merge(records,result),documents=docs,previous=result['state'],now='2026-10-16T11:00:00Z')
        self.assertFalse(again['incidents'])

    def test_structured_known404_holds_without_retraction_or_new_event_date(self):
        records,docs=known();before=next(iter(records.values()))
        missing=raw(dict(code='rest_post_invalid_id',message='El ID no es válido.',data={'status':404}))
        docs[m.endpoint('81001')]=(dict(httpStatus=404),missing)
        result,_=run(records=records,documents=docs);row=result['incidents'][0]
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
        self.assertEqual(row['occurredAt'],before['occurredAt']);self.assertEqual(row['firstSeenAt'],before['firstSeenAt'])
        self.assertEqual(row['reports'][1],before['reports'][0]);self.assertIn('not a resolution',row['sourceMetadata']['inaccessibleMeaning'])
        self.assertEqual(row['reports'][0]['publishedAt'],before['reports'][0]['publishedAt'])
        self.assertEqual(result['sourceStatus'],'unavailable')
        repeat,_=run(records=merge(records,result),previous=result['state'],documents=docs,now='2026-10-16T05:30:00Z')
        self.assertFalse(repeat['incidents']);self.assertEqual(repeat['state']['caribeQuarantine']['81001']['attempts'],2)

    def test_restored_article_after404_stays_held_pending_review(self):
        records,docs=known();url=m.endpoint('81001')
        failure,_=run(records=records,documents={url:(dict(httpStatus=404),raw({'code':'rest_post_invalid_id','message':'Missing','data':{'status':404}}))})
        retained=merge(records,failure)
        restored,_=run(records=retained,previous=failure['state'],documents=docs,now='2026-10-16T05:30:00Z')
        self.assertFalse(restored['state']['caribeQuarantine']);self.assertFalse(restored['incidents'])
        self.assertEqual(next(iter(retained.values()))['evidenceStatus'],'unverified')

    def test_generic_missing_wrong_code_wrong_identity_and_redirect_are_quarantined_only(self):
        records,_=known()
        for result,body in [
            (dict(httpStatus=404),b'<html>Not found</html>'),
            (dict(httpStatus=410),raw({'code':'rest_post_invalid_id','message':'Gone','data':{'status':404}})),
            (dict(httpStatus=404),raw({'code':'rest_forbidden','message':'Private','data':{'status':404}})),
            (dict(httpStatus=200),raw(post(81002))),
            (dict(httpStatus=200,finalUrl='https://other.test/'),raw(post())),
            (dict(httpStatus=304),None),
        ]:
            with self.subTest(result=result):
                out,_=run(records=records,documents={m.endpoint('81001'):(result,body)})
                self.assertFalse(out['incidents']);self.assertEqual(out['sourceStatus'],'unavailable')
                self.assertEqual(out['state']['caribeFailedRechecks'],1)

    def test_retry_backoff_does_not_block_new_articles_or_unchecked_old_ids(self):
        records,docs=known(5);url=m.endpoint('81001');docs[url]=(dict(httpStatus=503),None)
        first,_=run(records=records,documents=docs)
        self.assertEqual(first['state']['caribeQuarantine']['81001']['nextRetryAt'],'2026-10-16T05:30:00Z')
        second,f=run([post(82001)],records,first['state'],docs,now='2026-10-16T05:00:00Z')
        self.assertNotIn(url,[p['url'] for p,_ in f.calls]);self.assertEqual(len(f.calls),2)
        self.assertEqual(len(second['incidents']),1);self.assertEqual(second['state']['caribeFailedRechecks'],1)
        self.assertEqual(second['sourceStatus'],'unavailable')

    def test_failed_detail_full_hash_retained_and_other_valid_article_still_flows(self):
        records,docs=known();body=b'x'*101;docs[m.endpoint('81001')]=body
        with patch.object(m,'MAX_RESPONSE_BYTES',1000):
            # valid initial representation <1000; detail exceeds its own limit
            body=b'x'*1001;docs[m.endpoint('81001')]=body
            out,_=run([post(82001,body='<p>Un hombre murió este martes tras ser atacado a balazos en Matina. El OIJ investiga.</p>')],records,documents=docs)
        self.assertEqual(len(out['incidents']),1)
        entry=out['state']['caribeQuarantine']['81001']
        self.assertEqual(entry['bytes'],1001);self.assertEqual(entry['responseHash'],hashlib.sha256(body).hexdigest())

    def test_atomic_bad_listing_aggregate_budget_and_transaction_no_input_mutation(self):
        records,docs=known(3);prior={'lastSuccessAt':'2026-10-15T00:00:00Z'}
        original=(copy.deepcopy(records),copy.deepcopy(prior))
        bad=post(82001);bad['link']='https://evil.test/post'
        fetcher=Fetch(docs)
        with self.assertRaises(ValueError):m.collect_caribe_source(SOURCE,POLICY,raw([post(),bad]),NOW,COUNTRIES,prior,records,fetcher)
        self.assertFalse(fetcher.calls)
        with patch.object(m,'MAX_TOTAL_BYTES',1000),self.assertRaises(ValueError):run(records=records,previous=prior,documents=docs)
        self.assertEqual((records,prior),original)
        first,_=run(records=records,previous=prior,documents=docs)
        rerun,_=run(records=records,previous=prior,documents=docs)
        self.assertEqual(first,rerun)  # discard candidate => repeat exact cursor/work

    def test_disabled_inactive_and_identity_mismatch_fail_before_child_fetch(self):
        records,docs=known()
        variants=[dict(source=dict(SOURCE,enabled=False)),dict(policy=dict(POLICY,enabled=False)),
            dict(source=dict(SOURCE,url='https://evil.test/')),dict(countries={'CR':dict(status='suspended')})]
        for kw in variants:
            f=Fetch(docs)
            with self.assertRaises(ValueError):m.collect_caribe_source(kw.get('source',SOURCE),kw.get('policy',POLICY),b'[]',NOW,kw.get('countries',COUNTRIES),{},records,f)
            self.assertFalse(f.calls)

    def test_manual_review_owns_current_and_changed_url_for_same_id(self):
        records,docs=known();before=next(iter(records.values()));review=[dict(sourceId=m.SOURCE_ID,url=post()['link'])]
        changed=post();changed['link']=changed['link'].replace('noticia','otra')
        out,f=run([changed],records,documents=docs,reviews=review)
        self.assertFalse(out['incidents']);self.assertFalse(f.calls);self.assertFalse(out['state']['caribeRechecks'])
        other=copy.deepcopy(before);other['sourceMetadata']['parser']='manual-reviewed'
        out,f=run([changed],{other['id']:other},documents=docs)
        self.assertFalse(out['incidents']);self.assertFalse(f.calls)

    def test_state_cannot_request_arbitrary_url_or_forge_long_cooldown(self):
        records,docs=known();good,_=run(records=records,documents={})
        for mutate in [lambda s:s['caribeQuarantine']['81001'].update(url='https://evil.test/'),
            lambda s:s['caribeQuarantine']['81001'].update(nextRetryAt='2027-01-01T00:00:00Z'),
            lambda s:s['caribeRechecks']['81001'].update(url='https://evil.test/')]:
            state=copy.deepcopy(good['state']);mutate(state);f=Fetch(docs)
            with self.assertRaises(ValueError):m.collect_caribe_source(SOURCE,POLICY,b'[]',NOW,COUNTRIES,state,records,f)
            self.assertFalse(f.calls)

    def test_request_budget_checked_before_fifth_fetch(self):
        f=Fetch({m.endpoint('81001'):raw(post())});req=m.Requests(POLICY,f,NOW,None)
        req.accept(b'[]',m.LATEST_URL,dict(httpStatus=200))
        for _ in range(3):req.get('81001')
        with self.assertRaises(ValueError):req.get('81001')
        self.assertEqual(len(f.calls),3)

    def test_saturated_interval_gap_is_retained_after_later_success(self):
        rows=[post(82000+i) for i in range(100)]
        first,_=run(rows,previous={'lastSuccessAt':'2026-10-10T00:00:00Z'})
        self.assertFalse(first['state']['caribeCurrentIntervalComplete'])
        self.assertEqual(first['state']['caribeCoverageGaps'][0]['from'],'2026-10-10T00:00:00Z')
        later,_=run(rows,merge({},first),first['state'],now='2026-10-16T05:00:00Z')
        self.assertTrue(later['state']['caribeCurrentIntervalComplete'])
        self.assertEqual(later['state']['caribeCoverageGaps'],first['state']['caribeCoverageGaps'])
        self.assertFalse(later['state']['caribeAllYearBackfill'])

    def test_floor_suppresses_only_new_out_of_window_records(self):
        records,docs=known()
        result,_=run([post()],history_from='2026-10-14');self.assertFalse(result['incidents'])
        changed=post();changed['content']['rendered']+='<p>El informe era falso.</p>'
        result,_=run([changed],records,history_from='2026-10-14');self.assertEqual(len(result['incidents']),1)

    def test_exact_complete_retained_latest_corpus(self):
        path=Path(__file__).resolve().parents[1]/'artifacts/caribe-crime-20261016/posts.response'
        if not path.exists():self.skipTest('Complete provider capture remains local')
        body=path.read_bytes();f=Fetch({})
        out=m.collect_caribe_source(SOURCE,POLICY,body,NOW,COUNTRIES,{}, {},f)
        self.assertEqual(len(out['incidents']),11);self.assertFalse(f.calls)
        self.assertEqual(out['state']['caribeValidatedRepresentations'],100)
        self.assertEqual(out['state']['caribeCoverageGaps'][0]['reason'],'bounded_initial_latest100')
        for row in out['incidents']:
            self.assertEqual(row['location'],{'precision':'country'});self.assertEqual(row['category'],'crime')
            self.assertIn('Caribe Actual reports',row['summary'])


if __name__=='__main__':unittest.main()
