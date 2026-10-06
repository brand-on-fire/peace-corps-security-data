"""Offline end-to-end metadata collection; no HTTP or incident promotion."""
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import public_data_pipeline as pipeline


def document(title='Fiji police investigate robbery', url='https://fijisun.com.fj/news/robbery'):
    return (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            f'xmlns:n="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>{url}</loc>'
            f'<n:news><n:title>{title}</n:title><n:publication_date>2026-10-05T05:23:45.123Z'
            f'</n:publication_date></n:news></url></urlset>').encode()


class NewsSitemapPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'docs/data'
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.now = dt.datetime(2026, 10, 6, 6, tzinfo=dt.timezone.utc)
        self.country = dict(code='FJ', name='Fiji', region='Pacific', status='active', reviewDue='2026-11-04')
        self.source = dict(id='news-fj-fiji-sun', kind='rss', language='en', countryCodes=['FJ'])
        self.policy = dict(sourceId=self.source['id'], mode='discovery', cadenceMinutes=360,
                           url='https://fijisun.com.fj/news-sitemap.xml', format='google-news-sitemap')
        self.original = pipeline.make_incident(self.source, 'existing-reviewed-record',
            'Prior complete reviewed evidence', 'Original unabridged text. 日本語. '*100,
            self.country, 'crime', '2026-10-04T10:00:00Z', '2026-10-05T10:00:00.685Z',
            '2026-10-05T11:00:00Z', {'precision': 'country'},
            'https://fijisun.com.fj/news/prior', {'evidenceScope': 'reviewed article'}, 'Original source')
        self.previous = dict(sourceId=self.source['id'], lastAttemptAt='2026-10-05T18:00:00Z',
                             lastSuccessAt='2026-10-05T18:00:00Z', status='healthy', etag='prior-good')
        pipeline.write_archive(self.data, {self.original['id']: self.original}, {'FJ': self.country})
        for file, value in {
            'config/countries.json': [self.country], 'config/sources.json': [self.source],
            'config/collection-policy.json': {'sources': [self.policy]}, 'config/country-iso3.json': {},
            'config/geo/index.json': [], 'config/news-reviews.json': [],
            'docs/data/snapshot.json': {'coverage': [], 'history': {'from': '2021-10-05T00:00:00Z'}},
            'docs/data/collector-state.json': {self.source['id']: self.previous},
        }.items():
            pipeline.save(self.root / file, value)

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, body):
        (self.fixtures / (self.source['id'] + '.response')).write_bytes(body)
        with patch.object(pipeline.urllib.request, 'build_opener', side_effect=AssertionError('HTTP forbidden in offline test')):
            result = pipeline.run(self.root, self.now, self.fixtures)
        self.assertEqual(pipeline.read_archive(self.data, {'FJ': self.country}), {self.original['id']: self.original})
        return result, pipeline.load(self.data / 'collector-state.json')[self.source['id']]

    def test_full_title_metadata_remains_discovery_even_with_matching_approval(self):
        title = 'Fiji police investigate robbery: ' + ('Complete headline 日本語. '*600).rstrip()
        body = document(title)
        item = pipeline.parse_news_sitemap(body, {'fijisun.com.fj'})[0]
        pipeline.save(self.root / 'config/news-reviews.json', [dict(
            sourceId=self.source['id'], decision='include', url=item['url'],
            approvedItemHash=pipeline.digest(item), countryCode='FJ', locationPrecision='country',
            summary='This metadata-only representation must never become an incident.')])
        with patch.object(pipeline, 'reviewed_news_incidents', side_effect=AssertionError('Metadata must not enter promotion')):
            result, state = self.collect(body)
        rows = pipeline.load(self.data / 'discovery.json')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], title)
        self.assertEqual(rows[0]['contentHash'], pipeline.digest(item))
        self.assertEqual(rows[0]['status'], 'needs-review')
        self.assertEqual(rows[0]['locationStatus'], 'unverified')
        self.assertNotIn('countryCode', rows[0])
        self.assertNotIn('location', rows[0])
        self.assertEqual(result['changedIncidents'], [])
        self.assertEqual(state['acceptedCount'], 0)
        self.assertEqual(state['status'], 'healthy')
        self.assertEqual(state['bytes'], len(body))

    def test_unsafe_second_link_rejects_complete_response_not_valid_prefix(self):
        for url in ('http://fijisun.com.fj/a', 'https://attacker.example/a',
                    'https://user:pass@fijisun.com.fj/a', 'javascript:alert(1)',
                    'https://fijisun.com.fj:8443/a', 'https://fijisun.com.fj/a bad'):
            with self.subTest(url=url):
                # Reset attempt state so every malformed fixture is actually read.
                pipeline.save(self.data / 'collector-state.json', {self.source['id']: self.previous})
                second = document(url=url).split(b'<url>')[1].split(b'</url>')[0]
                body = document().replace(b'</urlset>', b'<url>' + second + b'</url></urlset>')
                result, state = self.collect(body)
                self.assertEqual(result['discoveryCount'], 0)
                self.assertEqual(state['status'], 'unavailable')
                self.assertEqual(state['lastSuccessAt'], self.previous['lastSuccessAt'])
                self.assertEqual(state['etag'], self.previous['etag'])

    def test_oversized_full_input_is_rejected_before_parser_and_preserves_archive(self):
        body = document('Robbery ' + 'x'*5000)
        with patch.object(pipeline, 'MAX_RESPONSE', len(body)-1), \
             patch.object(pipeline, 'parse_news_sitemap', side_effect=AssertionError('Oversized body must not be parsed')):
            result, state = self.collect(body)
        self.assertEqual(result['discoveryCount'], 0)
        self.assertEqual(state['status'], 'unavailable')
        self.assertIn('entire response rejected', state['message'])
        self.assertEqual(state['lastSuccessAt'], self.previous['lastSuccessAt'])
        with patch.object(pipeline, 'MAX_RESPONSE', len(body)-1):
            with self.assertRaisesRegex(ValueError, 'entire response rejected'):
                pipeline.read_complete(io.BytesIO(body))

    def test_malformed_tail_rejects_prefix_and_retains_previous_discoveries(self):
        self.collect(document())
        previous = pipeline.load(self.data / 'discovery.json')
        pipeline.save(self.data / 'collector-state.json', {self.source['id']: self.previous})
        result, state = self.collect(document() + b'<incomplete')
        self.assertEqual(state['status'], 'unavailable')
        self.assertEqual(pipeline.load(self.data / 'discovery.json'), previous)
        self.assertEqual(result['changedIncidents'], [])

    def test_unknown_format_cannot_fall_back_to_rss_even_on_304(self):
        for unknown in ('google-news-siteamp', '', None):
            with self.subTest(format=unknown):
                policy = dict(self.policy, format=unknown)
                pipeline.save(self.root / 'config/collection-policy.json', {'sources': [policy]})
                pipeline.save(self.data / 'collector-state.json', {self.source['id']: self.previous})
                response = dict(self.previous, lastAttemptAt=pipeline.stamp(self.now), httpStatus=304)
                with patch.object(pipeline, 'fetch', return_value=(response, None)):
                    pipeline.run(self.root, self.now, self.fixtures)
                state = pipeline.load(self.data / 'collector-state.json')[self.source['id']]
                self.assertEqual(state['status'], 'unavailable')
                self.assertIn('no reviewed adapter', state['message'])
                self.assertEqual(state['lastSuccessAt'], self.previous['lastSuccessAt'])
        with self.assertRaisesRegex(ValueError, 'no reviewed adapter'):
            pipeline.parse_discovery_feed(b'<rss/>', dict(self.policy, format='unknown'), self.source)

    def test_legacy_missing_format_still_reads_rss(self):
        policy = {key: value for key, value in self.policy.items() if key != 'format'}
        self.assertEqual(pipeline.parse_discovery_feed(b'<rss><channel/></rss>', policy, self.source), [])


if __name__ == '__main__':
    unittest.main()
