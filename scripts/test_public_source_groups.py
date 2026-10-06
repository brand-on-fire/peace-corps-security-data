"""Offline publisher grouping and correction replay; no network requests."""
import copy
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import public_data_pipeline as pipeline


class SourceGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'docs/data'
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.now = dt.datetime(2026, 10, 6, 2, tzinfo=dt.timezone.utc)
        self.country = dict(code='JM', name='Jamaica', region='Caribbean', status='active', reviewDue='2026-11-04')
        self.group = 'news-jm-observer'
        ids = [self.group, self.group+'-west', self.group+'-central', 'news-jm-unrelated']
        self.sources = [dict(id=sid, kind='rss', language='en', countryCodes=['JM']) for sid in ids]
        self.policies = [dict(sourceId=sid, mode='discovery', cadenceMinutes=60,
                             url='https://publisher.example/'+sid+'/feed',
                             independentGroup=self.group if sid.startswith(self.group) else sid) for sid in ids]
        reviews = []
        for i, source in enumerate(self.sources):
            # Three category feeds repeat the identical article; one unrelated
            # publisher has a distinct article. Neither implies corroboration.
            url = 'https://publisher.example/story' if i < 3 else 'https://other.example/story'
            raw = (f'<rss><channel><item><title>Jamaica police investigate robbery</title>'
                   f'<link>{url}</link><guid>{url}</guid><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate>'
                   f'<description>Police said a robbery was reported on Monday.</description>'
                   f'</item></channel></rss>').encode()
            (self.fixtures / (source['id']+'.response')).write_bytes(raw)
            item = pipeline.parse_discovery_feed(raw, self.policies[i], source)[0]
            reviews.append(dict(sourceId=source['id'], decision='include', url=url,
                approvedItemHash=pipeline.digest(item), countryCode='JM', locationPrecision='country',
                summary='Police reported a robbery; investigation ongoing.', category='crime',
                eventTimeBasis='explicit article date', evidenceScope='complete reviewed feed text',
                reviewedAt='2026-10-05T12:00:00Z'))
        for name, value in {
            'config/countries.json': [self.country], 'config/sources.json': self.sources,
            'config/collection-policy.json': {'sources': self.policies},
            'config/country-iso3.json': {}, 'config/geo/index.json': [],
            'config/news-reviews.json': reviews,
            'docs/data/snapshot.json': {'coverage': [], 'history': {'from': '2021-10-05T00:00:00Z'}},
        }.items():
            pipeline.save(self.root/name, value)

    def tearDown(self):
        self.temp.cleanup()

    def run_collector(self):
        with patch.object(pipeline.urllib.request, 'build_opener', side_effect=AssertionError('No network')):
            return pipeline.run(self.root, self.now, self.fixtures)

    def archive(self):
        return pipeline.read_archive(self.data, {'JM': self.country})

    def test_related_feeds_share_one_group_and_duplicate_replay_is_stable(self):
        self.run_collector()
        records = self.archive()
        self.assertEqual(len(records), 2)
        self.assertEqual({r['reports'][0]['independentGroup'] for r in records.values()},
                         {self.group, 'news-jm-unrelated'})
        self.assertTrue(all(r['evidenceStatus'] == 'reported' for r in records.values()))
        self.assertTrue(all(not r.get('revisions') for r in records.values()))
        self.assertEqual(pipeline.load(self.data/'snapshot.json')['sources'], self.sources)
        self.now += dt.timedelta(hours=2)
        self.assertEqual(self.run_collector()['changedIncidents'], [])
        self.assertEqual(self.archive(), records)

    def test_group_correction_persists_once_and_retains_original_evidence(self):
        # A previous publication had separate category-feed identities.
        sid = self.sources[1]['id']
        policy = dict(self.policies[1]); policy.pop('independentGroup')
        pipeline.save(self.root/'config/collection-policy.json', {'sources': [policy]})
        self.run_collector()
        before = next(iter(self.archive().values()))
        self.assertEqual(before['reports'][0]['independentGroup'], sid)
        pipeline.save(self.root/'config/collection-policy.json', {'sources': self.policies})
        self.now += dt.timedelta(hours=2)
        self.run_collector()
        after = self.archive()[before['id']]
        self.assertEqual(after['reports'][0]['independentGroup'], self.group)
        self.assertEqual(after['firstSeenAt'], before['firstSeenAt'])
        self.assertEqual(after['revisions'][0]['previousReports'], before['reports'])
        self.assertEqual(len(after['revisions']), 1)
        self.now += dt.timedelta(hours=2)
        self.assertEqual(self.run_collector()['changedIncidents'], [])
        self.assertEqual(self.archive()[before['id']], after)

    def test_invalid_group_cannot_replace_previous_publication_even_on_304(self):
        self.run_collector()
        records = self.archive()
        states = pipeline.load(self.data/'collector-state.json')
        for invalid in ('unreviewed-publisher', '', None, ['news-jm-observer']):
            with self.subTest(group=invalid):
                policy = dict(self.policies[0], independentGroup=invalid)
                pipeline.save(self.root/'config/collection-policy.json', {'sources': [policy]})
                pipeline.save(self.data/'collector-state.json', copy.deepcopy(states))
                self.now += dt.timedelta(hours=2)
                with patch.object(pipeline, 'fetch', return_value=(dict(httpStatus=304), None)):
                    pipeline.run(self.root, self.now, self.fixtures)
                self.assertEqual(self.archive(), records)
                new = pipeline.load(self.data/'collector-state.json')[self.group]
                self.assertEqual(new['status'], 'unavailable')
                self.assertEqual(new['lastSuccessAt'], states[self.group]['lastSuccessAt'])
                self.assertIn('not a reviewed registry identity', new['message'])


if __name__ == '__main__':
    unittest.main()
