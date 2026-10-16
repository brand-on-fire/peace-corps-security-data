"""Offline advisory projection, archive integrity and source-failure regressions."""
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
if not (SCRIPTS/'public_data_pipeline.py').exists():
    SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import public_data_pipeline as pipeline
import public_data_refresh as refresh
import public_security_official as official

NOW = dt.datetime(2026,10,16,1,tzinfo=dt.timezone.utc)
SOURCE = dict(id='cdc-travel-notices',kind='directory',language='en',
              url=official.CDC,countryCodes=['ID','CR','CO','MX'])
COUNTRIES = {code:dict(code=code,name=name,region='Synthetic fixture',status='active',reviewDue='2026-11-15')
             for code,name in [('ID','Indonesia'),('CR','Costa Rica'),('CO','Colombia'),('MX','Mexico')]}


def notice(slug='zika-indonesia', occurred='2026-08-07T00:00:00Z'):
    code,disease,_ = official.CDC_PROFILES[slug]
    identity = 'cdc-'+slug+'-'+code
    text = 'Complete synthetic source statement. Español. 日本語. End of source text.'
    row = dict(id=identity,title=disease+' in '+COUNTRIES[code]['name'],summary=text,countryCode=code,
        region='Synthetic fixture',category='health',severity='unknown',evidenceStatus='official',state='reported',
        occurredAt=occurred,updatedAt=occurred,firstSeenAt='2026-10-15T23:59:00Z',
        location=dict(precision='country'),sourceIds=[SOURCE['id']],historical=False,aiStatus='not-requested',
        sourceMetadata=dict(provider='CDC',providerActiveNotice=True,providerListed=True,providerNoticeLevel=2,
            firstPublicationTime=occurred,providerPublicationTime=occurred,
            eventTimeBasis='notice_publication_not_outbreak_onset'),
        reports=[dict(id=identity+'-report',incidentId=identity,sourceId=SOURCE['id'],title=disease,
            url='https://wwwnc.cdc.gov/travel/notices/level2/'+slug,language='en',publishedAt=occurred,
            retrievedAt='2026-10-15T23:59:00Z',excerpt=text,contentHash=pipeline.digest([slug,text]),
            independentGroup=SOURCE['id'])])
    pipeline.apply_archive_age(row,NOW)
    return row


