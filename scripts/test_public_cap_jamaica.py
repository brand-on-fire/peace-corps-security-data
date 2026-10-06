"""Offline provider fixtures plus explicitly synthetic lifecycle transitions."""
import copy
import pathlib
import unittest
import xml.etree.ElementTree as ET

from public_cap import collect_cap, apply_lifecycle, expire_warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests/fixtures/cap'
if not FIXTURES.exists():
    FIXTURES = ROOT / 'scripts/fixtures/cap'
EMPTY = (FIXTURES / 'jm-smartalert-empty.atom').read_bytes()
WARNINGS = [(FIXTURES / name).read_bytes() for name in (
    'jm-smartalert-wind.xml', 'jm-smartalert-thunderstorm.xml')]
SOURCE = {'id': 'cap-jm-smartalert', 'countryCodes': ['JM']}
POLICY = {'url': 'https://alert.metservice.gov.jm/capfeed.php',
          'format': 'jamaica-cap-atom', 'bulletinHosts': ['alert.metservice.gov.jm']}
COUNTRIES = {'JM': {'code': 'JM', 'name': 'Jamaica'}}
URLS = ['https://alert.metservice.gov.jm/data/publishedCap/wind.xml',
        'https://alert.metservice.gov.jm/data/publishedCap/thunderstorm.xml']
ATOM = 'http://www.w3.org/2005/Atom'
CAP = 'urn:oasis:names:tc:emergency:cap:1.2'
NOW = '2026-10-06T01:34:05Z'


def feed(urls):
    root = ET.Element('{'+ATOM+'}feed')
    for url in urls:
        entry = ET.SubElement(root, '{'+ATOM+'}entry')
        ET.SubElement(entry, '{'+ATOM+'}title').text = 'Provider warning'
        ET.SubElement(entry, '{'+ATOM+'}link', href=url, type='application/cap+xml')
    return ET.tostring(root)


