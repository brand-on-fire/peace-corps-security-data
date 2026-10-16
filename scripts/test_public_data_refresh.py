"""Offline transaction, lossless evidence, and workflow-provenance checks."""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
if SCRIPTS.name == 'scripts' and not (SCRIPTS/'public_data_refresh.py').exists():
    SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import public_data_pipeline as pipeline
import public_data_refresh as refresh


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root/'docs/data'
        self.now = dt.datetime(2026,10,5,8,tzinfo=dt.timezone.utc)
        self.country = dict(code='GH',status='active',reviewDue='2026-11-04')
        pipeline.save(self.root/'config/countries.json',[self.country])
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=[]))
        self.row = dict(id='one',countryCode='GH',historical=False,occurredAt='2026-10-05T01:00:00Z',
                        updatedAt='2026-10-05T01:00:00.685Z', firstSeenAt='2026-10-05T02:00:00Z',
                        reports=[dict(contentHash='original-evidence')])
        self.write_candidate(self.root, '2026-10-05T02:00:00Z')
        self.original = (self.data/'snapshot.json').read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def write_candidate(self, root, when, rows=None, sources=None):
        rows = [self.row] if rows is None else rows
        data = root/'docs/data'
        pipeline.write_archive(data,{r['id']:r for r in rows},{'GH':self.country})
        snapshot = dict(generatedAt=when,countries=[self.country],incidents=rows,archiveTotal=len(rows),history={'recordCount':len(rows),'from':'2025-10-05T00:00:00Z'})
        snapshot['publication'] = dict(activeCountryCodes=['GH'],revision=pipeline.publication_revision(snapshot))
        pipeline.save(data/'snapshot.json',snapshot)
        pipeline.save(data/'refresh-status.json',dict(generatedAt=when,sources=sources or [],requested=len(sources or [])))

    def fake_runner(self, command, **kwargs):
        self.assertEqual(kwargs['timeout'],480)
        self.assertNotIn('GITHUB_TOKEN',kwargs['env'])
        stage = Path(command[command.index('--root')+1])
        self.write_candidate(stage,pipeline.stamp(self.now),sources=[dict(sourceId='official',status='healthy')])

    def test_complete_candidate_replaces_publication_and_records_trigger(self):
        env = dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=refresh.REPOSITORY,GITHUB_RUN_ID='123',
                   GITHUB_RUN_ATTEMPT='1',GITHUB_SHA='a'*40,GITHUB_EVENT_NAME='schedule',GITHUB_TOKEN='not-exposed')
        result = refresh.refresh(self.root,self.now,runner=self.fake_runner,environment=env)
        self.assertEqual(result['outcome'],'complete')
        self.assertEqual(result['lastScheduledRun']['id'],'123')
        self.assertEqual(result['publicationGeneratedAt'],pipeline.stamp(self.now))
        self.assertEqual(pipeline.load(self.data/'snapshot.json')['incidents'],[self.row])
        self.assertNotIn('not-exposed',json.dumps(result))
        self.assertFalse((self.data.parent/'.data-previous').exists())

    def test_timeout_after_partial_stage_write_retains_all_baseline_data(self):
        def timeout(command, **kwargs):
            stage = Path(command[command.index('--root')+1])
            (stage/'docs/data/snapshot.json').write_text('partial invalid data')
            raise subprocess.TimeoutExpired(command,480)
        result = refresh.refresh(self.root,self.now,runner=timeout,environment={})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual((self.data/'snapshot.json').read_bytes(),self.original)
        self.assertIn('eight-minute',result['errors'][0])
        self.assertNotIn(str(self.root),json.dumps(result))

    def test_all_sources_unavailable_retains_last_good_publication(self):
        def unavailable(command, **kwargs):
            stage = Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(self.now),sources=[dict(sourceId='official',status='unavailable')])
        result = refresh.refresh(self.root,self.now,runner=unavailable,environment={})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['sourceCounts']['failed'],1)
        self.assertEqual((self.data/'snapshot.json').read_bytes(),self.original)

    def test_one_source_outage_keeps_complete_successful_transaction_visible(self):
        def mixed(command, **kwargs):
            stage = Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(self.now),sources=[dict(sourceId='a',status='healthy'),dict(sourceId='b',status='unavailable')])
        result = refresh.refresh(self.root,self.now,runner=mixed,environment={})
        self.assertEqual(result['outcome'],'degraded')
        self.assertEqual(result['sourceCounts'],dict(attempted=2,healthy=1,failed=1))
        self.assertNotEqual((self.data/'snapshot.json').read_bytes(),self.original)

    def test_validation_rejects_archive_loss_without_promoting_any_file(self):
        def lost(command, **kwargs):
            stage = Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(self.now),rows=[])
        result = refresh.refresh(self.root,self.now,runner=lost,environment={})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual((self.data/'snapshot.json').read_bytes(),self.original)

    def test_revision_keeps_original_report_or_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            import shutil
            shutil.copytree(self.root/'config',stage/'config')
            row = dict(self.row,reports=[dict(contentHash='updated-evidence')])
            self.write_candidate(stage,pipeline.stamp(self.now),rows=[row])
            with self.assertRaisesRegex(ValueError,'original source evidence'):
                refresh.validate_candidate(stage,self.data)
            row['revisions']=[dict(previousReports=self.row['reports'])]
            self.write_candidate(stage,pipeline.stamp(self.now),rows=[row])
            self.assertEqual(refresh.validate_candidate(stage,self.data)['incidents'],[row])

    def test_subsecond_rollback_or_first_seen_change_cannot_publish(self):
        for field,value in [('updatedAt','2026-10-05T01:00:00Z'),('firstSeenAt','2026-10-05T02:00:01Z')]:
            with self.subTest(field=field):
                def regressed(command, **kwargs):
                    stage=Path(command[command.index('--root')+1])
                    self.write_candidate(stage,pipeline.stamp(self.now),rows=[dict(self.row,**{field:value})],sources=[dict(sourceId='official',status='healthy')])
                result=refresh.refresh(self.root,self.now,runner=regressed,environment={})
                self.assertEqual(result['outcome'],'failed')
                self.assertEqual((self.data/'snapshot.json').read_bytes(),self.original)

    def test_prior_revision_history_cannot_be_lost(self):
        self.row['revisions']=[dict(at='2026-10-05T02:00:00Z',note='Prior evidence',previousReports=[dict(contentHash='earliest')])]
        self.write_candidate(self.root,'2026-10-05T02:00:00Z')
        original=(self.data/'snapshot.json').read_bytes()
        def lost(command, **kwargs):
            stage=Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(self.now),rows=[{k:v for k,v in self.row.items() if k!='revisions'}],sources=[dict(sourceId='official',status='healthy')])
        result=refresh.refresh(self.root,self.now,runner=lost,environment={})
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual((self.data/'snapshot.json').read_bytes(),original)

    def test_publication_time_must_advance_when_content_changes(self):
        for when in ['2026-10-05T01:59:59Z','2026-10-05T02:00:00Z']:
            with self.subTest(when=when),tempfile.TemporaryDirectory() as temporary:
                stage=Path(temporary)
                import shutil
                shutil.copytree(self.root/'config',stage/'config')
                changed=dict(self.row,state='expired')
                self.write_candidate(stage,when,rows=[changed])
                with self.assertRaisesRegex(ValueError,'publication timestamp'):
                    refresh.validate_candidate(stage,self.data)

    def test_failed_second_directory_rename_restores_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/'candidate'
            source.mkdir();(source/'snapshot.json').write_text('new')
            original_rename = Path.rename
            def rename(path, target):
                if path.name=='.data-next':
                    raise OSError('simulated disk error')
                return original_rename(path,target)
            with patch.object(Path,'rename',rename), self.assertRaises(OSError):
                refresh.promote(source,self.data)
        self.assertEqual((self.data/'snapshot.json').read_bytes(),self.original)

    def test_dispatch_does_not_become_natural_schedule_proof(self):
        common = dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=refresh.REPOSITORY,GITHUB_RUN_ID='123',
                      GITHUB_RUN_ATTEMPT='1',GITHUB_SHA='a'*40,GITHUB_EVENT_NAME='workflow_dispatch')
        result = refresh.refresh(self.root,self.now,runner=self.fake_runner,environment=common)
        self.assertIsNone(result['lastScheduledRun'])
        self.assertEqual(result['run']['event'],'workflow_dispatch')
        with self.assertRaisesRegex(ValueError,'repository'):
            refresh.provenance(dict(common,GITHUB_REPOSITORY='other/private'),pipeline.stamp(self.now))

    def test_failed_promotion_does_not_claim_candidate_was_published(self):
        with patch.object(refresh, 'promote', side_effect=OSError('simulated storage failure')):
            result = refresh.refresh(self.root,self.now,runner=self.fake_runner,environment={})
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['publicationGeneratedAt'], '2026-10-05T02:00:00Z')
        self.assertIsNone(result['lastSuccessfulCollectionAt'])
        self.assertEqual((self.data/'snapshot.json').read_bytes(), self.original)

    def test_health_counts_retained_source_outage_not_attempted_this_run(self):
        pipeline.save(self.root/'config/collection-policy.json', dict(sources=[dict(sourceId='old-news', mode='discovery', cadenceMinutes=360)]))
        def run(command, **kwargs):
            stage = Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(self.now),sources=[dict(sourceId='official',status='healthy')])
            snapshot=pipeline.load(stage/'docs/data/snapshot.json')
            snapshot['coverage']=[dict(sourceId='old-news',status='unavailable',lastSuccessAt='2026-10-04T00:00:00Z',lastAttemptAt='2026-10-05T04:00:00Z')]
            publication=snapshot.pop('publication')
            publication['revision']=pipeline.publication_revision(snapshot)
            snapshot['publication']=publication
            pipeline.save(stage/'docs/data/snapshot.json',snapshot)
        result=refresh.refresh(self.root,self.now,runner=run,environment={})
        self.assertEqual(result['sourceCounts']['failed'],0)
        self.assertEqual(result['outcome'],'degraded')
        self.assertEqual(result['sourceHealth']['counts']['overdue'],1)
        self.assertEqual(result['sourceHealth']['counts']['unavailable'],1)

    def test_window_provenance_distinguishes_trigger_and_actual_collector_code(self):
        env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=refresh.REPOSITORY,GITHUB_RUN_ID='123',
                 GITHUB_RUN_ATTEMPT='1',GITHUB_SHA='a'*40,GITHUB_EVENT_NAME='schedule',
                 COLLECTOR_CODE_SHA='b'*40,COLLECTOR_CYCLE='2')
        result=refresh.provenance(env,pipeline.stamp(self.now))
        self.assertEqual(result['headSha'],'a'*40)
        self.assertEqual(result['collectorHeadSha'],'b'*40)
        self.assertEqual(result['cycle'],2)
        for bad in [dict(COLLECTOR_CYCLE='0'),dict(COLLECTOR_CYCLE='21'),dict(COLLECTOR_CODE_SHA='invalid')]:
            with self.assertRaisesRegex(ValueError,'window provenance'):
                refresh.provenance(dict(env,**bad),pipeline.stamp(self.now))

    def test_child_generation_after_wrapper_start_is_not_a_clock_error(self):
        child_at=self.now+dt.timedelta(seconds=45)
        finished=self.now+dt.timedelta(seconds=100)
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=[dict(sourceId='official',mode='official',cadenceMinutes=15)]))
        def later_child(command, **kwargs):
            stage=Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(child_at),sources=[dict(sourceId='official',status='healthy',
                lastAttemptAt=pipeline.stamp(child_at),lastSuccessAt=pipeline.stamp(child_at))])
        result=refresh.refresh(self.root,self.now,runner=later_child,environment={},clock=lambda:finished)
        self.assertEqual(result['outcome'],'complete')
        self.assertEqual(result['generatedAt'],pipeline.stamp(child_at))
        self.assertEqual(result['completedAt'],pipeline.stamp(finished))
        self.assertEqual(result['sourceHealth']['counts']['clockErrors'],0)
        self.assertEqual(result['sourceHealth']['sources'][0]['successfulCheckAgeSeconds'],55)
        self.assertEqual(result['collectionTiming']['windowEnd'],pipeline.stamp(finished))

    def test_replay_without_clock_uses_later_child_as_evaluation_lower_bound(self):
        child_at=self.now+dt.timedelta(seconds=45)
        def later_child(command, **kwargs):
            stage=Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(child_at),sources=[dict(sourceId='official',status='healthy')])
        result=refresh.refresh(self.root,self.now,runner=later_child,environment={})
        self.assertEqual(result['outcome'],'complete')
        self.assertEqual(result['sourceHealth']['evaluatedAt'],pipeline.stamp(child_at))

    def test_failed_collection_dates_diagnostic_at_completion_and_retains_publication(self):
        finished=self.now+dt.timedelta(seconds=480)
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command,480)
        result=refresh.refresh(self.root,self.now,runner=timeout,environment={},clock=lambda:finished)
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['run']['startedAt'],pipeline.stamp(self.now))
        self.assertEqual(result['generatedAt'],pipeline.stamp(finished))
        self.assertEqual(result['publicationGeneratedAt'],'2026-10-05T02:00:00Z')
        self.assertEqual(result['collectionTiming']['observations'][-1]['at'],pipeline.stamp(finished))
        self.assertEqual((self.data/'snapshot.json').read_bytes(),self.original)

    def test_all_sources_failure_with_later_child_retains_usable_diagnostics(self):
        child_at=self.now+dt.timedelta(seconds=45)
        finished=self.now+dt.timedelta(seconds=90)
        def unavailable(command, **kwargs):
            stage=Path(command[command.index('--root')+1])
            self.write_candidate(stage,pipeline.stamp(child_at),sources=[dict(sourceId='official',status='unavailable',lastAttemptAt=pipeline.stamp(child_at))])
        result=refresh.refresh(self.root,self.now,runner=unavailable,environment={},clock=lambda:finished)
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['generatedAt'],pipeline.stamp(finished))
        self.assertEqual(result['publicationGeneratedAt'],'2026-10-05T02:00:00Z')
        self.assertIn('collectionTiming',result)
        self.assertEqual(len(result['errors']),1)

    def test_real_offline_child_generates_after_wrapper_start(self):
        import shutil
        self.write_candidate(self.root,'2026-10-05T02:00:00Z',rows=[])
        snapshot=pipeline.load(self.data/'snapshot.json');snapshot['coverage']=[]
        pipeline.save(self.data/'snapshot.json',snapshot)
        for name,value in [('sources.json',[]),('country-iso3.json',{}),('admin-places.json',[]),('geo/index.json',[])]:
            pipeline.save(self.root/'config'/name,value)
        (self.root/'scripts').mkdir()
        for script in SCRIPTS.glob('*.py'):
            shutil.copy2(script,self.root/'scripts'/script.name)
        fixtures=self.root/'fixtures';fixtures.mkdir()
        started=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)
        result=refresh.refresh(self.root,started,fixtures=fixtures,environment={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1'},
                               clock=lambda:dt.datetime.now(dt.timezone.utc))
        self.assertEqual(result['outcome'],'complete',result['errors'])
        self.assertGreater(result['generatedAt'],pipeline.stamp(started))
        self.assertGreaterEqual(result['completedAt'],result['generatedAt'])
        self.assertEqual(result['sourceHealth']['counts']['clockErrors'],0)
        self.assertIn('collectionTiming',result)


if __name__=='__main__':
    unittest.main()
