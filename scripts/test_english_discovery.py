"""Crime and road-report selection creates review candidates, never incidents."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import public_data_pipeline as pipeline


class EnglishDiscoveryTests(unittest.TestCase):
    def discover(self, title):
        source = dict(id='reviewed-publisher', countryCodes=['KE'], language='en', tier='official')
        items = [dict(title=title, description='', url='https://publisher.example/report',
                      publishedAt='2026-10-05T14:00:00Z')]
        before = copy.deepcopy(items)
        result = pipeline.news_discovery(source, items, '2026-10-06T02:30:00Z',
                                         {'KE': {'code': 'KE', 'name': 'Kenya'}})
        self.assertEqual(items, before)
        return result

    def test_phone_snatching_remains_an_attributed_review_candidate(self):
        title = 'Two suspected phone snatchers arrested in Kasarani'
        row = self.discover(title)[0]
        self.assertEqual(row['title'], title)
        self.assertEqual(row['categoryCandidates'], ['crime'])
        self.assertEqual(row['status'], 'needs-review')
        self.assertEqual(row['locationStatus'], 'unverified')
        for key in ('occurredAt', 'severity', 'countryCode', 'admin1Code', 'admin2Code'):
            self.assertNotIn(key, row)

    def test_phone_snatching_variants(self):
        for title in ('Police investigate phone-snatching', 'Two suspects snatched a phone',
                      'Witness reports a phone snatcher', 'Police investigate snatching phones'):
            with self.subTest(title=title):
                self.assertEqual(self.discover(title)[0]['categoryCandidates'], ['crime'])

    def test_road_context_in_either_order(self):
        for title in ('A bus was involved in a collision', 'Crash on the highway',
                      'Two vehicles collided in a road accident', 'Matatu crashes into lorry',
                      'Collision involving two buses'):
            with self.subTest(title=title):
                self.assertEqual(self.discover(title)[0]['categoryCandidates'], ['transport'])

    def test_unrelated_crash_or_snatching_is_not_selected(self):
        for title in ('Market crash threatens investments', 'Team snatches victory in final',
                      'Phone launch impresses fans', 'Vehicle for political change announced',
                      'Accident of history explored', 'Nithi Bridge project advances',
                      'Heavy traffic on Thika Road'):
            with self.subTest(title=title):
                self.assertEqual(self.discover(title), [])

    def test_transport_context_requires_complete_words(self):
        for title in ('Business crashes after losses', 'Roadmap crashes into reality',
                      'Accident of history explored in Madagascar'):
            with self.subTest(title=title):
                self.assertEqual(self.discover(title), [])


if __name__ == '__main__':
    unittest.main()
