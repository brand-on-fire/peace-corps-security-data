"""Offline checks for false-location prevention and lossless public publication."""
import datetime as dt
import importlib.util
import json
import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/public_data_pipeline.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('public_pipeline', SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class SourceCadenceTests(unittest.TestCase):
    def due(self, previous, current, minutes=15):
        now = dt.datetime.fromisoformat(current.replace('Z','+00:00'))
        return pipeline.source_due({'cadenceMinutes':minutes}, {'lastAttemptAt':previous}, now)

    def test_normal_runner_jitter_does_not_skip_next_quarter_hour(self):
        previous = '2026-10-05T04:22:58Z'
        current = '2026-10-05T04:37:30Z'
        elapsed = (dt.datetime.fromisoformat(current)-dt.datetime.fromisoformat(previous)).total_seconds()
        self.assertEqual(elapsed, 872)
        self.assertLess(elapsed, 15*60)  # The former predicate incorrectly skips.
        self.assertTrue(self.due(previous,current))

    def test_same_slot_never_retries_after_success_or_failure(self):
        self.assertFalse(self.due('2026-10-05T04:30:01Z','2026-10-05T04:44:59Z'))
        self.assertFalse(self.due('2026-10-05T04:37:30Z','2026-10-05T04:38:30Z'))

    def test_slot_boundary_requires_one_minute_between_attempts(self):
        self.assertFalse(self.due('2026-10-05T04:29:30Z','2026-10-05T04:30:29Z'))
        self.assertTrue(self.due('2026-10-05T04:29:30Z','2026-10-05T04:30:30Z'))

    def test_rss_uses_six_hour_utc_slots(self):
        self.assertFalse(self.due('2026-10-05T06:07:59Z','2026-10-05T11:59:59Z',360))
        self.assertTrue(self.due('2026-10-05T06:07:59Z','2026-10-05T12:07:01Z',360))

    def test_utc_slots_are_independent_of_timestamp_offset(self):
        self.assertTrue(self.due('2026-10-05T00:22:58-04:00','2026-10-05T04:37:30Z'))
        self.assertFalse(self.due('2026-10-05T00:37:30-04:00','2026-10-05T04:44:59Z'))

    def test_first_attempt_and_missed_slots_create_one_due_decision(self):
        self.assertIs(self.due(None,'2026-10-05T04:37:30Z'),True)
        self.assertIs(self.due('2026-10-02T04:37:30Z','2026-10-05T04:37:30Z'),True)

    def test_future_attempt_does_not_produce_retry_burst(self):
        self.assertFalse(self.due('2026-10-05T04:37:30Z','2026-10-05T04:22:58Z'))


class PublicPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026,10,5,tzinfo=dt.timezone.utc)
        self.country = dict(code='GH', name='Ghana', region='Africa',status='active',reviewDue='2026-11-04')
        self.source = dict(id='usgs',kind='usgs',language='en',countryCodes=['GH'])
        pipeline.save(self.root/'config/geo/index.json',[dict(code='GH',bounds=[[0,4,0,4]])])
        ring = [[0,0],[4,0],[4,4],[0,4],[0,0]]
        pipeline.save(self.root/'config/geo/GH.json',[dict(rings=[ring], minX=0,maxX=4,minY=0,maxY=4)])
        self.geo = pipeline.Geography(self.root,{'GH':self.country})

    def tearDown(self):
        self.temp.cleanup()

    def feature(self, fid='1', lon=2, lat=2):
        return dict(id=fid,geometry=dict(type='Point',coordinates=[lon,lat,10]),properties=dict(
            type='earthquake',mag=5.1,time=1791158400000,updated=1791158400000,title='M5.1 Earthquake',
            url='https://earthquake.usgs.gov/earthquakes/eventpage/'+fid,status='reviewed',alert=None))

    def test_offshore_and_country_boundary_are_not_nearest_country(self):
        self.assertEqual(self.geo.country(2,2),'GH')
        self.assertIsNone(self.geo.country(4,2))
        self.assertIsNone(self.geo.country(4.1,2))

    def test_official_feed_count_mismatch_rejects_whole_response(self):
        raw=pipeline.encode(dict(type='FeatureCollection',metadata=dict(count=2),features=[self.feature()]))
        with self.assertRaisesRegex(ValueError,'count/schema'):
            pipeline.official_incidents(self.source,raw,pipeline.stamp(self.now),{'GH':self.country},self.geo,{})

    def test_unreviewed_official_kind_is_not_interpreted_as_gdacs(self):
        with self.assertRaisesRegex(ValueError,'no reviewed adapter'):
            pipeline.official_incidents(dict(self.source,kind='unknown'),b'<rss/>',pipeline.stamp(self.now),{'GH':self.country},self.geo,{})

    def test_response_bound_rejects_whole_body_without_returning_prefix(self):
        from unittest.mock import patch
        with patch.object(pipeline,'MAX_RESPONSE',100):
            self.assertEqual(pipeline.read_complete(io.BytesIO(b'x'*100)),b'x'*100)
            with self.assertRaisesRegex(ValueError,'entire response rejected'):
                pipeline.read_complete(io.BytesIO(b'x'*101))

    def test_earthquake_is_occurrence_not_ongoing_impact(self):
        raw=pipeline.encode(dict(type='FeatureCollection',metadata=dict(count=2),features=[self.feature(),self.feature('offshore',5,2)]))
        rows=pipeline.official_incidents(self.source,raw,pipeline.stamp(self.now),{'GH':self.country},self.geo,{})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['state'],'reported')
        self.assertEqual(rows[0]['severity'],'unknown')
        self.assertIn('not assessed',rows[0]['summary'])

    def test_gdacs_representative_coordinate_is_not_an_exact_event_or_admin_match(self):
        source=dict(self.source,id='gdacs',kind='gdacs')
        raw=b'''<rss xmlns:g="http://www.gdacs.org" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel><item><title>Flood warning</title><link>https://www.gdacs.org/report</link><pubDate>2026-10-05T00:00:00Z</pubDate><g:iso3>GHA</g:iso3><g:eventtype>FL</g:eventtype><g:eventid>1</g:eventid><g:fromdate>2026-10-04T00:00:00Z</g:fromdate><geo:Point><geo:lat>2</geo:lat><geo:long>2</geo:long></geo:Point></item></channel></rss>'''
        row=pipeline.official_incidents(source,raw,pipeline.stamp(self.now),{'GH':self.country},self.geo,{'GHA':'GH'})[0]
        self.assertEqual(row['location'],dict(precision='country'))
        self.assertEqual(row['sourceMetadata']['representativePoint']['lon'],2)
        self.geo.administrative(row)
        self.assertNotIn('admin1Code',row)

    def test_overview_projection_preserves_complete_area_archive_and_text(self):
        geometry=dict(type='MultiPolygon',coordinates=[[[[0,0],[1,0],[1,1],[0,0]]]])
        row=dict(id='warning',countryCode='GH',historical=False,occurredAt='2026-10-05T00:00:00Z',
                 location=dict(precision='area',geometry=geometry),summary='Full original description',
                 reports=[dict(excerpt='Full evidence text')],sourceMetadata=dict(infoBlocks=['<complete>XML</complete>']))
        overview=pipeline.public_snapshot_record(row)
        self.assertEqual(overview['location'],dict(precision='area'))
        self.assertTrue(overview['sourceMetadata']['fullDetailInCountryArchive'])
        self.assertEqual(overview['summary'],row['summary'])
        self.assertEqual(overview['reports'],row['reports'])
        self.assertEqual(row['location']['geometry'],geometry)
        data=self.root/'docs/data'
        pipeline.write_archive(data,{'warning':row},{'GH':self.country})
        self.assertEqual(pipeline.read_archive(data,{'GH':self.country})['warning'],row)
        self.assertNotIn('geometry',pipeline.load(data/'history.json')[0]['location'])

    def test_foreign_trial_and_advice_news_remain_discovery(self):
        source=dict(id='gh-news',language='en',countryCodes=['GH'])
        items=[dict(title='Ghana newspaper: Nepal earthquake trial advice',url='https://example.org/a',
                    publishedAt='Mon, 05 Oct 2026 00:00:00 GMT',guid='a',description='Historical foreign earthquake advice')]
        rows=pipeline.news_discovery(source,items,pipeline.stamp(self.now),{'GH':self.country})
        self.assertEqual(rows[0]['status'],'needs-review')
        self.assertNotIn('countryCode',rows[0])
        self.assertNotIn('location',rows[0])
        self.assertEqual(pipeline.reviewed_news_incidents(source,items,[],pipeline.stamp(self.now),{'GH':self.country},[]),[])

    def test_admin_parent_conflict_is_withheld(self):
        ring=[[[0,0],[4,0],[4,4],[0,4],[0,0]]]
        for level,parent in [('ADM1','country:GH'),('ADM2','gb:wrong-parent')]:
            feature=dict(type='Feature',geometry=dict(type='Polygon',coordinates=ring),
                         properties=dict(id='gb:'+level,name=level,parentId=parent))
            pipeline.save(self.root/f'config/map-areas/GH-{level}.geojson',dict(features=[feature],source={}))
        row=dict(countryCode='GH',location=dict(precision='point',lat=2,lon=2))
        self.geo.administrative(row)
        self.assertEqual(row['admin1Code'],'gb:ADM1')
        self.assertNotIn('admin2Code',row)
        self.assertEqual(row['adminAssignment']['levels']['ADM2']['status'],'ambiguous')

    def test_local_crime_terms_are_discovered_without_promoting_incidents(self):
        source=dict(id='local-news',language='en',countryCodes=['GH'])
        headlines=['Police investigate murder', 'Robberies reported in the district',
                   'Medicine theft deepens hospital shortages', 'Kidnapped resident released',
                   'Burglary investigation', 'Assault sentencing for a 2024 incident',
                   'Investigan robos y asaltos', 'Dos homicidios reportados',
                   'Policía investiga una agresión', 'Enquête sur un meurtre',
                   'Cambriolages signalés', 'Un vol à main armée signalé',
                   'Enlèvement signalé dans la région']
        items=[dict(title=title,url=f'https://example.org/{i}',
                    publishedAt='Mon, 05 Oct 2026 00:00:00 GMT',guid=str(i),description='')
               for i,title in enumerate(headlines)]
        rows=pipeline.news_discovery(source,items,pipeline.stamp(self.now),{'GH':self.country})
        self.assertEqual(len(rows),len(headlines))
        self.assertTrue(all('crime' in row['categoryCandidates'] for row in rows))
        self.assertTrue(all(row['status']=='needs-review' and 'countryCode' not in row for row in rows))
        self.assertEqual(pipeline.reviewed_news_incidents(source,items,[],pipeline.stamp(self.now),{'GH':self.country},[]),[])

    def test_discovery_does_not_infer_crime_from_unexplained_death_or_french_flight(self):
        source=dict(id='local-news',language='fr',countryCodes=['GH'])
        items=[dict(title=title,url=f'https://example.org/{i}',
                    publishedAt='Mon, 05 Oct 2026 00:00:00 GMT',guid=str(i),description='')
               for i,title in enumerate(['Un vol vers Paris', 'Unexplained death under review'])]
        self.assertEqual(pipeline.news_discovery(source,items,pipeline.stamp(self.now),{'GH':self.country}),[])

    def test_archive_roundtrip_preserves_full_text_and_revisions(self):
        row=dict(id='one',countryCode='GH',occurredAt='2026-10-05T00:00:00Z',title='Whole title',
                 reports=[dict(excerpt='Complete original language. Καλημέρα. '*400)],
                 revisions=[dict(at='2026-10-05T00:01:00Z',note='Full revision')],summary='Full summary')
        data=self.root/'docs/data'
        pipeline.write_archive(data,{'one':row},{'GH':self.country})
        self.assertEqual(pipeline.read_archive(data,{'GH':self.country}),{'one':row})
        self.assertEqual(pipeline.load(data/'history.json')[0]['reports'],[])

    def test_browser_canonical_hash_uses_json_number_semantics(self):
        self.assertEqual(pipeline.publication_revision({'z':1.0,'a':-0.0}),
                         pipeline.publication_revision({'a':0,'z':1}))

    def test_expired_country_leaves_public_indices_but_retains_full_history(self):
        gh=dict(self.country,reviewDue='2026-10-04')
        al=dict(self.country,code='AL',name='Albania')
        countries={'GH':gh,'AL':al}
        raw=pipeline.encode(dict(type='FeatureCollection',metadata=dict(count=1),features=[self.feature()]))
        original=pipeline.official_incidents(self.source,raw,pipeline.stamp(self.now),{'GH':self.country},self.geo,{})[0]
        active=json.loads(json.dumps(original));active.update(id='active-al',countryCode='AL')
        active['reports'][0].update(id='active-al-report',incidentId='active-al')
        records={original['id']:original,active['id']:active}
        data=self.root/'docs/data'
        pipeline.write_archive(data,records,countries)
        pipeline.save(data/'snapshot.json',dict(coverage=[],history={'from':'2021-10-05T00:00:00Z'}))
        pipeline.save(self.root/'config/countries.json',list(countries.values()))
        pipeline.save(self.root/'config/sources.json',[dict(self.source,countryCodes=['GH','AL'])])
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=[]))
        pipeline.save(self.root/'config/country-iso3.json',{})
        result=pipeline.run(self.root,self.now)
        snapshot=pipeline.load(data/'snapshot.json')
        self.assertEqual([c['code'] for c in snapshot['countries']],['AL'])
        self.assertEqual({r['countryCode'] for r in snapshot['incidents']},{'AL'})
        self.assertEqual({r['countryCode'] for r in pipeline.load(data/'history.json')},{'AL'})
        self.assertEqual([c['code'] for c in pipeline.load(data/'history-manifest.json')['countries']],['AL'])
        self.assertEqual(snapshot['archiveTotal'],1)
        self.assertEqual(snapshot['history']['recordCount'],1)
        self.assertEqual(result['archiveCount'],2)
        self.assertEqual(result['publicArchiveCount'],1)
        self.assertEqual(pipeline.read_archive(data,countries)[original['id']],original)
        # The distinction also holds when the entire roster has expired.
        pipeline.write_archive(data,records,countries,[])
        self.assertEqual(pipeline.load(data/'history.json'),[])
        self.assertEqual(pipeline.load(data/'history-manifest.json')['recordCount'],0)
        self.assertEqual(pipeline.read_archive(data,countries),records)

    def test_plural_shortages_and_bridge_collapses_are_discovery_candidates(self):
        source=dict(id='local-news',language='en',countryCodes=['GH'])
        cases=[('Bridge collapses again','transport'),
               ('Continued theft of medicines deepens hospital shortages','health')]
        for i,(title,category) in enumerate(cases):
            item=dict(title=title,url=f'https://example.org/{i}',publishedAt='Mon, 05 Oct 2026 00:00:00 GMT',guid=str(i),description='')
            rows=pipeline.news_discovery(source,[item],pipeline.stamp(self.now),{'GH':self.country})
            self.assertIn(category,rows[0]['categoryCandidates'])

    def test_invalid_source_does_not_advance_success(self):
        previous=dict(lastSuccessAt='2026-10-04T00:00:00Z')
        result,raw=pipeline.fetch(dict(sourceId='bad',url='http://example.org/feed'),previous,pipeline.stamp(self.now))
        self.assertIsNone(raw)
        self.assertEqual(result['lastSuccessAt'],previous['lastSuccessAt'])
        self.assertEqual(result['status'],'unavailable')

    def test_failed_jittered_refresh_retains_history_and_does_not_retry_same_slot(self):
        raw=pipeline.encode(dict(type='FeatureCollection',metadata=dict(count=1),features=[self.feature()]))
        original=pipeline.official_incidents(self.source,raw,pipeline.stamp(self.now),{'GH':self.country},self.geo,{})[0]
        data=self.root/'docs/data'
        pipeline.write_archive(data,{original['id']:original},{'GH':self.country})
        pipeline.save(data/'snapshot.json',dict(coverage=[],history={'from':'2021-10-05T00:00:00Z'}))
        pipeline.save(self.root/'config/countries.json',[self.country])
        pipeline.save(self.root/'config/sources.json',[self.source])
        policy=dict(sourceId='usgs',mode='official',cadenceMinutes=15,url='https://example.org/feed')
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=[policy]))
        pipeline.save(self.root/'config/country-iso3.json',{})
        previous=dict(sourceId='usgs',lastAttemptAt='2026-10-05T04:22:58Z',
                      lastSuccessAt='2026-10-05T04:07:00Z',etag='prior-good',status='healthy')
        pipeline.save(data/'collector-state.json',{'usgs':previous})
        # A missing fixture is a deterministic offline failure; no HTTP is used.
        now=dt.datetime(2026,10,5,4,37,30,tzinfo=dt.timezone.utc)
        failed=pipeline.run(self.root,now,self.root/'missing-fixtures')
        self.assertEqual(failed['requested'],1)
        state=pipeline.load(data/'collector-state.json')['usgs']
        self.assertEqual(state['status'],'unavailable')
        self.assertEqual(state['lastAttemptAt'],'2026-10-05T04:37:30Z')
        self.assertEqual(state['lastSuccessAt'],previous['lastSuccessAt'])
        self.assertEqual(state['etag'],previous['etag'])
        self.assertEqual(pipeline.read_archive(data,{'GH':self.country})[original['id']],original)
        repeated=pipeline.run(self.root,now+dt.timedelta(minutes=1),self.root/'missing-fixtures')
        self.assertEqual(repeated['requested'],0)
        missed=pipeline.run(self.root,now+dt.timedelta(days=1),self.root/'missing-fixtures')
        self.assertEqual(missed['requested'],1)

    def _run_mock_official_update(self, source, adapter, original, incoming):
        from unittest.mock import patch
        import public_official
        data=self.root/'docs/data'
        pipeline.write_archive(data,{original['id']:original},{'GH':self.country})
        pipeline.save(data/'snapshot.json',dict(coverage=[],history={'from':'2021-10-05T00:00:00Z'}))
        pipeline.save(self.root/'config/countries.json',[self.country])
        pipeline.save(self.root/'config/sources.json',[source])
        pipeline.save(self.root/'config/country-iso3.json',{})
        pipeline.save(self.root/'config/collection-policy.json',dict(sources=[dict(
            sourceId=source['id'],mode='official',adapter=adapter,cadenceMinutes=15,
            url='https://example.invalid/synthetic-fixture')]))
        response=dict(sourceId=source['id'],lastAttemptAt=pipeline.stamp(self.now),httpStatus=200)
        collected=dict(incidents=[incoming],discoveries=[],state={'completeCountVerified':True},
                       evidence=[{'sha256':'synthetic-complete-response'}])
        # Dependency injection isolates the archive transaction from provider parsing.
        # Parser acceptance and full-source failure tests live in test_public_official.
        with patch.object(pipeline,'fetch',return_value=(response,b'complete synthetic fixture')), \
             patch.object(public_official,'collect_official',return_value=collected) as adapter_call:
            result=pipeline.run(self.root,self.now)
            adapter_call.assert_called_once()
        return result,pipeline.read_archive(data,{'GH':self.country})[original['id']]

    def test_who_secondary_report_revision_is_published_and_prior_reports_are_complete(self):
        source=dict(id='who-don',kind='directory',language='en',countryCodes=['GH'])
        original=pipeline.make_incident(source,'who-reviewed-GH','Health bulletin','Reviewed summary',
            self.country,'health','2026-10-04T00:00:00Z','2026-10-04T12:00:00Z',
            '2026-10-04T13:00:00Z',{'precision':'country'},'https://www.who.int/report',
            {'provider':'WHO'},'synthetic primary source')
        second=dict(original['reports'][0],id='who-second-report',excerpt='Entire prior report. Καλημέρα. '*200,
                    contentHash='prior-secondary-content-hash')
        original['reports'].append(second)
        incoming=json.loads(json.dumps(original));incoming['reports'][1].update(
            contentHash='revised-secondary-content-hash',excerpt='Entire changed report. 日本語. '*200)
        # Keep first report, timestamps, state, location and metadata identical:
        # this change used to disappear behind the reports[0]-only no-op check.
        result,persisted=self._run_mock_official_update(source,'who-don',original,incoming)
        self.assertEqual(result['changedIncidents'],[original['id']])
        self.assertEqual(persisted['reports'],incoming['reports'])
        self.assertEqual(persisted['reports'][0],original['reports'][0])
        self.assertEqual(persisted['revisions'][-1]['previousReports'],original['reports'])
        self.assertEqual(persisted['firstSeenAt'],original['firstSeenAt'])
        self.assertEqual(pipeline.load(self.root/'docs/data/snapshot.json')['incidents'][0]['reports'],incoming['reports'])

    def test_stale_official_representation_does_not_roll_back_newer_archive_record(self):
        raw=pipeline.encode(dict(type='FeatureCollection',metadata=dict(count=1),features=[self.feature()]))
        original=pipeline.official_incidents(self.source,raw,pipeline.stamp(self.now),
            {'GH':self.country},self.geo,{})[0]
        original['updatedAt']='2026-10-05T00:00:00Z'
        incoming=json.loads(json.dumps(original));incoming.update(updatedAt='2026-10-04T23:59:59Z',
            summary='A stale provider correction that must not overwrite the newer record.')
        incoming['sourceMetadata']['magnitude']=1.0;incoming['reports'][0]['contentHash']='stale-hash'
        result,persisted=self._run_mock_official_update(self.source,'usgs-week',original,incoming)
        self.assertEqual(result['changedIncidents'],[])
        self.assertEqual(persisted,original)
        self.assertEqual(pipeline.load(self.root/'docs/data/collector-state.json')['usgs']['status'],'healthy')

    def test_redirect_handler_refuses_host_and_scheme_before_follow(self):
        handler=pipeline.ReviewedSourceRedirect('source.example')
        request=pipeline.urllib.request.Request('https://source.example/feed')
        for url in ('https://other.example/feed','http://source.example/feed'):
            with self.assertRaisesRegex(ValueError,'before follow'):
                handler.redirect_request(request,None,302,'Found',{},url)
        follow=handler.redirect_request(request,None,302,'Found',{},'https://source.example/feed/new')
        self.assertEqual(follow.full_url,'https://source.example/feed/new')


if __name__=='__main__':
    unittest.main()
