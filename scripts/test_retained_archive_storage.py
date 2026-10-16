"""Complete archive, warning lifecycle, audit and rollback checks for compression."""
import datetime as dt
import gzip
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import public_archive_storage as storage
import public_cap as cap
import public_data_pipeline as pipeline
import public_data_publish as publisher
import public_data_refresh as refresh
import test_public_cap as cap_fixtures

NOW = '2026-10-16T04:40:00Z'
COUNTRY = dict(code='TH', name='Thailand', status='active', reviewDue='2026-11-04')
SOURCE = dict(id='cap-th-fixture', countryCodes=['TH'], kind='cap', tier='official', enabled=True)
URL = 'https://warnings.example.test/alert.xml'


class RetainedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)/'collector'
        self.data = self.root/'docs/data'
        self.baseline = Path(self.temporary.name)/'baseline'
        self.countries = {'TH': COUNTRY}
        for name, value in [('countries.json', [COUNTRY]), ('sources.json', [SOURCE]),
                            ('collection-policy.json', {'sources': []}), ('geo/index.json', []),
                            ('country-iso3.json', {'THA': 'TH'}), ('admin-places.json', [])]:
            pipeline.save(self.root/'config'/name, value)
        self.rows = {}
        for identity, severity in [('routine', 'Moderate'), ('severe', 'Severe')]:
            row = self.row(identity, severity)
            self.rows[row['id']] = row
        snapshot = dict(schemaVersion=1, mode='live', generatedAt=NOW, countries=[COUNTRY], sources=[SOURCE],
                        coverage=[], incidents=pipeline.current_snapshot_rows(self.rows, self.countries),
                        archiveTotal=1, history={'from':'2025-10-16T00:00:00Z','to':NOW,'recordCount':1,'sources':[SOURCE['id']],'notes':[]})
        snapshot['publication'] = dict(format='peace-corps-public-data-v1', generatedAt=NOW,
                                       revision=pipeline.publication_revision(snapshot), activeCountryCodes=['TH'])
        self.snapshot = snapshot
        pipeline.save(self.data/'snapshot.json', snapshot)
        self.write()
        shutil.copytree(self.data, self.baseline)

    def tearDown(self):
        self.temporary.cleanup()

    def row(self, identifier, severity):
        raw = cap_fixtures.CapPartialTests().bulletin(identifier=identifier, severity=severity)
        row = cap.parse_alert(SOURCE, raw, URL, NOW, self.countries, [])['record']
        pipeline.annotate_weather(row)
        return row

    def write(self, compressed=False):
        pipeline.write_archive(self.data, self.rows, self.countries, publication=self.snapshot['publication'],
                               compress_retained=compressed)

    def public_files(self):
        return {str(p.relative_to(self.data)):p.read_bytes() for p in self.data.rglob('*')
                if p.is_file() and p.relative_to(self.data).parts[0] != 'retained-weather'}

    def test_encoding_migration_preserves_records_public_bytes_and_canonical_gate(self):
        before = self.public_files()
        original = (self.data/'retained-weather/TH.json').read_bytes()
        self.write(True)
        self.assertEqual(storage.read_raw(self.data/'retained-weather/TH.json'), original)
        self.assertEqual(pipeline.read_archive(self.data, self.countries), self.rows)
        self.assertEqual(self.public_files(), before)
        self.assertEqual(refresh.validate_candidate(self.root, self.baseline), self.snapshot)
        first = {p.name:p.read_bytes() for p in (self.data/'retained-weather').iterdir()}
        self.write(True)
        self.assertEqual({p.name:p.read_bytes() for p in (self.data/'retained-weather').iterdir()}, first)
        self.assertTrue(publisher.audit(self.root, remote=False)['passed'])

    def test_compressed_multipart_and_unreferenced_parts_survive(self):
        self.rows = {r['id']:r for r in [self.row(str(i), 'Moderate') for i in range(3)]}
        orphan = self.data/'retained-weather/TH-009.json'
        raw = pipeline.encode([self.row('old', 'Moderate')]); orphan.write_bytes(raw)
        limit = max(len(pipeline.encode(row)) for row in self.rows.values()) + 50
        with patch.object(pipeline, 'PART_LIMIT', limit): self.write(True)
        manifest = storage.load(self.data/'retained-weather/TH.json')
        self.assertEqual(len(manifest['parts']), 3)
        self.assertEqual(pipeline.read_archive(self.data, self.countries), self.rows)
        self.assertEqual(storage.read_raw(orphan), raw)
        (self.data/'retained-weather'/manifest['parts'][0]).with_suffix('.json.gz').unlink()
        with self.assertRaises((ValueError, TypeError)): pipeline.read_archive(self.data, self.countries)

    def test_later_severe_update_can_use_retained_compressed_evidence(self):
        self.write(True)
        prior = pipeline.read_archive(self.data, self.countries)
        routine = next(row for row in prior.values() if row['severity'] == 'moderate')
        updated = cap_fixtures.CapPartialTests().bulletin(identifier='upgrade', severity='Severe', kind='Update',
                    sent='2026-10-13T05:00:00Z', reference='issuer@example.test,routine,2026-10-13T04:00:00Z')
        parsed = cap.parse_alert(SOURCE, updated, URL, NOW, self.countries, [])
        changed = cap.apply_lifecycle([parsed], prior, NOW)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['id'], routine['id'])
        self.assertIn(routine['reports'][0], changed[0]['reports'])
        self.rows = dict(prior, **{changed[0]['id']:changed[0]})
        self.write(True)
        self.assertEqual(pipeline.read_archive(self.data, self.countries), self.rows)
        self.assertEqual(storage.load(self.data/'retained-weather/TH.json'), [])
        self.assertEqual(len(pipeline.load(self.data/'history/TH.json')), 2)

    def test_gzip_is_opt_in_and_unknown_policy_fails_before_collection(self):
        policy_path = self.root/'config/collection-policy.json'
        for choice in ['unreviewed', True]:
            pipeline.save(policy_path, {'sources': [], 'policy': {'retainedWeatherStorage': choice}})
            with patch.object(pipeline, 'fetch', side_effect=AssertionError('Unexpected HTTP')):
                with self.assertRaisesRegex(ValueError, 'Unknown retained-weather storage policy'):
                    pipeline.run(self.root, dt.datetime.fromisoformat(NOW))
        self.assertTrue((self.data/'retained-weather/TH.json').exists())
        pipeline.save(policy_path, {'sources': [], 'policy': {'retainedWeatherStorage': 'gzip-v1'}})
        with patch.object(pipeline, 'fetch', side_effect=AssertionError('Unexpected HTTP')):
            pipeline.run(self.root, dt.datetime.fromisoformat('2026-10-16T04:40:01Z'))
        self.assertFalse((self.data/'retained-weather/TH.json').exists())
        self.assertEqual(pipeline.read_archive(self.data, self.countries), self.rows)
        refresh.validate_candidate(self.root, self.baseline)

    def test_corrupt_gzip_candidate_cannot_replace_last_good_publication(self):
        prior = self.public_files()
        original = (self.data/'retained-weather/TH.json').read_bytes()
        def runner(command, **kwargs):
            stage = Path(command[command.index('--root')+1])
            pipeline.save(stage/'config/collection-policy.json', {'sources': [], 'policy': {'retainedWeatherStorage': 'gzip-v1'}})
            pipeline.run(stage, dt.datetime.fromisoformat('2026-10-16T04:40:01Z'))
            packed = stage/'docs/data/retained-weather/TH.json.gz'
            packed.write_bytes(packed.read_bytes()[:-1])
        result = refresh.refresh(self.root, dt.datetime.fromisoformat('2026-10-16T04:40:01Z'), runner=runner, environment={})
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual((self.data/'retained-weather/TH.json').read_bytes(), original)
        self.assertEqual({k:v for k,v in self.public_files().items() if k != 'refresh-status.json'}, prior)

    def test_publisher_scans_decoded_bytes_and_rejects_unapproved_gzip(self):
        self.write(True)
        packed = self.data/'retained-weather/TH.json.gz'
        packed.write_bytes(gzip.compress(b'["' + b'ghp_' + b'a'*35 + b'"]'))
        with self.assertRaisesRegex(ValueError, 'Public-content scan failed'): publisher.audit(self.root, remote=False)
        self.write(True)
        (self.data/'history/TH.json.gz').write_bytes(gzip.compress(b'[]'))
        with self.assertRaisesRegex(ValueError, 'Unapproved compressed public file'): publisher.audit(self.root, remote=False)


if __name__ == '__main__': unittest.main()
