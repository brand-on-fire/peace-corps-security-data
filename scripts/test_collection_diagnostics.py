"""Offline timing attribution, bounds, interruption and publication-isolation checks."""
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_data_pipeline as pipeline
import public_data_refresh as refresh

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
POLICY = dict(sourceId='reviewed-parent-child', url='https://example.org/private-query?opaque=do-not-copy')


class Clock:
    def __init__(self):
        self.tick = 0
        self.lock = threading.Lock()
    def mono(self):
        with self.lock:
            self.tick += 1_000_000
            return self.tick
    def wall(self):
        return NOW
    def diagnostics(self, **kwargs):
        return pipeline.CollectionDiagnostics(clock=self.wall, monotonic=self.mono, **kwargs)


def response(policy, previous, now, fixture=None):
    return dict(sourceId=policy['sourceId'], url=policy['url'], httpStatus=200,
        transportTiming=dict(mode='live-http', requestStartedAt='2026-10-16T03:00:05.000Z',
            requestCompletedAt='2026-10-16T03:00:05.125Z', durationMs=125,
            outcome='response-complete', authorization='do-not-copy'),
        headers={'Set-Cookie':'do-not-copy'}, message='do-not-copy'), b'complete private body'


class CollectionDiagnosticTests(unittest.TestCase):
    def test_explicit_parent_binding_complete_result_and_metadata_projection(self):
        d = Clock().diagnostics()
        fetched = d.fetcher('reviewed-parent', 'child', response)(POLICY, {}, 'canonical-cycle')
        self.assertEqual(fetched, response(POLICY, {}, 'canonical-cycle'))
        summary = d.snapshot(); row = summary['requests'][0]
        self.assertEqual(row['sourceId'], 'reviewed-parent')
        self.assertEqual(row['phase'], 'child')
        self.assertEqual(row['urlSha256'], hashlib.sha256(POLICY['url'].encode()).hexdigest())
        self.assertEqual(row['transportTiming']['durationMs'], 125)
        self.assertEqual(row['durationMs'], 1)
        self.assertEqual(row['httpStatus'], 200)
        public = json.dumps(summary)
        for value in ('do-not-copy', 'complete private body', POLICY['url'], 'authorization', 'headers', 'Set-Cookie'):
            self.assertNotIn(value, public)
        self.assertEqual(summary['requestAggregates'][0]['transportDurationMsSum'], 125)

    def test_clock_rollback_does_not_change_monotonic_elapsed(self):
        walls = iter([NOW, NOW+dt.timedelta(seconds=10), NOW-dt.timedelta(seconds=10), NOW])
        ticks = iter([0, 1_000_000, 4_000_000, 5_000_000])
        d = pipeline.CollectionDiagnostics(clock=lambda:next(walls), monotonic=lambda:next(ticks))
        d.fetcher('parent','child',response)(POLICY,{},'canonical')
        row = d.snapshot()['requests'][0]
        self.assertEqual(row['durationMs'], 3)
        self.assertLess(row['endedAt'], row['startedAt'])

    def test_fixture_and_missing_transport_are_never_presented_as_live(self):
        d = Clock().diagnostics()
        d.fetcher('parent','initial',response)(POLICY,{},'canonical',Path('offline'))
        d.fetcher('parent','child',lambda *args:({'status':'unavailable'},None))(POLICY,{},'canonical')
        rows = d.snapshot()['requests']
        self.assertEqual([r['mode'] for r in rows], ['fixture-replay','unobserved'])
        self.assertTrue(all('transportTiming' not in r for r in rows))

    def test_private_deadline_and_regular_exception_keep_original_identity(self):
        for error,expected in [(pipeline._SourceDeadlineExpired(),'interrupted'),(ValueError('private-path'),'raised')]:
            d = Clock().diagnostics()
            def fail(*args): raise error
            with self.assertRaises(type(error)) as caught:
                d.fetcher('parent','child',fail)(POLICY,{},'canonical')
            self.assertIs(caught.exception, error)
            row = d.snapshot()['requests'][0]
            self.assertEqual((row['outcome'],row['mode']), (expected,'unobserved'))
            self.assertNotIn('transportTiming',row)
            self.assertNotIn('private-path',json.dumps(d.snapshot()))

    def test_real_fetch_304_and_timeout_remain_distinct(self):
        for error in (urllib.error.HTTPError(POLICY['url'],304,'Not Modified',{},io.BytesIO()),TimeoutError('slow')):
            d = Clock().diagnostics(); opener = Mock(); opener.open.side_effect = error
            with patch.object(pipeline.urllib.request,'build_opener',return_value=opener):
                result,raw = d.fetcher('parent','child',pipeline.fetch)(POLICY,{'lastSuccessAt':'earlier'},'canonical')
            self.assertIsNone(raw)
            row = d.snapshot()['requests'][0]
            self.assertEqual(row['outcome'],'returned')
            self.assertEqual(row['mode'],'live-http')
            if isinstance(error,urllib.error.HTTPError):
                self.assertEqual(row['httpStatus'],304)
                self.assertEqual(row['transportTiming']['outcome'],'response-complete')
                self.assertEqual(result['status'],'healthy')
                error.close()
            else:
                self.assertEqual(row['transportTiming']['outcome'],'request-failed')
                self.assertEqual(result['status'],'unavailable')

    def test_detail_and_identity_overflow_are_explicit_and_keep_exact_counts(self):
        d = Clock().diagnostics(detail_limit=2,aggregate_limit=1)
        for number in range(8):
            actual = d.fetcher(f'parent-{number}','child',response)(POLICY,{},'canonical')
            self.assertEqual(actual[1], b'complete private body')
        s=d.snapshot()
        self.assertEqual((s['requestCalls'],len(s['requests']),s['requestDetailsOmitted']),(8,2,6))
        self.assertEqual(sum(g['count'] for g in s['requestAggregates']),8)
        self.assertEqual(sum(g['transportCount'] for g in s['requestAggregates']),8)
        self.assertEqual(sum(g['transportDurationMsSum'] for g in s['requestAggregates']),1000)
        overflow=[g for g in s['requestAggregates'] if g['identityOverflow']]
        self.assertEqual(overflow[0]['count'],7)
        self.assertEqual(len(s['requestAggregates']),2)  # one identity plus explicit overflow bucket

    def test_four_worker_child_calls_do_not_mix_parent_identities_or_lose_counts(self):
        d=Clock().diagnostics(); barrier=threading.Barrier(4); active=peak=0; lock=threading.Lock()
        def parallel(policy,previous,now,fixture=None):
            nonlocal active,peak
            with lock: active+=1;peak=max(peak,active)
            barrier.wait(timeout=3)
            with lock: active-=1
            return response(policy,previous,now,fixture)
        def one(index):
            parent=f'parent-{index%2}'
            return d.fetcher(parent,'child',parallel)(dict(POLICY,url=f'https://example.org/{parent}/{index}'),{},'canonical')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(one,range(32)))
        s=d.snapshot();self.assertEqual(peak,4);self.assertEqual(len(results),32)
        self.assertEqual(s['requestCalls'],32);self.assertEqual(len(s['requests']),32)
        self.assertEqual({g['sourceId']:g['count'] for g in s['requestAggregates']},{'parent-0':16,'parent-1':16})
        self.assertEqual(sorted(r['sequence'] for r in s['requests']),list(range(1,33)))

    def test_stage_failures_and_aggregate_only_nested_geo_keep_interruptions(self):
        d=Clock().diagnostics(detail_limit=1)
        with d.stage('source-processing','parent'):
            with d.stage('geographic-assignment','parent',detail=False):pass
        with self.assertRaises(pipeline._SourceDeadlineExpired):
            with d.stage('interrupted'):raise pipeline._SourceDeadlineExpired()
        s=d.snapshot();self.assertEqual(s['stageCalls'],3)
        self.assertEqual(s['stageDetailsOmitted'],2)
        groups={g['stage']:g for g in s['stageAggregates']}
        self.assertGreater(groups['source-processing']['durationMsSum'],groups['geographic-assignment']['durationMsSum'])
        self.assertEqual(groups['interrupted']['outcomes'],{'interrupted':1})


class DiagnosticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'collector';self.data=self.root/'docs/data'
        country=dict(code='GH',name='Ghana',region='Africa',status='active',reviewDue='2026-11-04')
        pipeline.save(self.root/'config/countries.json',[country])
        pipeline.save(self.root/'config/country-iso3.json',{})
        pipeline.save(self.root/'config/geo/index.json',[dict(code='GH',bounds=[[0,4,0,4]])])
        pipeline.save(self.root/'config/geo/GH.json',[])
        self.sources=[dict(id=f'official-{i}',kind='usgs',language='en',countryCodes=['GH']) for i in range(5)]
        pipeline.save(self.root/'config/sources.json',self.sources)
        self.policies=[dict(sourceId=s['id'],mode='official',adapter='usgs-week',url='https://example.org/feed',cadenceMinutes=15) for s in self.sources]
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=self.policies))
        pipeline.save(self.data/'snapshot.json',dict(generatedAt='2026-10-16T02:45:00Z',coverage=[],history={'from':'2025-10-16T00:00:00Z'}))
        pipeline.write_archive(self.data,{}, {'GH':country})
    def tearDown(self):self.temp.cleanup()
    def adapter(self,source,policy,raw,now,countries,geo,previous,records,fetch,fixtures,**kwargs):
        for i in range(2):
            result,body=fetch(dict(policy,sourceId=policy['sourceId']+f'-child-{i}',url=f'https://example.org/{source["id"]}/{i}'),{},now,fixtures)
            self.assertEqual(body,b'complete private body')
        return dict(incidents=[],discoveries=[],state={},evidence=[])
    def test_pipeline_wires_all_initial_and_children_without_changing_snapshot(self):
        import public_official
        with patch.object(pipeline,'fetch',response),patch.object(public_official,'collect_official',self.adapter):
            audit=pipeline.run(self.root,NOW,diagnostics=Clock().diagnostics())
        d=audit['diagnostics'];self.assertEqual(d['requestCalls'],15)
        self.assertEqual(sum(r['phase']=='initial' for r in d['requests']),5)
        self.assertEqual(sum(r['phase']=='child' for r in d['requests']),10)
        self.assertEqual({g['sourceId']:g['count'] for g in d['requestAggregates'] if g['phase']=='child'}, {s['id']:2 for s in self.sources})
        stages={g['stage'] for g in d['stageAggregates']}
        self.assertTrue({'startup-config','archive-read-and-weather','geography-and-policy-setup','initial-prefetch','source-processing','lifecycle-and-projection','canonical-revision','archive-write','snapshot-and-state-write'}<=stages)
        snapshot=pipeline.load(self.data/'snapshot.json');self.assertNotIn('diagnostics',snapshot)
        unsigned=dict(snapshot);publication=unsigned.pop('publication')
        self.assertEqual(publication['revision'],pipeline.publication_revision(unsigned))
        self.assertNotIn('diagnostics',(self.data/'collector-state.json').read_text())
        self.assertEqual(len(audit['sources']),5)

    def test_pipeline_prefetch_keeps_four_workers_and_finishes_before_children(self):
        import public_official
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=self.policies[:4]))
        parents={p['sourceId'] for p in self.policies[:4]}
        barrier=threading.Barrier(4); lock=threading.Lock(); finished=set(); active=peak=0
        def concurrent_fetch(policy,previous,now,fixture=None):
            nonlocal active,peak
            if policy['sourceId'] in parents:
                with lock:active+=1;peak=max(peak,active)
                barrier.wait(timeout=3)
                with lock:active-=1;finished.add(policy['sourceId'])
            else:
                self.assertEqual(finished,parents)
            return response(policy,previous,now,fixture)
        with patch.object(pipeline,'fetch',concurrent_fetch),patch.object(public_official,'collect_official',self.adapter):
            audit=pipeline.run(self.root,NOW,diagnostics=Clock().diagnostics())
        self.assertEqual(peak,4)
        self.assertEqual(audit['diagnostics']['requestCalls'],12)
        self.assertEqual(len(audit['sources']),4)

    def test_refresh_stage_failure_diagnostics_do_not_promote_partial_child(self):
        # Establish a fully valid prior publication first.
        import public_official
        with patch.object(pipeline,'fetch',response),patch.object(public_official,'collect_official',self.adapter):
            pipeline.run(self.root,NOW,diagnostics=Clock().diagnostics())
        before={str(p.relative_to(self.data)):p.read_bytes()for p in self.data.rglob('*')if p.is_file() and p.name!='refresh-status.json'}
        def timeout(command,**kwargs):
            self.assertEqual(kwargs['timeout'],480)
            stage=Path(command[command.index('--root')+1]);(stage/'docs/data/snapshot.json').write_text('partial')
            raise subprocess.TimeoutExpired(command,480)
        result=refresh.refresh(self.root,NOW+dt.timedelta(minutes=15),runner=timeout,environment={},diagnostics=Clock().diagnostics())
        self.assertEqual(result['outcome'],'failed')
        self.assertNotIn('diagnostics',result)  # no stale child timings from the copied prior status
        stages={g['stage']:g for g in result['wrapperDiagnostics']['stageAggregates']}
        self.assertEqual(stages['collector-child']['outcomes'],{'failed':1})
        self.assertNotIn('promotion',stages);self.assertNotIn('candidate-validation',stages)
        self.assertEqual(before,{str(p.relative_to(self.data)):p.read_bytes()for p in self.data.rglob('*')if p.is_file() and p.name!='refresh-status.json'})

    def test_refresh_success_times_validation_and_promotion_without_redating_cycle(self):
        import public_official
        with patch.object(pipeline,'fetch',response),patch.object(public_official,'collect_official',self.adapter):
            pipeline.run(self.root,NOW,diagnostics=Clock().diagnostics())
        later=NOW+dt.timedelta(minutes=15)
        def child(command,**kwargs):
            stage=Path(command[command.index('--root')+1])
            with patch.object(pipeline,'fetch',response),patch.object(public_official,'collect_official',self.adapter):
                pipeline.run(stage,later,diagnostics=Clock().diagnostics())
        result=refresh.refresh(self.root,later,runner=child,environment={},diagnostics=Clock().diagnostics())
        self.assertEqual(result['outcome'],'complete',result.get('errors'))
        self.assertEqual(result['generatedAt'],pipeline.stamp(later))
        self.assertEqual(result['completedAt'],pipeline.stamp(later))
        self.assertEqual(result['diagnostics']['requestCalls'],15)
        self.assertEqual({g['stage'] for g in result['wrapperDiagnostics']['stageAggregates']}, {'transaction-copy','collector-child','candidate-validation','promotion'})
        self.assertTrue(all(g['outcomes']=={'complete':1} for g in result['wrapperDiagnostics']['stageAggregates']))


if __name__=='__main__':unittest.main()
