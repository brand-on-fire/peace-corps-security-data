"""Common importer checks: complete evidence, no accidental duplicates, rollback."""
import copy
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import import_public_data_batches as importer
p=importer.pipeline

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=dt.datetime(2026,10,5,tzinfo=dt.timezone.utc)
        self.country=dict(code='JM',name='Jamaica',region='Caribbean',status='active',reviewDue='2026-11-04')
        self.source=dict(id='test',url='https://example.org/',countryCodes=['JM'])
        self.row=dict(id='one',title='Report',summary='Complete evidence summary',countryCode='JM',region='Area',category='crime',severity='unknown',
            evidenceStatus='reported',state='reported',occurredAt='2026-10-01T00:00:00Z',updatedAt='2026-10-02T00:00:00Z',firstSeenAt='2026-10-03T00:00:00Z',
            location=dict(precision='area'),sourceIds=['test'],historical=False,aiStatus='not-requested',sourceMetadata={'recordId':'provider1'},reports=[dict(id='one-report',incidentId='one',sourceId='test',title='Report',url='https://example.org/a',language='en',publishedAt='2026-10-02T00:00:00Z',retrievedAt='2026-10-03T00:00:00Z',excerpt='',contentHash='a'*64,independentGroup='test')])
        p.save(self.root/'config/countries.json',[self.country]);p.save(self.root/'config/sources.json',[self.source]);p.save(self.root/'config/admin-places.json',[])
        p.write_archive(self.root/'docs/data',{}, {'JM':self.country})
        p.save(self.root/'docs/data/snapshot.json',dict(generatedAt='2026-10-04T00:00:00Z',mode='live',countries=[self.country],sources=[self.source],incidents=[],coverage=[],
            history={'from':'2021-10-05T00:00:00Z','to':'2026-10-04T00:00:00Z','sources':['test'],'notes':[],'recordCount':0},collector={'discoveryCount':0}))
        self.batch=self.root/'batch.json'
    def tearDown(self):self.tmp.cleanup()
    def run_batch(self,batch,apply=True):
        p.save(self.batch,batch);return importer.run(self.root,[self.batch],self.now,apply)
    def records(self):return p.read_archive(self.root/'docs/data',{'JM':self.country})
    def test_idempotent_import_and_full_evidence_roundtrip(self):
        first=self.run_batch({'incidents':[self.row]});before=self.records()
        second=self.run_batch({'incidents':[self.row]})
        self.assertEqual(first['newIncidents'],1);self.assertEqual(second['newIncidents'],0)
        self.assertEqual(second['changedIncidents'],0);self.assertEqual(before,self.records())
    def test_exact_provider_identity_consolidates_not_title(self):
        self.run_batch({'incidents':[self.row]});new=copy.deepcopy(self.row);new['id']='alias';new['reports'][0].update(id='alias-report',incidentId='alias',url='https://example.org/update')
        audit=self.run_batch({'incidents':[new]});self.assertEqual(audit['aliases'],{'alias':'one'});self.assertEqual(len(self.records()),1)
        unrelated=copy.deepcopy(new);unrelated['id']='two';unrelated['sourceMetadata']['recordId']='provider2';unrelated['reports'][0].update(id='two-report',incidentId='two')
        self.run_batch({'incidents':[unrelated]});self.assertEqual(len(self.records()),2)
    def test_correction_preserves_original_reports_and_dates(self):
        self.run_batch({'incidents':[self.row]})
        correction={'incidentId':'one','reason':'Source proves earlier day','patch':{'occurredAt':'2026-09-19T00:00:00Z'}}
        self.run_batch({'corrections':[correction]});row=self.records()['one'];self.assertEqual(row['firstSeenAt'],self.row['firstSeenAt'])
        self.assertEqual(row['revisions'][0]['previousReports'],self.row['reports'])
        self.assertEqual(row['sourceMetadata']['fieldRevisions'][0]['previousFields']['occurredAt'],self.row['occurredAt'])
        before=self.records();self.run_batch({'corrections':[correction]});self.assertEqual(before,self.records())
    def test_invalid_country_parent_and_future_are_rejected_without_mutation(self):
        baseline=(self.root/'docs/data/snapshot.json').read_bytes()
        for change in ({'countryCode':'XX'},{'admin1Code':'missing','admin1Name':'Bad'},{'occurredAt':'2027-01-01T00:00:00Z'}):
            row=copy.deepcopy(self.row);row.update(change)
            with self.assertRaises(ValueError):self.run_batch({'incidents':[row]})
            self.assertEqual((self.root/'docs/data/snapshot.json').read_bytes(),baseline)
    def test_unverified_new_source_cannot_replace_valid_publication(self):
        self.run_batch({'incidents':[self.row]})
        before_snapshot=(self.root/'docs/data/snapshot.json').read_bytes()
        before_registry=(self.root/'config/sources.json').read_bytes()
        before_records=self.records()
        for verified in (None,'','2027-01-01T00:00:00Z','2026-10-04T00:00:00'):
            source=dict(id='new-directory',url='https://official.example/',countryCodes=['JM'],enabled=False)
            if verified is not None:source['verifiedAt']=verified
            with self.assertRaises(ValueError):self.run_batch({'newSources':[source]})
            self.assertEqual((self.root/'docs/data/snapshot.json').read_bytes(),before_snapshot)
            self.assertEqual((self.root/'config/sources.json').read_bytes(),before_registry)
            self.assertEqual(self.records(),before_records)
    def test_report_update_keeps_previous_version_and_no_date_rollback(self):
        self.run_batch({'incidents':[self.row]});new=copy.deepcopy(self.row);new['updatedAt']='2026-10-01T00:00:00Z';new['reports'][0]['contentHash']='b'*64
        self.run_batch({'incidents':[new]});row=self.records()['one']
        self.assertEqual(row['updatedAt'],'2026-10-02T00:00:00Z');self.assertEqual(row['reports'][0]['contentHash'],'a'*64)
        self.assertEqual(row['revisions'][0]['previousReports'][0]['contentHash'],'b'*64)
        before=self.records();self.run_batch({'incidents':[new]});self.assertEqual(before,self.records())
    def test_snapshot_limit_rejects_instead_of_truncating(self):
        new=copy.deepcopy(self.row);new['summary']='x'*10_000_000
        with self.assertRaisesRegex(ValueError,'snapshot exceeds'):self.run_batch({'incidents':[new]})
        self.assertEqual(self.records(),{})
    def test_unknown_publication_is_only_allowed_for_declared_historical_ucdp(self):
        row=copy.deepcopy(self.row);row['occurredAt']='2025-10-10T00:00:00Z';row['reports'][0]['publishedAt']=''
        row['sourceMetadata']['publicationTimeUnknown']=True
        with self.assertRaises(ValueError):self.run_batch({'incidents':[row]})
        self.source['kind']='ucdp';p.save(self.root/'config/sources.json',[self.source])
        self.run_batch({'incidents':[row]});self.assertEqual(self.records()['one']['reports'][0]['publishedAt'],'')
    def test_correction_is_idempotent_with_same_url_different_report_id(self):
        self.run_batch({'incidents':[self.row]});report=copy.deepcopy(self.row['reports'][0]);report.update(id='new-id',contentHash='b'*64)
        correction={'incidentId':'one','reason':'Updated source','patch':{'summary':'Updated summary'},'appendReports':[report]}
        self.run_batch({'corrections':[correction]});before=self.records();self.now+=dt.timedelta(minutes=1)
        self.run_batch({'corrections':[correction]});self.assertEqual(before,self.records())
        self.assertEqual(len(before['one']['reports']),1)
    def test_correction_older_provider_cannot_roll_back_facts(self):
        self.run_batch({'incidents':[self.row]});report=copy.deepcopy(self.row['reports'][0]);report['contentHash']='b'*64
        correction={'incidentId':'one','reason':'Archive checked','patch':{'updatedAt':'2026-10-01T00:00:00Z','summary':'Older account'},'appendReports':[report]}
        self.run_batch({'corrections':[correction]});row=self.records()['one']
        self.assertEqual(row['summary'],self.row['summary']);self.assertEqual(row['updatedAt'],self.row['updatedAt'])
        self.assertEqual(row['revisions'][0]['previousReports'][0]['contentHash'],'b'*64)
    def test_withheld_spatial_assignment_still_requires_boundary_provenance(self):
        row=copy.deepcopy(self.row);row['location']={'precision':'point','lon':-77,'lat':18}
        row['adminAssignment']={'method':'strict_point_containment','confidence':'spatial_inference_only','geometryResolution':'provider_simplified','levels':{'ADM2':{'status':'ambiguous','candidateIds':['unassigned']}}}
        with self.assertRaisesRegex(ValueError,'boundary provenance missing'):self.run_batch({'incidents':[row]})
        row['adminAssignment']['levels']['ADM2'].update(layerId='test-layer',boundaryYearRepresented='2020',geometryResolution='provider_simplified')
        self.run_batch({'incidents':[row]})

if __name__=='__main__':unittest.main()
