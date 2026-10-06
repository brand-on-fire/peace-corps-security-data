"""Registry health is metadata; only an actual admitted attempt creates timestamps."""
import copy
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_data_pipeline as pipeline


class RegistryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'docs/data'
        self.now = dt.datetime(2026, 10, 6, 7, 45, tzinfo=dt.timezone.utc)
        self.country = dict(code='GH', name='Ghana', region='Africa', status='active', reviewDue='2026-11-04')
        self.old = dict(id='usgs', kind='usgs', language='en', countryCodes=['GH'], enabled=False)
        self.reference = dict(id='new-reference', kind='directory', language='en', countryCodes=['GH'], enabled=False)
        self.feed = dict(id='new-reviewed-feed', kind='rss', language='en', countryCodes=['GH'], enabled=True, url='https://example.org/')
        self.sources = [self.old, self.reference, self.feed]
        pipeline.save(self.root/'config/countries.json', [self.country])
        pipeline.save(self.root/'config/sources.json', self.sources)
        pipeline.save(self.root/'config/country-iso3.json', {})
        pipeline.save(self.root/'config/collection-policy.json', dict(sources=[]))
        pipeline.save(self.root/'config/geo/index.json', [dict(code='GH', bounds=[[0,4,0,4]])])
        pipeline.save(self.root/'config/geo/GH.json', [dict(rings=[[[0,0],[4,0],[4,4],[0,4],[0,0]]], minX=0, maxX=4, minY=0, maxY=4)])
        geo = pipeline.Geography(self.root, {'GH':self.country})
        feature = dict(id='preserved', geometry=dict(type='Point', coordinates=[2,2,10]), properties=dict(type='earthquake', mag=5.1, time=int(self.now.timestamp()*1000), updated=int(self.now.timestamp()*1000), title='Recorded earthquake', url='https://earthquake.usgs.gov/earthquakes/eventpage/preserved', status='reviewed', alert=None))
        row = pipeline.official_incidents(self.old, pipeline.encode(dict(type='FeatureCollection', metadata=dict(count=1), features=[feature])), pipeline.stamp(self.now), {'GH':self.country}, geo, {})[0]
        row['firstSeenAt'] = '2026-10-05T07:45:00Z'
        row['revisions'] = [dict(at='2026-10-05T08:45:00Z', note='Retained earlier representation', previousReports=copy.deepcopy(row['reports']))]
        self.original = copy.deepcopy(row)
        pipeline.write_archive(self.data, {row['id']:row}, {'GH':self.country})
        self.health = dict(sourceId='usgs', status='unavailable', lastAttemptAt='2026-10-06T07:00:00Z', lastSuccessAt='2026-10-05T22:48:47Z', message='Original outage and success remain visible.')
        pipeline.save(self.data/'snapshot.json', dict(coverage=[self.health], history={'from':'2021-10-05T00:00:00Z'}))
        self.fixtures = self.root/'fixtures'
        self.fixtures.mkdir()
        self.network = patch.object(pipeline.urllib.request, 'build_opener', side_effect=AssertionError('Network forbidden by this regression'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.temp.cleanup()

    def run_pipeline(self, now=None, implementation=pipeline):
        before_policy = (self.root/'config/collection-policy.json').read_bytes()
        with patch.object(implementation, 'fetch', wraps=implementation.fetch) as fetch:
            audit = implementation.run(self.root, now or self.now, self.fixtures)
        self.assertEqual((self.root/'config/collection-policy.json').read_bytes(), before_policy)
        self.assertEqual(implementation.read_archive(self.data, {'GH':self.country}), {self.original['id']:self.original})
        snapshot = implementation.load(self.data/'snapshot.json')
        self.assertEqual(snapshot['archiveTotal'], 1)
        self.assertEqual(snapshot['incidents'][0]['firstSeenAt'], self.original['firstSeenAt'])
        self.assertEqual(snapshot['incidents'][0]['reports'], self.original['reports'])
        self.assertEqual(snapshot['incidents'][0]['revisions'], self.original['revisions'])
        return audit, {row['sourceId']:row for row in snapshot['coverage']}, fetch.call_count

    def test_new_unadmitted_references_get_disabled_health_without_attempts_or_source_state(self):
        audit, coverage, calls = self.run_pipeline()
        self.assertEqual(calls, 0)
        self.assertEqual(audit['sources'], [])
        self.assertEqual(coverage['usgs'], self.health)
        self.assertEqual(set(coverage), {row['id'] for row in self.sources})
        for sid in ('new-reference', 'new-reviewed-feed'):
            self.assertEqual(coverage[sid]['status'], 'disabled')
            self.assertIsNone(coverage[sid]['lastAttemptAt'])
            self.assertIsNone(coverage[sid]['lastSuccessAt'])
        self.assertEqual(pipeline.load(self.data/'collector-state.json'), {})
        _, repeated, calls = self.run_pipeline(self.now+dt.timedelta(minutes=1))
        self.assertEqual(repeated, coverage)
        self.assertEqual(calls, 0)

    def test_admission_success_then_failure_records_only_real_attempts_and_preserves_evidence(self):
        self.run_pipeline()
        policy = dict(sourceId=self.feed['id'], mode='discovery', cadenceMinutes=15, url='https://example.org/feed', independentGroup=self.feed['id'])
        pipeline.save(self.root/'config/collection-policy.json', dict(sources=[policy]))
        path = self.fixtures/(self.feed['id']+'.response')
        path.write_bytes(b'<rss><channel><item><title>Community meeting</title><link>https://example.org/report</link><pubDate>Tue, 06 Oct 2026 07:30:00 GMT</pubDate></item></channel></rss>')
        audit, coverage, calls = self.run_pipeline()
        self.assertEqual(calls, 1)
        self.assertEqual(len(audit['sources']), 1)
        self.assertEqual(coverage[self.feed['id']]['status'], 'healthy')
        self.assertEqual(coverage[self.feed['id']]['lastAttemptAt'], pipeline.stamp(self.now))
        self.assertEqual(coverage[self.feed['id']]['lastSuccessAt'], pipeline.stamp(self.now))
        self.assertEqual(coverage['usgs'], self.health)
        self.assertEqual(coverage[self.reference['id']]['status'], 'disabled')
        self.assertEqual(pipeline.load(self.data/'discovery.json'), [])
        _, repeated, calls = self.run_pipeline(self.now+dt.timedelta(seconds=5))
        self.assertEqual(calls, 0)
        self.assertEqual(repeated, coverage)
        path.write_bytes(b'<rss>malformed')
        later = self.now+dt.timedelta(minutes=15)
        _, failed, calls = self.run_pipeline(later)
        self.assertEqual(calls, 1)
        self.assertEqual(failed[self.feed['id']]['status'], 'unavailable')
        self.assertEqual(failed[self.feed['id']]['lastAttemptAt'], pipeline.stamp(later))
        self.assertEqual(failed[self.feed['id']]['lastSuccessAt'], pipeline.stamp(self.now))
        self.assertEqual(failed[self.reference['id']], coverage[self.reference['id']])
        self.assertEqual(failed['usgs'], self.health)

    def test_first_admitted_failure_is_not_mislabeled_disabled_or_healthy(self):
        policy = dict(sourceId=self.feed['id'], mode='discovery', cadenceMinutes=15, url='https://example.org/feed')
        pipeline.save(self.root/'config/collection-policy.json', dict(sources=[policy]))
        _, coverage, calls = self.run_pipeline()
        self.assertEqual(calls, 1)
        self.assertEqual(coverage[self.feed['id']]['status'], 'unavailable')
        self.assertEqual(coverage[self.feed['id']]['lastAttemptAt'], pipeline.stamp(self.now))
        self.assertIsNone(coverage[self.feed['id']]['lastSuccessAt'])
        self.assertEqual(coverage[self.reference['id']]['status'], 'disabled')



if __name__ == '__main__':
    unittest.main()
