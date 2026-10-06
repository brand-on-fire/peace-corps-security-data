"""Routine forecasts stay outside monitor views; severe warning lifecycle remains intact."""
import copy
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import public_data_pipeline as pipeline
import public_cap as cap

SOURCE=dict(id='cap-gh-example',kind='cap',language='en',countryCodes=['GH'])
COUNTRY=dict(code='GH',name='Ghana',region='Africa',status='active',reviewDue='2026-11-05')
NOW='2026-10-06T02:00:00Z'

def message(identifier='m1',severity='Moderate',kind='Alert',reference='',event='Thunderstorm',sent='2026-10-06T01:00:00Z'):
    raw=f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>{identifier}</identifier><sender>issuer.example</sender><sent>{sent}</sent><status>Actual</status><scope>Public</scope><msgType>{kind}</msgType><references>{reference}</references><info><language>en</language><category>Met</category><event>{event}</event><severity>{severity}</severity><urgency>Immediate</urgency><certainty>Observed</certainty><expires>2026-10-06T06:00:00Z</expires><headline>{event} forecast</headline><description>Complete issuer bulletin; not a report of verified damage.</description><area><areaDesc>Test region</areaDesc></area></info></alert>'''.encode()
    return cap.parse_alert(SOURCE,raw,'https://issuer.example/'+identifier,NOW,{'GH':COUNTRY},[])

class WeatherScopeTests(unittest.TestCase):
    maxDiff = None
    def setUp(self):
        self.run_count=0;self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.data=self.root/'docs/data'
        for name,value in {'config/countries.json':[COUNTRY],'config/sources.json':[SOURCE],
            'config/collection-policy.json':{'sources':[dict(sourceId=SOURCE['id'],mode='official',cadenceMinutes=15,url='https://issuer.example/feed')]},
            'config/country-iso3.json':{},'config/geo/index.json':[],
            'docs/data/snapshot.json':dict(coverage=[],history={'from':'2025-10-05T00:00:00Z'})}.items():pipeline.save(self.root/name,value)
    def tearDown(self):self.temp.cleanup()
    def run_messages(self,messages):
        response=dict(sourceId=SOURCE['id'],lastAttemptAt=NOW,httpStatus=200)
        with patch.object(pipeline,'fetch',return_value=(response,b'complete fixture')),patch.object(cap,'collect_cap',return_value=messages):
            now=dt.datetime.fromisoformat(NOW.replace('Z','+00:00'))+dt.timedelta(minutes=20*self.run_count)
            self.run_count+=1
            return pipeline.run(self.root,now)
    def test_only_severe_weather_warnings_pass_without_hiding_actual_disasters(self):
        for severity in ('Minor','Moderate','Unknown','Severe','Extreme'):
            with self.subTest(severity=severity):self.assertEqual(pipeline.public_incident(message(severity=severity)['record']),severity in ('Severe','Extreme'))
        flood=message(event='Flood warning')['record'];self.assertTrue(pipeline.public_incident(flood))
        impact=copy.deepcopy(message()['record']);impact.update(id='gdacs-documented-fire',sourceIds=['gdacs']);impact['sourceMetadata']['provider']='GDACS'
        self.assertTrue(pipeline.public_incident(impact))
        crime=copy.deepcopy(message()['record']);crime['category']='crime';self.assertTrue(pipeline.public_incident(crime))
    def test_prior_routine_records_are_complete_in_retained_storage_but_absent_from_public_views(self):
        routine=message()['record'];severe=message('m2','Severe')['record'];records={r['id']:r for r in (routine,severe)}
        pipeline.write_archive(self.data,records,{'GH':COUNTRY})
        self.assertEqual(pipeline.read_archive(self.data,{'GH':COUNTRY}),records)
        self.assertEqual(pipeline.load(self.data/'retained-weather/GH.json'),[routine])
        self.assertEqual(pipeline.load(self.data/'history/GH.json'),[severe])
        self.assertEqual([r['id'] for r in pipeline.load(self.data/'history.json')],[severe['id']])
        self.assertEqual(pipeline.load(self.data/'history-manifest.json')['recordCount'],1)
        self.assertEqual(pipeline.current_snapshot_rows(records,{'GH':COUNTRY}),[pipeline.public_snapshot_record(severe)])
    def test_new_routine_forecast_is_not_admitted_and_source_stays_healthy(self):
        audit=self.run_messages([message()])
        self.assertEqual(pipeline.read_archive(self.data,{'GH':COUNTRY}),{})
        self.assertFalse((self.data/'retained-weather/GH.json').exists())
        self.assertEqual(pipeline.load(self.data/'snapshot.json')['incidents'],[])
        self.assertEqual(audit['publicArchiveCount'],0)
        self.assertEqual(audit['sources'][0]['acceptedCount'],0)
        self.assertEqual(audit['sources'][0]['routineWeatherExcludedCount'],1)
        self.assertEqual(pipeline.load(self.data/'collector-state.json')[SOURCE['id']]['status'],'healthy')
    def test_retained_forecast_can_upgrade_to_severe_then_cancel_with_prior_evidence(self):
        prior=message()['record'];pipeline.write_archive(self.data,{prior['id']:prior},{'GH':COUNTRY})
        update=message('m2','Extreme','Update','issuer.example,m1,2026-10-06T01:00:00Z',sent='2026-10-06T01:15:00Z')
        self.run_messages([update]);records=pipeline.read_archive(self.data,{'GH':COUNTRY});current=records[prior['id']]
        self.assertEqual(current['severity'],'severe');self.assertEqual(current['firstSeenAt'],prior['firstSeenAt'])
        self.assertEqual(current['revisions'][-1]['previousReports'],prior['reports'])
        self.assertEqual(pipeline.load(self.data/'retained-weather/GH.json'),[])
        self.assertEqual(len(pipeline.load(self.data/'history.json')),1)
        cancel=message('m3','Unknown','Cancel','issuer.example,m2,2026-10-06T01:15:00Z',sent='2026-10-06T01:30:00Z')
        self.run_messages([cancel]);current=pipeline.read_archive(self.data,{'GH':COUNTRY})[prior['id']]
        self.assertEqual(current['state'],'cancelled');self.assertEqual(current['severity'],'severe')
        self.assertEqual(len(pipeline.load(self.data/'history.json')),1)
        self.assertEqual(current['reports'][0]['title'],'Warning cancelled')
    def test_downgrade_ends_prior_severe_warning_without_erasing_its_history(self):
        prior=message(severity='Severe')['record'];pipeline.write_archive(self.data,{prior['id']:prior},{'GH':COUNTRY})
        update=message('m2','Minor','Update','issuer.example,m1,2026-10-06T01:00:00Z',sent='2026-10-06T01:15:00Z')
        self.run_messages([update]);current=pipeline.read_archive(self.data,{'GH':COUNTRY})[prior['id']]
        self.assertEqual(current['severity'],'severe');self.assertEqual(current['state'],'expired')
        self.assertEqual(current['title'],prior['title']);self.assertEqual(current['location'],prior['location'])
        self.assertEqual(current['sourceMetadata']['currentProviderSeverity'],'Minor')
        self.assertEqual(current['sourceMetadata']['expires'],'2026-10-06T01:15:00Z')
        self.assertEqual(current['revisions'][-1]['previousReports'],prior['reports'])
        self.assertEqual(len(pipeline.load(self.data/'history.json')),1)
        self.assertEqual(len(pipeline.load(self.data/'snapshot.json')['incidents']),1)
        self.assertFalse((self.data/'retained-weather/GH.json').exists())

if __name__=='__main__':unittest.main()
