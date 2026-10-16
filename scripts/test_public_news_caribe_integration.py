"""Offline complete-publication tests for bounded Spanish crime collection."""
import copy
import datetime as dt
import hashlib
import io
import json
import shutil
import signal
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import public_data_pipeline as pipeline
import public_data_refresh as refresh
import public_news_caribe_fetch as caribe
from test_public_news_caribe import post, NOW
from test_public_news_caribe_fetch import SOURCE as IDENTITY, POLICY, raw

COUNTRY=dict(code='CR',name='Costa Rica',region='Americas',lat=9.9,lon=-84.1,status='active',
    evidenceUrl='https://www.peacecorps.gov/costa-rica/',evidenceText='Synthetic reviewed-country fixture.',
    evidenceDate='2026-10-01',verifiedAt='2026-10-01T00:00:00Z',reviewDue='2026-11-15',languages=['es'])
SOURCE=dict(IDENTITY,cadenceMinutes=30,verifiedAt='2026-10-01T00:00:00Z',notes='Synthetic offline source.')


def initialize(root, enabled=True):
    source=dict(SOURCE,enabled=enabled)
    for name,value in [('countries.json',[COUNTRY]),('sources.json',[source]),
        ('collection-policy.json',{'sources':[dict(POLICY,enabled=enabled)]}),
        ('geo/index.json',[]),('country-iso3.json',{'CRI':'CR'}),('admin-places.json',[])]:
        pipeline.save(root/'config'/name,value)
    data=root/'docs/data'
    pipeline.save(data/'snapshot.json',dict(schemaVersion=1,mode='live',generatedAt='2026-10-16T00:00:00Z',
        countries=[COUNTRY],sources=[source],incidents=[],coverage=[],archiveTotal=0,
        history={'recordCount':0,'from':'2025-10-16T00:00:00Z','to':'2026-10-16T00:00:00Z','sources':[SOURCE['id']],'notes':[]}))
    pipeline.write_archive(data,{}, {'CR':COUNTRY})
    return data


def client(documents):
    calls=[]
    def fetched(policy,previous,now,fixtures):
        calls.append((copy.deepcopy(policy),copy.deepcopy(previous)))
        value=documents.get(policy['url'])
        if isinstance(value,tuple):result,body=value
        else:result,body=dict(httpStatus=200 if value is not None else 503,finalUrl=policy['url']),value
        result=dict(result,sourceId=policy['sourceId'],url=policy['url'],lastAttemptAt=now)
        if body is None:result.update(status='unavailable',lastSuccessAt=previous.get('lastSuccessAt'),message='Synthetic source failure')
        return result,body
    fetched.calls=calls
    return fetched


class CaribeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'collector';self.data=initialize(self.root)
        self.documents={caribe.LATEST_URL:raw([post()])}

    def tearDown(self):self.temp.cleanup()

    def records(self):return pipeline.read_archive(self.data,{'CR':COUNTRY})
    def state(self):return pipeline.load(self.data/'collector-state.json',{}).get(SOURCE['id'],{})

    def run_refresh(self,documents=None,now=NOW,fetcher=None):
        fetched=fetcher or client(self.documents if documents is None else documents)
        def runner(command,**kwargs):
            stage=Path(command[command.index('--root')+1])
            with patch.object(pipeline,'fetch',fetched):pipeline.run(stage,dt.datetime.fromisoformat(now.replace('Z','+00:00')))
        result=refresh.refresh(self.root,dt.datetime.fromisoformat(now.replace('Z','+00:00')),runner=runner,environment={})
        return result,fetched

    def test_disabled_source_or_policy_never_requests_and_remains_visible_as_disabled(self):
        for source_enabled,policy_enabled in [(False,True),(True,False)]:
            pipeline.save(self.root/'config/sources.json',[dict(SOURCE,enabled=source_enabled)])
            pipeline.save(self.root/'config/collection-policy.json',{'sources':[dict(POLICY,enabled=policy_enabled)]})
            with patch.object(pipeline,'fetch',side_effect=AssertionError('Disabled source requested')):
                audit=pipeline.run(self.root,dt.datetime.fromisoformat(NOW))
            self.assertEqual(audit['requested'],0)
            self.assertEqual(pipeline.load(self.data/'snapshot.json')['coverage'][0]['status'],'disabled')

    def test_canonical_publication_repeat_and_discovery_revision_keep_full_evidence(self):
        first,fetched=self.run_refresh();self.assertEqual(first['outcome'],'complete',first['errors'])
        original=copy.deepcopy(next(iter(self.records().values())))
        self.assertEqual((original['category'],original['evidenceStatus'],original['severity']),('crime','reported','unknown'))
        self.assertEqual(original['reports'][0]['language'],'es');self.assertEqual(len(fetched.calls),1)
        self.assertEqual(original['location'],{'precision':'country'})
        discovery=pipeline.load(self.data/'discovery.json')[0]
        self.assertRegex(discovery['contentHash'],'^[a-f0-9]{64}$')
        repeat,_=self.run_refresh(now='2026-10-16T05:00:00Z')
        self.assertEqual(repeat['outcome'],'complete',repeat['errors']);self.assertEqual(repeat['changedIncidents'],[])
        self.assertEqual(next(iter(self.records().values())),original)
        changed=post();changed['content']['rendered']+='<p>El informe anterior era falso.</p>'
        corrected,_=self.run_refresh({caribe.LATEST_URL:raw([changed])},'2026-10-16T05:30:00Z')
        self.assertEqual(corrected['outcome'],'complete',corrected['errors'])
        row=next(iter(self.records().values()))
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
        self.assertEqual(row['occurredAt'],original['occurredAt']);self.assertEqual(row['firstSeenAt'],original['firstSeenAt'])
        self.assertIn(original['reports'][0],row['reports']);self.assertEqual(row['revisions'][0]['previousReports'],original['reports'])
        revised=pipeline.load(self.data/'discovery.json')[0]
        self.assertEqual(revised['firstSeenAt'],discovery['firstSeenAt'])
        self.assertEqual(revised['revisions'][0]['previous'],discovery['contentHash'])

    def test_partial_only_cycle_publishes_new_article_and_preserves_failure_clock(self):
        self.run_refresh();before=copy.deepcopy(self.records());old_success=self.state()['lastSuccessAt']
        documents={caribe.LATEST_URL:raw([post(81002)])}
        result,fetched=self.run_refresh(documents,'2026-10-16T10:30:00Z')
        self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(len(fetched.calls),2)
        self.assertEqual(len(self.records()),2)
        self.assertEqual(self.records()[next(iter(before))],next(iter(before.values())))
        self.assertEqual(self.state()['lastSuccessAt'],old_success)
        row=result['sources'][0];self.assertTrue(refresh.validated_partial(row,[POLICY]))
        self.assertIn('81001',self.state()['caribeQuarantine'])
        documents[caribe.endpoint('81001')]=raw(post())
        recovery,_=self.run_refresh(documents,'2026-10-16T11:30:00Z')
        self.assertEqual(recovery['outcome'],'complete',recovery['errors'])
        self.assertEqual(self.state()['lastSuccessAt'],'2026-10-16T11:30:00Z')
        self.assertFalse(self.state()['caribeQuarantine'])

    def test_empty_listing_with_verified_missing_post_commits_hold_without_resolution(self):
        self.run_refresh();before=copy.deepcopy(next(iter(self.records().values())))
        missing=raw({'code':'rest_post_invalid_id','message':'No existe.','data':{'status':404}})
        result,_=self.run_refresh({caribe.LATEST_URL:b'[]',caribe.endpoint('81001'):
            (dict(httpStatus=404,finalUrl=caribe.endpoint('81001')),missing)},'2026-10-16T10:30:00Z')
        self.assertEqual(result['outcome'],'degraded',result['errors'])
        self.assertTrue(refresh.validated_partial(result['sources'][0],[POLICY]))
        self.assertEqual(result['sources'][0]['validatedPartial']['validatedRepresentations'],0)
        row=next(iter(self.records().values()))
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
        self.assertEqual(row['occurredAt'],before['occurredAt']);self.assertIn(before['reports'][0],row['reports'])
        self.assertIn('not a resolution',row['sourceMetadata']['inaccessibleMeaning'])
        self.assertEqual(row['reports'][0]['contentHash'],hashlib.sha256(missing).hexdigest())

    def test_pending_validated_rechecks_can_advance_without_failed_requests(self):
        self.run_refresh({caribe.LATEST_URL:raw([post(i) for i in range(81001,81006)])})
        docs={caribe.LATEST_URL:b'[]',**{caribe.endpoint(str(i)):raw(post(i)) for i in range(81001,81006)}}
        result,fetched=self.run_refresh(docs,'2026-10-16T10:30:00Z')
        self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(len(fetched.calls),4)
        self.assertEqual(self.state()['caribePendingRechecks'],2);self.assertFalse(self.state()['caribeQuarantine'])
        self.assertTrue(refresh.validated_partial(result['sources'][0],[POLICY]))
        done,fetched=self.run_refresh(docs,'2026-10-16T11:00:00Z')
        self.assertEqual(done['outcome'],'complete',done['errors']);self.assertEqual(len(fetched.calls),3)

    def test_failed_listing_retains_complete_records_and_checkpoint(self):
        self.run_refresh();records=copy.deepcopy(self.records());state=copy.deepcopy(self.state())
        for value in (None,b'{invalid complete json', (dict(httpStatus=200,finalUrl='https://caribeactual.com/login'),b'[]')):
            result,_=self.run_refresh({caribe.LATEST_URL:value},'2026-10-16T05:00:00Z')
            self.assertEqual(result['outcome'],'failed');self.assertEqual(self.records(),records);self.assertEqual(self.state(),state)

    def add_independent_source(self):
        source=dict(SOURCE,id='other-reviewed-news',url='https://example.org/',feedUrl='https://example.org/feed',language='en')
        pipeline.save(self.root/'config/sources.json',[SOURCE,source])
        pipeline.save(self.root/'config/collection-policy.json',{'sources':[POLICY,dict(sourceId=source['id'],mode='discovery',cadenceMinutes=30,url=source['feedUrl'])]})
        return source

    def test_source_deadline_preserves_prior_records_while_other_source_publishes(self):
        self.run_refresh();before=copy.deepcopy(self.records());state=copy.deepcopy(self.state());other=self.add_independent_source()
        ordinary=client({caribe.LATEST_URL:raw([post()]),other['feedUrl']:b'<rss><channel><item><title>Crime report</title><link>https://example.org/report</link><pubDate>2026-10-16T05:00:00Z</pubDate><description>Robbery reported in Costa Rica.</description></item></channel></rss>'})
        def fetched(policy,previous,now,fixtures):
            if policy['sourceId']==SOURCE['id']:time.sleep(.2)
            return ordinary(policy,previous,now,fixtures)
        handler=signal.getsignal(signal.SIGALRM)
        with patch.object(pipeline,'CARIBE_DEADLINE_SECONDS',.02):
            result,_=self.run_refresh(now='2026-10-16T05:00:00Z',fetcher=fetched)
        self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(self.records(),before)
        self.assertEqual(self.state()['lastSuccessAt'],state['lastSuccessAt'])
        self.assertEqual(self.state()['caribeRechecks'],state['caribeRechecks'])
        self.assertIn('wall-time budget',result['sources'][0]['message'])
        self.assertNotIn('validatedPartial',result['sources'][0])
        self.assertEqual(signal.getsignal(signal.SIGALRM),handler);self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        self.assertTrue(any(d['sourceId']==other['id'] for d in pipeline.load(self.data/'discovery.json')))

    def test_wrong_source_policy_or_inactive_country_rejected_before_any_request(self):
        for change in ('url','source','country'):
            initialize(self.root)
            if change=='url':pipeline.save(self.root/'config/collection-policy.json',{'sources':[dict(POLICY,url='https://caribeactual.com/other')]})
            if change=='source':pipeline.save(self.root/'config/sources.json',[dict(SOURCE,feedUrl='https://caribeactual.com/other')])
            if change=='country':pipeline.save(self.root/'config/countries.json',[dict(COUNTRY,reviewDue='2026-10-01')])
            forbidden=Mock(side_effect=AssertionError('Unreviewed request'))
            result,_=self.run_refresh(fetcher=forbidden)
            self.assertEqual(result['outcome'],'failed');forbidden.assert_not_called()

    def test_canonical_record_failure_cannot_advance_checkpoint_in_otherwise_successful_cycle(self):
        self.run_refresh();before=copy.deepcopy(self.records());state=copy.deepcopy(self.state());other=self.add_independent_source()
        documents={caribe.LATEST_URL:raw([post(81002)]),other['feedUrl']:b'<rss><channel></channel></rss>'}
        original=caribe.collect_caribe_source
        def overlarge(*args,**kwargs):
            batch=original(*args,**kwargs)
            batch['incidents'][0]['summary']='x'*pipeline.PART_LIMIT
            return batch
        with patch.object(caribe,'collect_caribe_source',side_effect=overlarge):
            result,_=self.run_refresh(documents,'2026-10-16T05:00:00Z')
        self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(self.records(),before)
        self.assertIn('12MB',result['sources'][0]['message']);self.assertNotIn('validatedPartial',result['sources'][0])
        self.assertEqual(self.state()['lastSuccessAt'],state['lastSuccessAt'])
        self.assertEqual({k:v for k,v in self.state().items() if k.startswith('caribe')},
                         {k:v for k,v in state.items() if k.startswith('caribe')})

    def test_partial_marker_cannot_bypass_source_binding_or_evidence_gates(self):
        self.run_refresh();result,_=self.run_refresh({caribe.LATEST_URL:b'[]'},'2026-10-16T10:30:00Z')
        valid=result['sources'][0];self.assertTrue(refresh.validated_partial(valid,[POLICY]))
        variants=[]
        for name,value in [('sourceId','other'),('caribeListingContentHash','0'*64),('caribeListingValidated',False),('caribeVersion',True)]:
            row=copy.deepcopy(valid);row[name]=value;variants.append(row)
        for name,value in [('adapter','other'),('validatedRepresentations',True),('pendingRechecks',-1),('failedRechecks',0),('initialUrl','https://other.example/')]:
            row=copy.deepcopy(valid);row['validatedPartial'][name]=value;variants.append(row)
        row=copy.deepcopy(valid);row['caribeQuarantine']['81001']['url']='https://other.example/';variants.append(row)
        for row in variants:self.assertFalse(refresh.validated_partial(row,[POLICY]))
        self.assertFalse(refresh.validated_partial(valid,[dict(POLICY,enabled=False)]))
        self.assertFalse(refresh.validated_partial(valid,[POLICY,POLICY]))

    def test_exact_404_transport_exception_is_bound_to_reviewed_numeric_identity(self):
        body=raw({'code':'rest_post_invalid_id','message':'Missing','data':{'status':404}})
        exact=dict(POLICY,sourceId=SOURCE['id']+'-81001',url=caribe.endpoint('81001'))
        for policy,accepted in [(exact,True),(dict(exact,sourceId=SOURCE['id']+'-81002'),False),
            (dict(exact,adapter='other'),False),(dict(exact,url=caribe.LATEST_URL),False)]:
            error=urllib.error.HTTPError(policy['url'],404,'Missing',{},io.BytesIO(body))
            self.addCleanup(error.close)
            opener=Mock();opener.open.side_effect=error
            with patch.object(pipeline.urllib.request,'build_opener',return_value=opener):
                result,response=pipeline.fetch(policy,{},NOW)
            self.assertEqual(response,body if accepted else None)
            if accepted:self.assertEqual(result['sha256'],hashlib.sha256(body).hexdigest())


if __name__=='__main__':unittest.main()