def five_notices():
    dates = ['2026-09-18T00:00:00Z','2026-09-18T00:00:00Z','2026-08-07T00:00:00Z',
             '2026-07-30T00:00:00Z','2025-12-23T00:00:00Z']
    return {row['id']:row for row in (notice(slug,date) for slug,date in zip(official.CDC_PROFILES,dates))}


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'baseline'
        self.candidate = Path(self.temp.name)/'candidate'
        self.records = five_notices()
        for root in (self.root,self.candidate):
            pipeline.save(root/'config/countries.json',list(COUNTRIES.values()))
            pipeline.save(root/'config/sources.json',[SOURCE])
            pipeline.save(root/'config/collection-policy.json',dict(sources=[]))
            pipeline.save(root/'config/admin-places.json',[])
            pipeline.save(root/'config/country-iso3.json',{})
            pipeline.save(root/'config/geo/index.json',[])
        self.write(self.root,'2026-10-16T00:00:00Z')

    def tearDown(self):
        self.temp.cleanup()

    def project(self, records=None, countries=None, start='2021-10-16T00:00:00Z', end=None):
        return pipeline.advisory_snapshot_rows(self.records if records is None else records,
            COUNTRIES if countries is None else countries,start,end or pipeline.stamp(NOW))

    def write(self, root, when=None, records=None, advisories=None):
        records = self.records if records is None else records
        when = when or pipeline.stamp(NOW)
        snapshot = dict(generatedAt=when,mode='live',countries=list(COUNTRIES.values()),sources=[SOURCE],
            incidents=pipeline.current_snapshot_rows(records,COUNTRIES),archiveTotal=len(records),
            history={'recordCount':len(records),'from':'2021-10-16T00:00:00Z','to':when,'sources':[SOURCE['id']],'notes':[]},
            coverage=[dict(sourceId=SOURCE['id'],status='healthy',lastAttemptAt='2026-10-15T23:59:00Z',
                lastSuccessAt='2026-10-15T23:59:00Z')],collector={'discoveryCount':0},
            advisoryRecords=self.project(records,end=when) if advisories is None else advisories)
        snapshot['publication'] = dict(activeCountryCodes=sorted(COUNTRIES),revision=pipeline.publication_revision(snapshot))
        pipeline.write_archive(root/'docs/data',records,COUNTRIES,COUNTRIES,snapshot['publication'])
        pipeline.save(root/'docs/data/snapshot.json',snapshot)
        return snapshot

    def validate(self):
        return refresh.validate_candidate(self.candidate,self.root/'docs/data')

    def test_five_complete_projections_keep_three_old_dates_and_two_recent_events(self):
        snapshot = self.write(self.candidate)
        accepted = self.validate()
        self.assertEqual(len(accepted['advisoryRecords']),5)
        self.assertEqual(sum(row['historical'] for row in accepted['advisoryRecords']),3)
        self.assertEqual(len(accepted['incidents']),2)
        self.assertEqual((accepted['archiveTotal'],accepted['history']['recordCount']),(5,5))
        for row in snapshot['advisoryRecords']:
            self.assertEqual(row,pipeline.public_snapshot_record(self.records[row['id']]))
        shared = {row['id']:row for row in accepted['incidents']}
        self.assertTrue(all(shared[row['id']]==row for row in accepted['advisoryRecords'] if row['id'] in shared))

    def test_canonical_projection_is_independent_copy_and_deterministically_ordered(self):
        forward = self.project()
        self.assertEqual(forward,self.project(dict(reversed(list(self.records.items())))))
        forward[0]['reports'][0]['excerpt']='Unpublished mutation'
        self.assertNotEqual(forward,self.project())

    def test_exact_projection_rejects_omissions_duplicates_forgery_and_inactive_country(self):
        valid = self.project()
        forged = copy.deepcopy(valid);forged[0]['summary']='Forged summary'
        foreign = copy.deepcopy(valid);foreign[0]['countryCode']='NP'
        stale = copy.deepcopy(valid);stale[0]['sourceMetadata']['providerActiveNotice']=False
        outside = copy.deepcopy(valid);outside[0]['occurredAt']='2020-01-01T00:00:00Z'
        for label,rows in [('missing',valid[:-1]),('duplicate',valid+[valid[0]]),('forged',forged),
                           ('inactive',foreign),('stale',stale),('outside',outside)]:
            with self.subTest(label=label):
                self.write(self.candidate,advisories=rows)
                with self.assertRaisesRegex(ValueError,'advisory'):
                    self.validate()
        snapshot=self.write(self.candidate)
        snapshot.pop('advisoryRecords');snapshot.pop('publication')
        snapshot['publication']=dict(activeCountryCodes=sorted(COUNTRIES),revision=pipeline.publication_revision(snapshot))
        pipeline.save(self.candidate/'docs/data/snapshot.json',snapshot)
        with self.assertRaisesRegex(ValueError,'advisory'):
            self.validate()

    def test_scope_excludes_unreviewed_wrong_source_date_basis_and_unsupported_location(self):
        original = notice()
        mutations = [dict(id='cdc-unreviewed-ID'),dict(countryCode='CR'),dict(category='crime'),
                     dict(sourceIds=['other-source']),dict(severity='severe'),dict(location={'precision':'point','lat':0,'lon':0}),
                     dict(sourceMetadata={**original['sourceMetadata'],'eventTimeBasis':'guessed_onset'}),
                     dict(sourceMetadata={**original['sourceMetadata'],'firstPublicationTime':'2026-01-01T00:00:00Z'}),
                     dict(sourceMetadata={**original['sourceMetadata'],'providerListed':'yes'}),dict(evidenceStatus='unverified')]
        for mutation in mutations:
            row={**copy.deepcopy(original),**mutation}
            with self.subTest(mutation=mutation):
                self.assertEqual(self.project({row['id']:row}),[])
        bad=copy.deepcopy(original);bad['reports'][0]['url']='https://wwwnc.cdc.gov/travel/notices/level2/unreviewed'
        self.assertEqual(self.project({bad['id']:bad}),[])
        bad=copy.deepcopy(original);bad['occurredAt']=bad['sourceMetadata']['firstPublicationTime']='2026-08-06T00:00:00Z'
        self.assertEqual(self.project({bad['id']:bad}),[])
        bad=copy.deepcopy(original);bad['revisions']=[dict(previousReports=[dict(bad['reports'][0],sourceId='other-source')])]
        self.assertEqual(self.project({bad['id']:bad}),[])
        self.assertEqual(self.project({original['id']:original},countries={'CR':COUNTRIES['CR']}),[])

    def test_history_bounds_are_inclusive_without_recent_event_exception(self):
        row=notice();records={row['id']:row}
        self.assertEqual(self.project(records,start=row['occurredAt'],end=row['occurredAt']),[row])
        self.assertEqual(self.project(records,start='2026-08-08T00:00:00Z'),[])
        self.assertEqual(self.project(records,end='2026-08-06T23:59:59Z'),[])
        snapshot=self.write(self.candidate)
        old=next(row for row in snapshot['advisoryRecords'] if row['historical'])
        snapshot['incidents'].append(old);snapshot.pop('publication')
        snapshot['publication']=dict(activeCountryCodes=sorted(COUNTRIES),revision=pipeline.publication_revision(snapshot))
        pipeline.save(self.candidate/'docs/data/snapshot.json',snapshot)
        with self.assertRaisesRegex(ValueError,'current incident'):
            self.validate()

    def test_terminal_records_are_retained_with_all_prior_evidence(self):
        for state,evidence in [('expired','unverified'),('cancelled','official'),('resolved','official')]:
            records=copy.deepcopy(self.records);row=next(iter(records.values()))
            row.update(state=state,evidenceStatus=evidence,updatedAt=pipeline.stamp(NOW))
            row['sourceMetadata'].update(providerActiveNotice=False,providerListed=False)
            row['revisions']=[dict(at=pipeline.stamp(NOW),note='Changed complete source representation',
                                   previousReports=copy.deepcopy(row['reports']))]
            with self.subTest(state=state):
                self.write(self.candidate,records=records)
                self.assertEqual(len(self.validate()['advisoryRecords']),5)

    def test_failed_cdc_retrieval_keeps_complete_projection_and_successful_check(self):
        data=self.root/'docs/data'
        original=pipeline.load(data/'snapshot.json')
        other=dict(id='independent-feed',kind='usgs',language='en',countryCodes=['ID'])
        pipeline.save(self.root/'config/sources.json',[SOURCE,other])
        policies=[dict(sourceId=SOURCE['id'],mode='official',adapter='cdc-notices',cadenceMinutes=15,url=official.CDC),
                  dict(sourceId=other['id'],mode='official',cadenceMinutes=15,url='https://example.invalid/synthetic')]
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=policies))
        previous=original['coverage'][0]
        pipeline.save(data/'collector-state.json',{SOURCE['id']:previous})
        complete_empty=pipeline.encode(dict(type='FeatureCollection',metadata=dict(count=0),features=[]))
        def fetch(policy, prior, when, fixtures):
            response=dict(sourceId=policy['sourceId'],lastAttemptAt=when,httpStatus=200)
            return response,b'<rss>invalid complete source response</rss>' if policy['sourceId']==SOURCE['id'] else complete_empty
        with patch.object(pipeline,'fetch',side_effect=fetch):
            result=pipeline.run(self.root,NOW)
        snapshot=pipeline.load(data/'snapshot.json')
        self.assertEqual(snapshot['advisoryRecords'],original['advisoryRecords'])
        self.assertEqual(pipeline.read_archive(data,COUNTRIES),self.records)
        states={row['sourceId']:row for row in snapshot['coverage']}
        self.assertEqual(states[SOURCE['id']]['status'],'unavailable')
        self.assertEqual(states[SOURCE['id']]['lastSuccessAt'],previous['lastSuccessAt'])
        self.assertEqual(states[other['id']]['lastSuccessAt'],pipeline.stamp(NOW))
        self.assertEqual(states[other['id']]['status'],'healthy')
        self.assertEqual(result['changedIncidents'],[])

    def test_legacy_writer_refuses_advisory_snapshot_before_loading_or_writing_inputs(self):
        script=SCRIPTS/'refresh_merge_public.py'
        if not script.exists():
            self.skipTest('Legacy one-off writer is not distributed with public collector')
        legacy=Path(self.temp.name)/'legacy'
        (legacy/'scripts').mkdir(parents=True)
        shutil.copyfile(script,legacy/'scripts'/script.name)
        pipeline.save(legacy/'data/countries.json',[])
        pipeline.save(legacy/'data/snapshot.json',{'advisoryRecords':[]})
        before=(legacy/'data/snapshot.json').read_bytes()
        result=subprocess.run([sys.executable,'-B',str(legacy/'scripts'/script.name),str(legacy/'missing-inputs')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Legacy merge cannot update advisory-enabled snapshots',result.stderr)
        self.assertEqual((legacy/'data/snapshot.json').read_bytes(),before)
        self.assertFalse((legacy/'data/history.json').exists())


if __name__=='__main__':
    unittest.main()
