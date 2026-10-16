"""Offline evidence-completeness and false-success regression tests."""
import datetime as dt
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import observe_public_collector as observer


class PaginationRecorder:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.evidence = {}

    def get(self, key, url):
        data, headers = next(self.responses)
        self.evidence[key] = dict(headers=headers)
        return data


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = '2026-10-06T01:00:00Z'
        self.evidence = {}
        self.main = 'a'*40

    def tearDown(self):
        self.temp.cleanup()

    def body(self, key, data):
        raw = observer.canonical(data)
        sha = observer.digest(raw)
        path = self.root/'bodies'/f'{sha}.gz'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(gzip.compress(raw, mtime=0))
        self.evidence[key] = dict(url=observer.API+'/fixture/'+key, retrievedAt=self.now,
                                 bytes=len(raw), sha256=sha, headers={}, status=200)

    def fixture(self, event='schedule', outcome='complete', conclusion='success', head=None, publication=True):
        head = head or observer.FIXED_COMMIT
        run = dict(id=1, event=event, created_at='2026-10-06T00:26:00Z', updated_at='2026-10-06T00:28:00Z',
                   head_sha=head, run_attempt=1, status='completed', conclusion=conclusion)
        provenance = dict(id='1', event=event, headSha=head, attempt=1)
        self.body('runs/0', dict(total_count=1, workflow_runs=[run]))
        self.body('ancestry/'+head, dict(status='identical' if head==observer.FIXED_COMMIT else 'behind', behind_by=0 if head==observer.FIXED_COMMIT else 1))
        self.body('main', dict(sha=self.main))
        self.body('workflow', dict(state='active'))
        self.body('fixedCommit', dict(commit=dict(committer=dict(date='2026-10-06T00:05:00Z'))))
        status = dict(generatedAt='2026-10-06T00:27:00Z', publicationGeneratedAt='2026-10-06T00:27:00Z',
                      run=provenance, outcome=outcome, sources=[dict(sourceId='one', status='healthy')])
        if not publication:
            status['run'] = dict(provenance, id='999')
        self.body('latestStatus', status)
        self.body('latestSnapshot', dict(generatedAt=status['publicationGeneratedAt'],
            publication=dict(generatedAt=status['publicationGeneratedAt'], revision='b'*64), incidents=[], archiveTotal=1,
            sources=[dict(id='one',url='https://example.org',cadenceMinutes=15)],coverage=status['sources']))
        return dict(schemaVersion=1, observedAt=self.now, windowStart='2026-09-28T01:00:00Z',
                    windowEnd=self.now, evidence=self.evidence, fixedCommit=observer.FIXED_COMMIT, outcome='complete')

    def test_reads_all_pages_and_refuses_missing_or_duplicate_rows(self):
        url = observer.API+'/actions/workflows/refresh-data.yml/runs?per_page=100'
        next_url = url+'&page=2'
        recorder = PaginationRecorder([(dict(total_count=2, workflow_runs=[dict(id=1)]), {'link':f'<{next_url}>; rel="next"'}),
                                       (dict(total_count=2, workflow_runs=[dict(id=2)]), {})])
        self.assertEqual(len(observer.paginate(recorder,'runs',url,'workflow_runs')),2)
        for ids in ([1], [1,1]):
            recorder = PaginationRecorder([(dict(total_count=2,workflow_runs=[dict(id=x) for x in ids]),{})])
            with self.assertRaisesRegex(ValueError, 'Incomplete or duplicated'):
                observer.paginate(recorder,'runs',url,'workflow_runs')

    def test_refuses_api_search_cap_and_pagination_escape(self):
        url = observer.API+'/actions/workflows/refresh-data.yml/runs?per_page=100'
        recorder = PaginationRecorder([(dict(total_count=1001,workflow_runs=[]),{})])
        with self.assertRaisesRegex(ValueError,'1,000'):
            observer.paginate(recorder,'runs',url,'workflow_runs')
        for next_url in ('https://example.org/private?page=2', url.replace('per_page=100','per_page=1')+'&page=2'):
            with self.assertRaises(ValueError):
                observer.next_page({'link':f'<{next_url}>; rel="next"'},url)

    def test_push_and_manual_runs_never_count_as_natural(self):
        for event in ('push','workflow_dispatch'):
            result = observer.summarize_observation(self.root,self.fixture(event=event))
            self.assertEqual(result['scheduleRuns'],[])
            self.assertEqual(result['naturalPublicationGaps']['count'],0)

    def test_failed_or_unmatched_status_is_not_publication_proof(self):
        for kwargs in (dict(outcome='failed'),dict(conclusion='failure'),dict(publication=False)):
            result = observer.summarize_observation(self.root,self.fixture(**kwargs))
            self.assertIsNone(result['scheduleRuns'][0]['publication'])

    def test_old_code_schedule_not_counted_as_fixed_code(self):
        result = observer.summarize_observation(self.root,self.fixture(head='c'*40))
        self.assertEqual(result['naturalPublicationGaps']['count'],1)
        self.assertEqual(result['fixedNaturalPublicationGaps']['count'],0)

    def test_degraded_run_can_publish_but_not_claim_every_source_healthy(self):
        row = self.fixture(outcome='degraded')
        status=json.loads(observer.read_body(self.root,self.evidence['latestStatus']))
        status['sources'].append(dict(sourceId='two',status='unavailable',message='HTTP 403'))
        self.body('latestStatus',status)
        snapshot=json.loads(observer.read_body(self.root,self.evidence['latestSnapshot']))
        snapshot['coverage']=status['sources']
        snapshot['sources'].append(dict(id='two',url='https://example.net',cadenceMinutes=360))
        self.body('latestSnapshot',snapshot)
        result=observer.summarize_observation(self.root,row)
        self.assertEqual(result['naturalPublicationGaps']['count'],1)
        self.assertEqual(result['latestPublication']['failedSources'][0]['sourceId'],'two')

    def test_known_gap_and_repeated_timestamp_are_not_hidden(self):
        result=observer.gap_stats(['2026-10-05T16:23:38Z','2026-10-05T22:48:36Z','2026-10-05T22:48:36Z'])
        self.assertEqual(result['maxSeconds'],23098)
        self.assertEqual(result['over15Minutes'],1)
        self.assertEqual(result['count'],2)

    def test_ledger_and_body_tampering_are_rejected(self):
        row=self.fixture()
        observer.append(self.root,row)
        self.assertEqual(len(observer.read_ledger(self.root)),1)
        path=self.root/'observations.jsonl'
        text=path.read_text()
        path.write_text(text.replace('"outcome":"complete"','"outcome":"failed"'))
        with self.assertRaisesRegex(ValueError,'hash chain'):
            observer.read_ledger(self.root)
        path.write_text(text)
        receipt=self.evidence['main']
        (self.root/'bodies'/f'{receipt["sha256"]}.gz').write_bytes(gzip.compress(b'corrupt'))
        with self.assertRaisesRegex(ValueError,'corrupt'):
            observer.read_ledger(self.root)

    def test_elapsed_week_with_sparse_observations_is_not_success(self):
        row=self.fixture()
        observer.append(self.root,row)
        row=dict(row,observedAt='2026-10-13T01:00:00Z',windowEnd='2026-10-13T01:00:00Z')
        observer.append(self.root,row)
        report=observer.report(self.root)
        self.assertFalse(report['sevenDayObservationComplete'])
        self.assertFalse(report['quarterHourDeliveryDemonstrated'])
        self.assertGreater(report['maxObservationGapSeconds'],3600)

    def test_failed_read_preserves_previous_complete_observation(self):
        observer.append(self.root,self.fixture())
        def outage(url):
            raise TimeoutError('Public endpoint did not respond')
        result=observer.collect(self.root,dt.datetime(2026,10,6,2,tzinfo=dt.timezone.utc),fetcher=outage)
        self.assertEqual(result['outcome'],'failed')
        report=observer.report(self.root)
        self.assertEqual(report['latestComplete']['mainCommit'],self.main)
        self.assertEqual(report['failedObservations'],1)
        self.assertFalse(report['quarterHourDeliveryDemonstrated'])

    def test_pinned_snapshot_and_status_must_agree(self):
        row=self.fixture()
        snapshot=json.loads(observer.read_body(self.root,self.evidence['latestSnapshot']))
        snapshot['generatedAt']='2026-10-06T00:28:00Z'
        self.body('latestSnapshot',snapshot)
        with self.assertRaisesRegex(ValueError,'disagree'):
            observer.summarize_observation(self.root,row)

    def test_window_cycles_are_counted_separately_despite_non_green_run(self):
        row = self.fixture(outcome='degraded', conclusion='failure')
        status = json.loads(observer.read_body(self.root, self.evidence['latestStatus']))
        code = 'd'*40
        self.body('ancestry/'+code, dict(status='ahead', behind_by=0))
        status['run'].update(cycle=1, collectorHeadSha=code)
        status['generatedAt']=status['publicationGeneratedAt']='2026-10-06T00:26:30Z'
        self.body('status/'+'e'*40, status)
        status['run']['cycle']=2
        status['generatedAt']=status['publicationGeneratedAt']='2026-10-06T00:27:00Z'
        self.body('latestStatus', status)
        self.body('status/'+'f'*40, status)
        result = observer.summarize_observation(self.root, row)
        self.assertEqual(result['naturalPublicationGaps']['count'], 2)
        self.assertEqual(result['fixedNaturalPublicationGaps']['count'], 2)
        self.assertEqual([p['cycle'] for p in result['scheduleRuns'][0]['publications']], [1, 2])
        self.assertEqual(result['scheduleRuns'][0]['publication']['commit'], 'f'*40)

    def test_in_progress_window_accepts_only_matching_committed_cycle(self):
        row = self.fixture(outcome='degraded', conclusion=None)
        runs = json.loads(observer.read_body(self.root, self.evidence['runs/0']))
        runs['workflow_runs'][0].update(status='in_progress', updated_at='2026-10-06T00:26:00Z')
        self.body('runs/0', runs)
        status = json.loads(observer.read_body(self.root, self.evidence['latestStatus']))
        status['run'].update(cycle=1, collectorHeadSha=observer.FIXED_COMMIT)
        self.body('latestStatus', status)
        result = observer.summarize_observation(self.root, row)
        self.assertEqual(result['naturalPublicationGaps']['count'], 1)
        status['run']['attempt']=2
        self.body('latestStatus', status)
        self.assertEqual(observer.summarize_observation(self.root, row)['naturalPublicationGaps']['count'], 0)

    def test_invalid_window_provenance_does_not_relax_failed_run_checks(self):
        for cycle, sha in [(0, observer.FIXED_COMMIT), (21, observer.FIXED_COMMIT),
                           (True, observer.FIXED_COMMIT), (1, 'not-a-commit')]:
            row=self.fixture(outcome='degraded', conclusion='failure')
            status=json.loads(observer.read_body(self.root,self.evidence['latestStatus']))
            status['run'].update(cycle=cycle, collectorHeadSha=sha)
            self.body('latestStatus',status)
            self.assertEqual(observer.summarize_observation(self.root,row)['naturalPublicationGaps']['count'], 0)


if __name__=='__main__':
    unittest.main()
