"""Complete offline CAP transactions, retry persistence and degraded publication."""
import copy
import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
if not (SCRIPTS/'public_data_pipeline.py').exists():SCRIPTS=Path(__file__).resolve().parent
sys.path.insert(0,str(SCRIPTS))
import public_data_pipeline as pipeline
import public_data_refresh as refresh
import test_public_cap as cap_fixtures

SID='cap-vu-vmgd-en'
HOST='https://cap-sources.s3.amazonaws.com'
URL=HOST+'/vu-vmgd-en/rss.xml'
SOURCE=dict(id=SID,name='Synthetic reviewed CAP source',url=HOST,feedUrl=URL,countryCodes=['VU'],
 kind='cap',tier='official',language='en',cadenceMinutes=15,enabled=True,access='public-feed',verifiedAt='2026-10-01T00:00:00Z')
POLICY=dict(sourceId=SID,mode='official',cadenceMinutes=15,url=URL)
COUNTRY=dict(code='VU',name='Vanuatu',region='Pacific',lat=-16,lon=167,status='active',
 evidenceUrl='https://www.peacecorps.gov/vanuatu/',evidenceText='Synthetic reviewed fixture.',evidenceDate='2026-10-01',
 verifiedAt='2026-10-01T00:00:00Z',reviewDue='2026-11-15',languages=['en'])
NOW='2026-10-16T03:00:00Z'


def feed(names):
 return ('<rss><channel>'+''.join(f'<item><link>{HOST}/{n}</link></item>' for n in names)+'</channel></rss>').encode()


def body(identifier='a',**kwargs):
 return cap_fixtures.CapPartialTests().bulletin(identifier=identifier,expires='2026-10-20T07:00:00Z',**kwargs)


def initialize(root):
 for name,value in [('countries.json',[COUNTRY]),('sources.json',[SOURCE]),('collection-policy.json',{'sources':[POLICY]}),
                    ('geo/index.json',[]),('country-iso3.json',{'VUT':'VU'}),('admin-places.json',[])]:
  pipeline.save(root/'config'/name,value)
 data=root/'docs/data'
 snapshot=dict(schemaVersion=1,mode='live',generatedAt='2026-10-16T02:00:00Z',countries=[COUNTRY],sources=[SOURCE],
 incidents=[],coverage=[],archiveTotal=0,history={'recordCount':0,'from':'2025-10-16T00:00:00Z','to':'2026-10-16T02:00:00Z','sources':[SID],'notes':[]})
 pipeline.save(data/'snapshot.json',snapshot);pipeline.write_archive(data,{}, {'VU':COUNTRY})
 return data


def client(bodies,names=None,initial=None):
 calls=[]
 def fetch(policy,previous,now,fixtures):
  calls.append((policy['url'],copy.deepcopy(previous)))
  result=dict(sourceId=policy['sourceId'],url=policy['url'],lastAttemptAt=now)
  if policy['url']==URL:
   if initial is not None:return initial(result,previous,now)
   raw=feed(list(bodies) if names is None else names)
  else:raw=bodies[policy['url'].removeprefix(HOST+'/')]
  if raw is None:return dict(result,status='unavailable',lastSuccessAt=previous.get('lastSuccessAt'),message='Synthetic unavailable child'),None
  return dict(result,httpStatus=200,etag='synthetic-etag',sha256=hashlib.sha256(raw).hexdigest()),raw
 fetch.calls=calls
 return fetch


class CapPartialPipelineTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'collector';self.data=initialize(self.root)
 def tearDown(self):self.temp.cleanup()
 def run_pipeline(self,fetch,now=NOW):
  with patch.object(pipeline,'fetch',fetch):return pipeline.run(self.root,dt.datetime.fromisoformat(now))
 def run_refresh(self,fetch,now=NOW,corrupt=None):
  def runner(command,**kwargs):
   stage=Path(command[command.index('--root')+1])
   with patch.object(pipeline,'fetch',fetch):pipeline.run(stage,dt.datetime.fromisoformat(now))
   if corrupt:corrupt(stage)
  return refresh.refresh(self.root,dt.datetime.fromisoformat(now),runner=runner,environment={})
 def state(self):return pipeline.load(self.data/'collector-state.json')[SID]
 def records(self):return pipeline.read_archive(self.data,{'VU':COUNTRY})
 def before(self):return {str(p.relative_to(self.data)):p.read_bytes() for p in self.data.rglob('*.json') if p.name!='refresh-status.json'}
 def baseline(self):
  result=self.run_refresh(client({}),now='2026-10-16T02:30:00Z');self.assertEqual(result['outcome'],'complete');return self.state()['lastSuccessAt']
 def bad(self,**kwargs):return cap_fixtures.CapPartialTests().bad(identifier='bad',**kwargs)

 def test_severe_plus_bad_publishes_with_degraded_health_and_actual_full_success_clock(self):
  prior=self.baseline();result=self.run_refresh(client({'bad':self.bad(),'a':body()}))
  self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(result['lastSuccessfulCollectionAt'],prior)
  state=self.state();self.assertEqual((state['status'],state['lastSuccessAt']),('unavailable',prior))
  self.assertTrue(refresh.validated_partial(state,[POLICY]));self.assertEqual(len(state['capQuarantine']),1)
  rows=self.records();self.assertEqual(len(rows),1);row=next(iter(rows.values()))
  self.assertEqual((row['category'],row['severity']),('weather','severe'))
  self.assertEqual(pipeline.load(self.data/'snapshot.json')['incidents'],[pipeline.public_snapshot_record(row)])
  self.assertEqual(row['reports'][0]['contentHash'],hashlib.sha256(body()).hexdigest())

 def test_repeat_partial_and_omitted_url_retry_then_recovery(self):
  prior=self.baseline();self.run_refresh(client({'bad':self.bad(),'a':body()}));rows=copy.deepcopy(self.records())
  second=client({'bad':self.bad(),'a':body()},names=['a'])
  result=self.run_refresh(second,now='2026-10-16T03:15:00Z');self.assertEqual(result['outcome'],'degraded')
  self.assertEqual(self.records(),rows);self.assertIn(HOST+'/bad',[u for u,_ in second.calls])
  self.assertNotIn('etag',second.calls[0][1]);self.assertNotIn('lastModified',second.calls[0][1])
  self.assertEqual(self.state()['lastSuccessAt'],prior)
  recovered=client({'bad':body('bad'),'a':body()},names=['a'])
  result=self.run_refresh(recovered,now='2026-10-16T03:30:00Z');self.assertEqual(result['outcome'],'complete',result['errors'])
  self.assertEqual(self.state()['capQuarantine'],[]);self.assertNotIn('validatedPartial',self.state())
  self.assertEqual(self.state()['lastSuccessAt'],'2026-10-16T03:30:00Z');self.assertEqual(len(self.records()),2)

 def test_excluded_routine_message_is_valid_partial_check_without_public_incidents(self):
  result=self.run_refresh(client({'bad':self.bad(),'routine':body(severity='Moderate')}))
  self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertIsNone(result['lastSuccessfulCollectionAt'])
  self.assertEqual(self.state()['capValidatedMessages'],1);self.assertEqual(self.state()['acceptedCount'],0)
  self.assertEqual(pipeline.load(self.data/'snapshot.json')['incidents'],[])
  self.assertEqual(len(self.records()),1);self.assertFalse(pipeline.public_incident(next(iter(self.records().values()))))

 def test_all_bad_does_not_publish_but_retained_failure_audit_retries_unlisted_url(self):
  prior=self.baseline();before=self.before()
  result=self.run_refresh(client({'bad':self.bad()}));self.assertEqual(result['outcome'],'failed')
  self.assertEqual(self.before(),before);self.assertNotIn('validatedPartial',result['sources'][0])
  self.assertEqual(len(result['sources'][0]['capQuarantine']),1)
  later=client({'bad':self.bad(),'a':body()},names=['a'])
  result=self.run_refresh(later,now='2026-10-16T03:15:00Z');self.assertEqual(result['outcome'],'degraded')
  self.assertIn(HOST+'/bad',[u for u,_ in later.calls]);self.assertEqual(self.state()['lastSuccessAt'],prior)

 def test_audit_only_queue_survives_outer_validation_failure_without_fake_attempts(self):
  prior=self.baseline();self.run_refresh(client({'bad':self.bad()}));before=self.before()
  def corrupt(stage):pipeline.save(stage/'docs/data/history.json',[])
  failed=self.run_refresh(client({'bad':self.bad(),'a':body()},names=['a']),now='2026-10-16T03:15:00Z',corrupt=corrupt)
  self.assertEqual(failed['outcome'],'failed');self.assertEqual(failed['sources'],[]);self.assertEqual(self.before(),before)
  retained=failed['retainedCapQuarantines'][SID];self.assertEqual(len(retained),1)
  self.assertEqual(retained[0]['retrievedAt'],NOW);self.assertNotIn('validatedPartial',failed)
  later=client({'bad':self.bad(),'a':body()},names=['a'])
  result=self.run_refresh(later,now='2026-10-16T03:30:00Z')
  self.assertEqual(result['outcome'],'degraded');self.assertIn(HOST+'/bad',[u for u,_ in later.calls])
  self.assertEqual(self.state()['lastSuccessAt'],prior);self.assertNotIn('retainedCapQuarantines',result)

 def test_audit_only_queue_survives_repeated_outer_timeouts(self):
  self.baseline();self.run_refresh(client({'bad':self.bad()}));before=self.before()
  def timeout(command,**kwargs):raise subprocess.TimeoutExpired(command,480)
  for minute in (15,30):
   result=refresh.refresh(self.root,dt.datetime.fromisoformat(f'2026-10-16T03:{minute}:00Z'),runner=timeout,environment={})
   self.assertEqual(result['outcome'],'failed');self.assertEqual(result['sources'],[]);self.assertEqual(self.before(),before)
   self.assertEqual(result['retainedCapQuarantines'][SID][0]['retrievedAt'],NOW)
  later=client({'bad':body('bad')},names=[])
  result=self.run_refresh(later,now='2026-10-16T03:45:00Z');self.assertEqual(result['outcome'],'complete')
  self.assertIn(HOST+'/bad',[u for u,_ in later.calls]);self.assertNotIn('retainedCapQuarantines',result)
  self.assertEqual(self.state()['capQuarantine'],[])

 def test_malformed_correction_does_not_cancel_or_replace_original(self):
  self.run_refresh(client({'a':body()}),now='2026-10-16T02:30:00Z');old=copy.deepcopy(self.records());ref='issuer@example.test,a,2026-10-13T04:00:00Z'
  for minute,kind in [(0,'Update'),(15,'Cancel')]:
   result=self.run_refresh(client({'bad':self.bad(kind=kind,reference=ref),'b':body('b')}),now=f'2026-10-16T03:{minute:02}:00Z')
   self.assertEqual(result['outcome'],'degraded')
   for iid,row in old.items():self.assertEqual(self.records()[iid],row)
   self.assertEqual(self.state()['capQuarantine'][0]['identity']['kind'],kind)

 def test_valid_cancel_continues_during_unrelated_quarantine(self):
  self.run_refresh(client({'a':body()}),now='2026-10-16T02:30:00Z');old=next(iter(self.records().values()))
  cancel=body('cancel',kind='Cancel',sent='2026-10-13T06:00:00Z',reference='issuer@example.test,a,2026-10-13T04:00:00Z')
  result=self.run_refresh(client({'bad':self.bad(),'cancel':cancel}));self.assertEqual(result['outcome'],'degraded')
  row=self.records()[old['id']];self.assertEqual(row['state'],'cancelled');self.assertEqual(len(row['reports']),2)
  self.assertIn(old['reports'][0],row['reports'])

 def test_pending_queue_304_and_fetch_failures_cannot_refresh_full_success_or_drop_queue(self):
  self.baseline();self.run_refresh(client({'bad':self.bad(),'a':body()}));before=self.before()
  for minute,initial in [(15,lambda result,previous,now:(dict(result,status='healthy',httpStatus=304,lastSuccessAt=now,unchanged=True),None)),
                         (30,lambda result,previous,now:(dict(result,status='unavailable',lastSuccessAt=previous.get('lastSuccessAt')),None))]:
   result=self.run_refresh(client({},initial=initial),now=f'2026-10-16T03:{minute:02}:00Z')
   self.assertEqual(result['outcome'],'failed');self.assertEqual(self.before(),before)
   self.assertNotIn('validatedPartial',result['sources'][0]);self.assertEqual(len(result['sources'][0]['capQuarantine']),1)

 def test_queued_child_fetch_failure_keeps_archive_and_retry_state(self):
  self.baseline();self.run_refresh(client({'bad':self.bad(),'a':body()}));before=self.before()
  result=self.run_refresh(client({'bad':None,'new':body('new')},names=['new']),now='2026-10-16T03:15:00Z')
  self.assertEqual(result['outcome'],'failed');self.assertEqual(self.before(),before);self.assertNotIn('validatedPartial',result['sources'][0])

 def test_record_size_failure_rolls_back_new_records_and_quarantine_checkpoint(self):
  self.baseline();before=self.before()
  with patch.object(pipeline,'PART_LIMIT',200):result=self.run_refresh(client({'bad':self.bad(),'a':body()}))
  self.assertEqual(result['outcome'],'failed');self.assertEqual(self.before(),before)
  self.assertNotIn('validatedPartial',result['sources'][0]);self.assertNotIn('capQuarantine',result['sources'][0])

 def test_outer_archive_failure_cannot_promote_partial_source_or_queue(self):
  self.baseline();before=self.before()
  def corrupt(stage):pipeline.save(stage/'docs/data/history.json',[])
  result=self.run_refresh(client({'bad':self.bad(),'a':body()}),corrupt=corrupt)
  self.assertEqual(result['outcome'],'failed');self.assertEqual(self.before(),before)

 def test_empty_complete_bad_body_has_valid_hash_diagnostic(self):
  result=self.run_refresh(client({'bad':b'','a':body()}))
  self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(self.state()['capQuarantine'][0]['bytes'],0)

 def test_partial_marker_requires_reviewed_policy_and_matching_bounded_diagnostics(self):
  self.run_refresh(client({'bad':self.bad(),'a':body()}));original=self.state()
  mutations=[lambda x:x['validatedPartial'].update(capVersion='1.1'),lambda x:x['validatedPartial'].update(indexUrl='https://unreviewed.test/feed'),
   lambda x:x['validatedPartial'].update(validatedMessages=0),lambda x:x['validatedPartial'].update(quarantinedBulletins=2),
   lambda x:x['capQuarantine'][0].update(url='https://unreviewed.test/bad'),lambda x:x['capQuarantine'][0].update(contentHash='bad'),
   lambda x:x['validatedPartial'].update(bulletinHosts=['unreviewed.test']),lambda x:x.update(status='healthy')]
  mutations.extend([lambda x:x['validatedPartial'].update(quarantinedBulletins=True),lambda x:x.update(capValidatedMessages=True)])
  for mutation in mutations:
   bad=copy.deepcopy(original);mutation(bad);self.assertFalse(refresh.validated_partial(bad,[POLICY]))
  for policy in [dict(POLICY,mode='discovery'),dict(POLICY,format='unreviewed'),dict(POLICY,adapter='unreviewed'),dict(POLICY,capVersion='1.1')]:
   self.assertFalse(refresh.validated_partial(original,[policy]))

 def test_unreviewed_source_cannot_emit_partial_marker(self):
  pipeline.save(self.root/'config/sources.json',[dict(SOURCE,tier='news')]);self.baseline();before=self.before()
  result=self.run_refresh(client({'bad':self.bad(),'a':body()}));self.assertEqual(result['outcome'],'failed')
  self.assertEqual(self.before(),before);self.assertNotIn('validatedPartial',result['sources'][0])


if __name__=='__main__':unittest.main()
