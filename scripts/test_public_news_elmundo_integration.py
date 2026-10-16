"""Offline opt-in, partial-health, retention and publication transaction checks."""
import copy
import datetime as dt
import shutil
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import public_data_pipeline as pipeline
import public_data_refresh as refresh
import public_official as official
import public_news_elmundo_fetch as elmundo
from test_public_news_elmundo import SOURCE as IDENTITY, NOW, URL, article, reviewed_geography
from test_public_news_elmundo_fetch import POLICY, listing, Fetch

COUNTRY=dict(code='SV',name='El Salvador',region='Americas',lat=13.7,lon=-89.2,status='active',
    evidenceUrl='https://www.peacecorps.gov/el-salvador/',evidenceText='Synthetic reviewed-country fixture.',
    evidenceDate='2026-10-01',verifiedAt='2026-10-01T00:00:00Z',reviewDue='2026-11-15',languages=['es'])
SOURCE=dict(IDENTITY,feedUrl=elmundo.LISTING,kind='directory',cadenceMinutes=30,enabled=False,
    access='public-page',verifiedAt='2026-10-01T00:00:00Z',notes='Synthetic test source; no external requests.')


def initialize(root,enabled=True):
    source=dict(SOURCE,enabled=enabled)
    for name,value in [('countries.json',[COUNTRY]),('sources.json',[source]),
        ('collection-policy.json',{'sources':[POLICY] if enabled else []}),
        ('staged-elmundo-policy.json',{'enabled':False,'source':SOURCE,'policy':POLICY}),
        ('geo/index.json',[]),('country-iso3.json',{'SLV':'SV'}),('admin-places.json',[])]:
        pipeline.save(root/'config'/name,value)
    data=root/'docs/data'
    snapshot=dict(schemaVersion=1,mode='live',generatedAt='2026-10-15T00:00:00Z',countries=[COUNTRY],
        sources=[source],incidents=[],coverage=[],archiveTotal=0,
        history={'recordCount':0,'from':'2025-10-16T00:00:00Z','to':'2026-10-15T00:00:00Z','sources':[SOURCE['id']],'notes':[]})
    pipeline.save(data/'snapshot.json',snapshot);pipeline.write_archive(data,{}, {'SV':COUNTRY})
    return data


def client(documents):
    fetcher=Fetch(documents)
    def fetch(policy,previous,now,fixtures):
        result,raw=fetcher(policy)
        result.update(sourceId=policy['sourceId'],url=policy['url'],lastAttemptAt=now)
        if raw is None:result.update(status='unavailable',lastSuccessAt=previous.get('lastSuccessAt'),message='Synthetic failed response')
        return result,raw
    fetch.calls=fetcher.calls
    return fetch


class ElMundoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'collector';self.data=initialize(self.root)
        self.documents={elmundo.LISTING:listing([URL]),URL:article()['raw']}

    def tearDown(self):self.temp.cleanup()

    def run_pipeline(self,documents=None,now=NOW):
        with patch.object(pipeline,'fetch',client(documents or self.documents)):
            return pipeline.run(self.root,dt.datetime.fromisoformat(now.replace('Z','+00:00')))

    def run_refresh(self,documents,now):
        def runner(command,**kwargs):
            stage=Path(command[command.index('--root')+1])
            with patch.object(pipeline,'fetch',client(documents)):
                pipeline.run(stage,dt.datetime.fromisoformat(now.replace('Z','+00:00')))
        return refresh.refresh(self.root,dt.datetime.fromisoformat(now.replace('Z','+00:00')),runner=runner,environment={})

    def test_disabled_stage_has_no_requests_or_activation(self):
        initialize(self.root,False)
        with patch.object(pipeline,'fetch',side_effect=AssertionError('Disabled source requested')):
            result=pipeline.run(self.root,dt.datetime.fromisoformat(NOW))
        self.assertEqual(result['requested'],0)
        self.assertEqual(pipeline.load(self.data/'snapshot.json')['coverage'][0]['status'],'disabled')

    def test_opt_in_canonical_projection_is_valid_and_original_language_kept(self):
        baseline=Path(self.temp.name)/'baseline';shutil.copytree(self.data,baseline)
        result=self.run_pipeline();self.assertEqual(result['sources'][0]['status'],'healthy')
        snapshot=refresh.validate_candidate(self.root,baseline)
        row=snapshot['incidents'][0]
        self.assertEqual((row['category'],row['severity'],row['evidenceStatus']),('crime','unknown','reported'))
        self.assertEqual(row['reports'][0]['language'],'es');self.assertEqual(row['sourceMetadata']['eventTimeBasis'],'publication')
        self.assertEqual(row['sourceMetadata']['summaryLanguage'],'en');self.assertEqual(row['location'],{'precision':'country'})
        state=pipeline.load(self.data/'collector-state.json')[SOURCE['id']]
        self.assertEqual(state['lastSuccessAt'],NOW);self.assertNotIn('validatedPartial',result['sources'][0])

    def install_geography(self):
        geography=reviewed_geography()
        pipeline.save(self.root/'config/admin-places.json',geography['areas'])
        for level,layer in geography['boundary_layers'].items():
            pipeline.save(self.root/f'config/map-areas/SV-{level}.geojson',layer)

    def test_geography_only_upgrade_and_invalidation_survive_complete_pipeline_transaction(self):
        self.run_pipeline();first=next(iter(pipeline.read_archive(self.data,{'SV':COUNTRY}).values()))
        self.install_geography()
        result=self.run_pipeline(now='2026-10-16T08:06:00Z')
        self.assertEqual(result['sources'][0]['status'],'healthy')
        upgraded=next(iter(pipeline.read_archive(self.data,{'SV':COUNTRY}).values()))
        self.assertEqual(upgraded['admin2Name'],'Colón');self.assertEqual(upgraded['location'],{'precision':'area'})
        self.assertEqual(upgraded['reports'],first['reports']);self.assertEqual(upgraded['occurredAt'],first['occurredAt'])
        self.assertEqual(upgraded['revisions'][-1]['previousReports'],first['reports'])
        # A corrupt local boundary must remove unsupported precision, without
        # failing the valid complete crime response or fabricating source changes.
        (self.root/'config/map-areas/SV-ADM2.geojson').write_text('{broken complete boundary')
        result=self.run_pipeline(now='2026-10-16T15:06:00Z')
        self.assertEqual(result['sources'][0]['status'],'healthy')
        invalidated=next(iter(pipeline.read_archive(self.data,{'SV':COUNTRY}).values()))
        self.assertEqual(invalidated['location'],{'precision':'country'});self.assertNotIn('admin1Code',invalidated)
        self.assertEqual(invalidated['reports'],first['reports']);self.assertEqual(len(invalidated['revisions']),2)
        self.assertEqual(invalidated['sourceMetadata']['administrativeHistory'][-1]['previous']['admin2Name'],'Colón')

    def test_malformed_local_geographic_index_cannot_drop_valid_crime_evidence(self):
        self.install_geography();(self.root/'config/admin-places.json').write_text('{broken complete index')
        result=self.run_pipeline();self.assertEqual(result['sources'][0]['status'],'healthy')
        row=next(iter(pipeline.read_archive(self.data,{'SV':COUNTRY}).values()))
        self.assertEqual(row['location'],{'precision':'country'});self.assertNotIn('adminAssignment',row)
        self.assertEqual(row['sourceMetadata']['administrativeMatchStatus'],'unverified-dataset-or-place')

    def test_all_partial_refresh_publishes_healthy_article_retains_failed_evidence_and_clocks(self):
        first=self.run_refresh(self.documents,NOW);self.assertEqual(first['outcome'],'complete')
        baseline=pipeline.read_archive(self.data,{'SV':COUNTRY});before=copy.deepcopy(next(iter(baseline.values())))
        new=URL+'-new';docs={elmundo.LISTING:listing([new]),URL:None,new:article(url=new,status='2110000000000000002')['raw']}
        # Due old accepted article is checked after six hours even off the list.
        when='2026-10-16T08:06:00Z';result=self.run_refresh(docs,when)
        self.assertEqual(result['outcome'],'degraded',result['errors'])
        self.assertEqual(result['sourceCounts'],dict(attempted=1,healthy=0,failed=1))
        self.assertEqual(result['lastSuccessfulCollectionAt'],NOW)
        self.assertEqual(result['publicationGeneratedAt'],when)
        records=pipeline.read_archive(self.data,{'SV':COUNTRY})
        self.assertEqual(len(records),2);self.assertEqual(records[before['id']],before)
        state=pipeline.load(self.data/'collector-state.json')[SOURCE['id']]
        self.assertEqual(state['lastSuccessAt'],NOW);self.assertEqual(state['lastPartialSuccessAt'],when)
        self.assertIn(URL,state['elmundoQuarantine'])
        coverage=pipeline.load(self.data/'snapshot.json')['coverage'][0]
        self.assertEqual(coverage['status'],'unavailable');self.assertEqual(coverage['lastSuccessAt'],NOW)
        self.assertIn('failed article',coverage['message'])
        # Successful later retry clears quarantine and re-establishes full checks.
        docs[URL]=article()['raw'];recovered=self.run_refresh(docs,'2026-10-16T09:06:00Z')
        self.assertEqual(recovered['outcome'],'complete',recovered['errors'])
        self.assertEqual(recovered['lastSuccessfulCollectionAt'],'2026-10-16T09:06:00Z')

    def test_listing_only_partial_persists_retry_state_without_success_claim(self):
        result=self.run_refresh({elmundo.LISTING:listing([URL]),URL:None},NOW)
        self.assertEqual(result['outcome'],'degraded',result['errors'])
        self.assertIsNone(result['lastSuccessfulCollectionAt'])
        state=pipeline.load(self.data/'collector-state.json')[SOURCE['id']]
        self.assertIsNone(state['lastSuccessAt']);self.assertIn(URL,state['elmundoQuarantine'])
        self.assertFalse(pipeline.read_archive(self.data,{'SV':COUNTRY}))

    def test_malformed_or_failed_listing_keeps_previous_publication_and_all_state(self):
        self.run_refresh(self.documents,NOW)
        original=(self.data/'snapshot.json').read_bytes();state=(self.data/'collector-state.json').read_bytes()
        for raw in [None,b'<html>Incomplete listing</html>']:
            result=self.run_refresh({elmundo.LISTING:raw},'2026-10-16T02:06:00Z')
            self.assertEqual(result['outcome'],'failed');self.assertEqual((self.data/'snapshot.json').read_bytes(),original)
            self.assertEqual((self.data/'collector-state.json').read_bytes(),state)
            self.assertNotIn('validatedPartial',result['sources'][0])

    def test_canonical_size_failure_cannot_commit_quarantine_or_partial_marker(self):
        self.run_pipeline();before=pipeline.read_archive(self.data,{'SV':COUNTRY})
        state=pipeline.load(self.data/'collector-state.json')[SOURCE['id']]
        good=URL+'-new';bad=URL+'-bad'
        title=next(iter(before.values()))['title']+' '+('Additional complete source context '*60)
        docs={elmundo.LISTING:listing([good,bad]),good:article(url=good,title=title,status='2110000000000000002')['raw'],bad:None}
        limit=len(pipeline.encode(next(iter(before.values()))))+100
        with patch.object(pipeline,'PART_LIMIT',limit):result=self.run_pipeline(docs,'2026-10-16T02:06:00Z')
        self.assertEqual(result['sources'][0]['status'],'unavailable');self.assertNotIn('validatedPartial',result['sources'][0])
        after=pipeline.load(self.data/'collector-state.json')[SOURCE['id']]
        for key in ('elmundoChecked','elmundoPending','elmundoQuarantine','lastSuccessAt'):
            self.assertEqual(after[key],state[key])
        self.assertEqual(pipeline.read_archive(self.data,{'SV':COUNTRY}),before)

    def test_partial_marker_cannot_authorize_unreviewed_adapter_or_other_source(self):
        marker=dict(adapter='sv-elmundo',sourceId=SOURCE['id'],listingValidated=True,checkedArticles=0,failedArticles=1,queuedArticles=1)
        valid=dict(sourceId=SOURCE['id'],status='unavailable',validatedPartial=marker)
        self.assertTrue(refresh.validated_partial(valid,[POLICY]))
        for row,policies in [(dict(valid,sourceId='other'),[POLICY]),(valid,[]),
            (dict(valid,validatedPartial=dict(marker,listingValidated=False)),[POLICY]),
            (dict(valid,validatedPartial=dict(marker,checkedArticles=True)),[POLICY])]:
            self.assertFalse(refresh.validated_partial(row,policies))

    def test_initial_listing_etag_never_skips_correction_work(self):
        previous=dict(url=elmundo.LISTING,etag='old',lastModified='old',lastSuccessAt=NOW)
        self.assertEqual(official.request_state(POLICY,previous),dict(url=elmundo.LISTING,lastSuccessAt=NOW))


if __name__=='__main__':unittest.main()
