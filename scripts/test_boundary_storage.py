"""Lossless collector map storage; no coordinate simplification or archive loss."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import public_data_pipeline as pipeline
import public_data_publish as publisher


class BoundaryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'config/map-areas/GH-ADM1.geojson'
        self.path.parent.mkdir(parents=True)
        pipeline.save(self.root/'config/collection-policy.json', {'sources': []})
        self.layer = {'features': [{'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]},
                                   'properties': {'id': 'province', 'name': 'Example', 'parentId': 'country:GH'}}], 'source': {'version': 'fixture'}}

    def tearDown(self):
        self.temp.cleanup()

    def packed(self, raw):
        packed = self.path.with_suffix('.geojson.gz')
        packed.write_bytes(gzip.compress(raw, mtime=0))
        return packed

    def test_identical_json_and_administrative_assignment(self):
        raw = pipeline.encode(self.layer)
        self.path.write_bytes(raw)
        self.assertEqual(pipeline.load_boundary_layer(self.path), self.layer)
        self.path.unlink()
        packed = self.packed(raw)
        self.assertEqual(gzip.decompress(packed.read_bytes()), raw)
        self.assertEqual(pipeline.load_boundary_layer(self.path), self.layer)
        pipeline.save(self.root/'config/geo/index.json', [])
        geo = pipeline.Geography(self.root, {'GH': {}})
        row = {'countryCode': 'GH', 'location': {'precision': 'point', 'lat': 2, 'lon': 2}}
        geo.administrative(row)
        self.assertEqual(row['admin1Code'], 'province')
        self.assertEqual(row['adminAssignment']['levels']['ADM1']['source'], self.layer['source'])
        self.assertEqual(row['adminAssignment']['levels']['ADM2']['status'], 'unavailable')
        self.assertTrue(publisher.audit(self.root, remote=False)['passed'])

    def test_duplicate_representations_are_refused(self):
        self.path.write_bytes(pipeline.encode(self.layer))
        self.packed(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            pipeline.load_boundary_layer(self.path)

    def test_security_audit_scans_decompressed_content(self):
        self.packed(json.dumps({'notes': '/'+'Users'+'/example/private'}).encode())
        with self.assertRaisesRegex(ValueError, 'content scan'):
            publisher.audit(self.root, remote=False)

    def test_security_audit_also_scans_gzip_headers(self):
        packed = gzip.compress(pipeline.encode(self.layer), mtime=0)
        comment = ('/'+'Users'+'/header-only').encode() + b'\0'
        packed = packed[:3] + bytes([packed[3] | 0x10]) + packed[4:10] + comment + packed[10:]
        self.path.with_suffix('.geojson.gz').write_bytes(packed)
        self.assertEqual(pipeline.load_boundary_layer(self.path), self.layer)
        with self.assertRaisesRegex(ValueError, 'content scan'):
            publisher.audit(self.root, remote=False)

    def test_oversized_complete_payload_is_rejected_not_truncated(self):
        self.packed(b' '*25_000_001)
        with self.assertRaisesRegex(ValueError, 'exceeds 25MB'):
            pipeline.load_boundary_layer(self.path)
        with self.assertRaisesRegex(ValueError, 'exceeds 25MB'):
            publisher.audit(self.root, remote=False)

    def test_invalid_compressed_or_json_payload_is_refused(self):
        path = self.packed(b'{invalid')
        with self.assertRaises(ValueError):
            pipeline.load_boundary_layer(self.path)
        path.write_bytes(b'not gzip')
        with self.assertRaises(OSError):
            pipeline.load_boundary_layer(self.path)

    def test_compression_is_only_admitted_for_boundary_files(self):
        self.packed(pipeline.encode(self.layer)).rename(self.root/'config/other.json.gz')
        with self.assertRaisesRegex(ValueError, 'Unapproved compressed'):
            publisher.audit(self.root, remote=False)


if __name__ == '__main__':
    unittest.main()
