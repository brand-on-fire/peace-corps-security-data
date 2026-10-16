import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import public_archive_storage as storage


class RetainedStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'retained-weather/TH.json'
        self.raw = '[{"id":"warning","text":"คำเตือน – lluvia","geometry":[[100.123456789,14.2]],"revisions":[{"text":"old"}]}]\n'.encode()
        self.packed = self.path.with_suffix('.json.gz')

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self.raw)

    def test_exact_unicode_coordinates_evidence_and_repeat(self):
        self.seed()
        storage.save(self.path, self.raw, compressed=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(storage.read_raw(self.path), self.raw)
        self.assertEqual(storage.load(self.path), json.loads(self.raw))
        content = self.packed.read_bytes()
        modified = self.packed.stat().st_mtime_ns
        storage.save(self.path, self.raw, compressed=True)
        self.assertEqual(self.packed.read_bytes(), content)
        self.assertEqual(self.packed.stat().st_mtime_ns, modified)

    def test_plain_legacy_and_explicit_lossless_return(self):
        self.seed()
        self.assertEqual(storage.read_raw(self.path), self.raw)
        storage.save(self.path, self.raw, compressed=True)
        storage.save(self.path, self.raw, compressed=False)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertFalse(self.packed.exists())

    def test_missing_is_distinct_from_corruption(self):
        self.assertEqual(storage.load(self.path, []), [])
        self.seed(); self.path.write_bytes(b'{')
        with self.assertRaises(ValueError): storage.load(self.path, [])
        self.path.unlink(); self.packed.write_bytes(b'bad gzip')
        with self.assertRaises(ValueError): storage.load(self.path, [])

    def test_duplicate_representations_are_not_silently_selected(self):
        self.seed(); self.packed.write_bytes(gzip.compress(self.raw))
        with self.assertRaisesRegex(ValueError, 'Ambiguous'): storage.load(self.path)

    def test_truncated_checksum_trailing_and_multiple_members_rejected(self):
        packed = gzip.compress(self.raw, mtime=0)
        bad_checksum = packed[:-8] + bytes([packed[-8] ^ 1]) + packed[-7:]
        for invalid in (packed[:-1], packed[:-8], bad_checksum, packed + b'x', packed + gzip.compress(b' ')):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(ValueError): storage.decode_gzip(invalid)

    def test_wire_and_expansion_bounds_do_not_accept_a_prefix(self):
        packed = gzip.compress(b'[' + b' ' * 1000 + b']')
        with patch.object(storage, 'MAX_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'Decoded archive exceeds'): storage.decode_gzip(packed)
            with self.assertRaisesRegex(ValueError, 'Compressed archive exceeds'): storage.decode_gzip(b'x' * 101)
            self.seed()
            with self.assertRaisesRegex(ValueError, 'Archive file exceeds'): storage.load(self.path)

    def test_invalid_new_content_does_not_change_existing_file(self):
        self.seed()
        for raw in (b'{', self.raw + b'incomplete', b'\xff'):
            with self.assertRaises(ValueError): storage.save(self.path, raw, compressed=True)
            self.assertEqual(self.path.read_bytes(), self.raw)
            self.assertFalse(self.packed.exists())

    def test_failed_replace_keeps_original_and_cleans_temp(self):
        self.seed()
        with patch.object(Path, 'replace', side_effect=OSError('Synthetic write failure')):
            with self.assertRaises(OSError): storage.save(self.path, self.raw, compressed=True)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_unreferenced_old_parts_are_preserved_byte_for_byte(self):
        self.seed(); old = self.path.with_name('TH-009.json'); old.write_bytes(self.raw)
        storage.compress_legacy_files(self.path.parent)
        for path in (self.path, old): self.assertEqual(storage.read_raw(path), self.raw)
        self.assertEqual(len(list(self.path.parent.glob('*.json.gz'))), 2)

    def test_unapproved_names_and_symlinks_fail_before_mutation(self):
        self.seed()
        for path in (self.root/'history/TH.json', self.path.with_name('TH-index.json'), self.path.with_name('TH-1.json')):
            with self.assertRaises(ValueError): storage.save(path, self.raw, compressed=True)
        self.packed.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'symlinks'): storage.load(self.path)
        with self.assertRaisesRegex(ValueError, 'symlinks'): storage.save(self.path, self.raw, compressed=True)
        self.assertEqual(self.path.read_bytes(), self.raw)


if __name__ == '__main__': unittest.main()
