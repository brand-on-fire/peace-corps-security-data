"""Offline opt-in, state atomicity and archive validation for Fiji transport."""
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_data_pipeline as pipeline
import public_data_refresh as refresh
import public_official as official
import public_transport as transport
from test_public_transport import document, Fetch

NOW = '2026-10-16T00:00:00Z'
COUNTRY = dict(code='FJ', name='Fiji', region='Pacific', lat=-17.8, lon=178.0,
    status='active', evidenceUrl='https://www.peacecorps.gov/fiji/',
    evidenceText='Synthetic reviewed-country fixture.', evidenceDate='2026-10-01',
    verifiedAt='2026-10-01T00:00:00Z', reviewDue='2026-11-15', languages=['en'])
SOURCE = dict(id=transport.SOURCE_ID, name='Fiji Roads Authority', url='https://www.fijiroads.org/',
    feedUrl=transport.endpoint(), countryCodes=['FJ'], kind='rss', tier='official',
    language='en', cadenceMinutes=60, enabled=False, access='public-feed',
    verifiedAt='2026-10-01T00:00:00Z', notes='Synthetic test source; no external requests.')
POLICY = dict(sourceId=transport.SOURCE_ID, adapter='fj-fra', mode='official', cadenceMinutes=60,
    historyFrom='2025-10-16T00:00:00Z', url=transport.endpoint())


def initialize(root, enabled=True):
    source = dict(SOURCE, enabled=enabled)
    for name, value in [('countries.json',[COUNTRY]),('sources.json',[source]),
        ('collection-policy.json',{'sources':[POLICY] if enabled else []}),
        ('staged-transport-policy.json',{'enabled':False,'source':SOURCE,'policy':POLICY}),
        ('geo/index.json',[]),('country-iso3.json',{'FJI':'FJ'}),('admin-places.json',[])]:
        pipeline.save(root/'config'/name,value)
    data=root/'docs/data'
    snapshot=dict(schemaVersion=1,mode='live',generatedAt='2026-10-15T00:00:00Z',
        countries=[COUNTRY],sources=[source],incidents=[],coverage=[],archiveTotal=0,
        history=dict(recordCount=0,from_='unused',to='2026-10-15T00:00:00Z',sources=[source['id']],notes=[]))
    snapshot['history'].pop('from_');snapshot['history']['from']='2025-10-16T00:00:00Z'
    pipeline.save(data/'snapshot.json',snapshot)
    pipeline.write_archive(data,{}, {'FJ':COUNTRY})
    return data


def fixture_fetch(rows, failures=None):
    client=Fetch(rows,overrides=failures)
    def fetch(policy,previous,now,fixtures):
        result, raw=client(policy)
        result.update(sourceId=policy['sourceId'],url=policy['url'],lastAttemptAt=now)
        return result,raw
    fetch.client=client
    return fetch


class TransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'collector'
        self.data=initialize(self.root)

    def tearDown(self):self.temp.cleanup()

    def run_pipeline(self, rows=None, when=NOW, fetch=None):
        with patch.object(pipeline,'fetch',fetch or fixture_fetch(rows or [document()])):
            return pipeline.run(self.root,dt.datetime.fromisoformat(when.replace('Z','+00:00')))

    def test_disabled_stage_produces_no_request_and_disabled_coverage(self):
        initialize(self.root,enabled=False)
        with patch.object(pipeline,'fetch',side_effect=AssertionError('Disabled source requested')):
            result=pipeline.run(self.root,dt.datetime.fromisoformat(NOW))
        self.assertEqual(result['requested'],0)
        self.assertFalse(pipeline.load(self.data/'snapshot.json')['sources'][0]['enabled'])
        self.assertEqual(pipeline.load(self.data/'snapshot.json')['coverage'][0]['status'],'disabled')

    def test_opt_in_flows_through_canonical_archive_and_refresh_validation(self):
        baseline=Path(self.temp.name)/'baseline';shutil.copytree(self.data,baseline)
        result=self.run_pipeline()
        self.assertEqual(result['sources'][0]['status'],'healthy')
        snapshot=refresh.validate_candidate(self.root,baseline)
        archived=pipeline.read_archive(self.data,{'FJ':COUNTRY})
        self.assertEqual(len(archived),1)
        row=next(iter(archived.values()))
        self.assertEqual(snapshot['incidents'],[pipeline.public_snapshot_record(row)])
        self.assertEqual(row['occurredAt'],'2026-10-09T09:00:00Z')
        self.assertEqual((row['state'],row['category'],row['severity']),('expired','transport','unknown'))
        state=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
        self.assertTrue(state['transportBaseline']['complete'])
        self.assertTrue(state['transportLiveCaughtUp'])

    def test_rejected_record_does_not_save_new_transport_cursor(self):
        with patch.object(pipeline,'PART_LIMIT',100):
            result=self.run_pipeline()
        self.assertEqual(result['sources'][0]['status'],'unavailable')
        state=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
        self.assertNotIn('transportBaseline',state);self.assertNotIn('transportPendingPostIds',state)
        self.assertFalse(pipeline.read_archive(self.data,{'FJ':COUNTRY}))

    def test_resume_cursor_only_advances_after_complete_next_batch(self):
        rows=[document(i,date_gmt=f'2026-10-0{i}T00:00:00',modified_gmt=f'2026-10-0{i}T00:00:00') for i in range(1,6)]
        with patch.object(transport,'PAGE_SIZE',2),patch.object(transport,'BASELINE_PAGES',1):
            self.run_pipeline(rows)
            before=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
            self.assertEqual(before['transportBaseline']['nextPage'],2)
            records_before=pipeline.read_archive(self.data,{'FJ':COUNTRY})
            failed=self.run_pipeline(rows,when='2026-10-16T01:00:00Z',fetch=fixture_fetch(rows,{transport.endpoint('date',2):False}))
            self.assertEqual(failed['sources'][0]['status'],'unavailable')
            state=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
            self.assertEqual(state['transportBaseline'],before['transportBaseline'])
            self.assertEqual(state['lastSuccessAt'],before['lastSuccessAt'])
            self.assertEqual(pipeline.read_archive(self.data,{'FJ':COUNTRY}),records_before)
            result=self.run_pipeline(rows,when='2026-10-16T02:00:00Z')
            self.assertEqual(result['sources'][0]['status'],'healthy')
            state=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
            self.assertEqual(state['transportBaseline']['nextPage'],3)
            self.assertFalse(state['transportBaseline']['complete'])

    def test_listing_conditionals_are_removed_so_pending_and_history_run(self):
        previous=dict(url=transport.endpoint(),etag='old-etag',lastModified='old-date',lastSuccessAt=NOW)
        self.assertEqual(official.request_state(POLICY,previous),dict(url=transport.endpoint(),lastSuccessAt=NOW))
        self.assertIn('etag',previous)

    def test_older_exact_post_withdrawal_fails_without_certifying_old_assertion(self):
        self.run_pipeline()
        before=pipeline.read_archive(self.data,{'FJ':COUNTRY})
        state_before=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
        rollback=document(modified_gmt='2026-10-05T04:52:09',
            content={'rendered':'<p>The earlier closure notice is withdrawn.</p>','protected':False})
        result=self.run_pipeline([rollback],when='2026-10-16T01:00:00Z')
        self.assertEqual(result['sources'][0]['status'],'unavailable')
        self.assertIn('provider modification moved backwards',result['sources'][0]['message'])
        self.assertEqual(pipeline.read_archive(self.data,{'FJ':COUNTRY}),before)
        state=pipeline.load(self.data/'collector-state.json')[transport.SOURCE_ID]
        for key in ('lastSuccessAt','transportLiveThrough','transportBaseline','transportRecheckCursor'):
            self.assertEqual(state[key],state_before[key])


if __name__=='__main__':unittest.main()
