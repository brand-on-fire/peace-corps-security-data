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
        snapshot = dict(generatedAt=when,countries=[self.country],incidents=rows,archiveTotal=len(rows),history=dict(recordCount=len(rows)))
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


if __name__=='__main__':
    unittest.main()