class JamaicaCapTests(unittest.TestCase):
    def collect(self, raw=EMPTY, bodies=None, now=NOW, policy=None):
        self.calls = []
        def fetch(child, *_args):
            self.calls.append(child['url'])
            body = (bodies or {}).get(child['url'])
            return {'message': 'fixture unavailable'}, body
        return collect_cap(SOURCE, policy or POLICY, raw, now, COUNTRIES, [], fetch)

    def test_real_empty_placeholder_fetches_nothing_and_creates_nothing(self):
        self.assertEqual(self.collect(), [])
        self.assertEqual(self.calls, [])

    def test_exact_placeholder_mixed_with_warnings_retains_all_warnings(self):
        root = ET.fromstring(EMPTY)
        for entry in ET.fromstring(feed(URLS)):
            root.append(entry)
        raw = ET.tostring(root)
        messages = self.collect(raw, dict(zip(URLS, WARNINGS)))
        self.assertCountEqual(self.calls, URLS)
        self.assertEqual(len(messages), 2)
        import hashlib
        for message in messages:
            self.assertTrue(message['record']['sourceMetadata']['capIndexInconsistentNoActivePlaceholder'])
            self.assertEqual(message['record']['sourceMetadata']['capIndexContentHash'], hashlib.sha256(raw).hexdigest())
        self.assertEqual([len(m['record']['location']['geometry']['coordinates']) for m in messages], [2, 12])

    def test_changed_or_malformed_placeholder_fails_closed(self):
        for change in ('title', 'summary', 'id', 'link', 'second-link'):
            with self.subTest(change=change):
                root = ET.fromstring(EMPTY)
                entry = root.find('{*}entry')
                if change == 'second-link':
                    ET.SubElement(entry, '{'+ATOM+'}link', href=POLICY['url'])
                elif change == 'link':
                    entry.find('{*}link').set('href', 'https://unreviewed.example/capfeed.php')
                else:
                    entry.find('{*}'+change).text = 'changed'
                with self.assertRaises(ValueError):
                    self.collect(ET.tostring(root))
                self.assertEqual(self.calls, [])

    def test_format_cannot_enable_other_endpoints_or_non_atom(self):
        with self.assertRaises(ValueError):
            self.collect(policy={**POLICY, 'url': 'https://unreviewed.example/capfeed.php'})
        with self.assertRaises(ValueError):
            self.collect(b'<rss><channel/></rss>')

    def test_all_real_expired_warnings_keep_every_polygon(self):
        messages = self.collect(feed(URLS), dict(zip(URLS, WARNINGS)))
        self.assertCountEqual(self.calls, URLS)
        self.assertEqual(len(messages), 2)
        self.assertEqual([len(m['record']['location']['geometry']['coordinates']) for m in messages], [2, 12])
        for raw, message in zip(WARNINGS, messages):
            row = message['record']
            expected = []
            for polygon in ET.fromstring(raw).findall('.//{*}info/{*}area/{*}polygon'):
                expected.append([[[float(lon), float(lat)] for lat, lon in
                                  (pair.split(',') for pair in polygon.text.split())]])
            self.assertEqual(row['location'], {'precision': 'area', 'geometry': {
                'type': 'MultiPolygon', 'coordinates': expected}})
            self.assertEqual(row['state'], 'expired')
            self.assertTrue(row['sourceMetadata']['warningNotConfirmedImpact'])
            self.assertNotIn('admin1Code', row)
            self.assertNotIn('admin2Code', row)

    def test_no_active_placeholder_does_not_cancel_unexpired_retained_warning(self):
        earlier = '2026-10-04T14:00:00Z'
        row = self.collect(feed(URLS[:1]), {URLS[0]: WARNINGS[0]}, earlier)[0]['record']
        before = copy.deepcopy(row)
        records = {row['id']: row}
        self.assertEqual(row['state'], 'reported')
        self.assertEqual(apply_lifecycle(self.collect(), records, earlier), [])
        expire_warnings(records, earlier)
        self.assertEqual(records[row['id']], before)
        expire_warnings(records, NOW)
        self.assertEqual(row['state'], 'expired')
        self.assertEqual(row['location'], before['location'])

    def test_unsafe_active_url_rejected_before_any_fetch(self):
        for url in ('https://unreviewed.example/a.xml',
                    'https://user@alert.metservice.gov.jm/a.xml',
                    'http://alert.metservice.gov.jm/a.xml'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.collect(feed([URLS[0], url]), dict(zip(URLS, WARNINGS)))
            self.assertEqual(self.calls, [])

    def test_failed_or_malformed_second_bulletin_retains_prior_records(self):
        row = self.collect(feed(URLS[:1]), {URLS[0]: WARNINGS[0]})[0]['record']
        records = {row['id']: row}
        before = copy.deepcopy(records)
        for bad in (None, b'not CAP XML'):
            with self.subTest(bad=bad), self.assertRaises((ValueError, ET.ParseError)):
                # Same order as the collector: apply only after complete collect succeeds.
                messages = self.collect(feed(URLS), {URLS[0]: WARNINGS[0], URLS[1]: bad})
                for updated in apply_lifecycle(messages, records, NOW):
                    records[updated['id']] = updated
            self.assertEqual(records, before)

    def test_synthetic_update_and_cancel_preserve_geometry_and_identity(self):
        original = self.collect(feed(URLS[:1]), {URLS[0]: WARNINGS[0]})[0]
        row = original['record']
        root = ET.fromstring(WARNINGS[0])
        reference = ','.join(root.findtext('{*}'+key) for key in ('sender', 'identifier', 'sent'))
        root.find('{*}msgType').text = 'Update'
        root.find('{*}identifier').text = 'synthetic-reviewed-update'
        root.find('{*}sent').text = '2026-10-04T14:00:00Z'
        ET.SubElement(root, '{'+CAP+'}references').text = reference
        update = self.collect(feed(URLS[:1]), {URLS[0]: ET.tostring(root)})
        changed = apply_lifecycle(update, {row['id']: row}, NOW)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['id'], row['id'])
        self.assertEqual(changed[0]['location'], row['location'])
        updated = changed[0]
        update_reference = ','.join(root.findtext('{*}'+key) for key in ('sender', 'identifier', 'sent'))
        root.find('{*}msgType').text = 'Cancel'
        root.find('{*}identifier').text = 'synthetic-reviewed-cancel'
        root.find('{*}sent').text = '2026-10-04T15:00:00Z'
        root.find('{*}references').text = update_reference
        for info in root.findall('{*}info'):
            root.remove(info)
        cancel = self.collect(feed(URLS[:1]), {URLS[0]: ET.tostring(root)})
        changed = apply_lifecycle(cancel, {updated['id']: updated}, NOW)
        self.assertEqual(changed[0]['state'], 'cancelled')
        self.assertEqual(changed[0]['id'], row['id'])
        self.assertEqual(changed[0]['location'], row['location'])
        self.assertEqual(len(changed[0]['reports']), 2)
        self.assertEqual(apply_lifecycle(cancel, {row['id']: changed[0]}, NOW), [])


if __name__ == '__main__':
    unittest.main()
