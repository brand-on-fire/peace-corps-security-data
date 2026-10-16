"""Offline complete-record partition and exact-byte routing index checks."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import public_data_pipeline as pipeline


class ArchivePartIndexTests(unittest.TestCase):
    def rows(self):
        return {str(n): dict(id=str(n), countryCode='GH', occurredAt='2026-10-05T00:00:00Z',
                            category='crime', reports=[dict(excerpt='évidence entière '*15)],
                            summary='Full evidence '+str(n), sourceMetadata={'full': [1, 2, 3]}) for n in range(6)}

    def test_complete_sidecar_matches_every_exact_part_and_public_index(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, 'PART_LIMIT', 1000):
            data=Path(tmp);rows=self.rows();publication={'revision':'a'*64}
            pipeline.write_archive(data,rows,{'GH':{}},publication=publication)
            manifest=pipeline.load(data/'history/GH.json');sidecar=pipeline.load(data/'history/GH-index.json')
            self.assertEqual(sidecar['publicationRevision'],publication['revision'])
            self.assertEqual(sidecar['recordCount'],len(rows))
            self.assertEqual(manifest['parts'],[p['path'] for p in sidecar['parts']])
            recovered=[]
            for part in sidecar['parts']:
                raw=(data/'history'/part['path']).read_bytes();contents=pipeline.load(data/'history'/part['path'])
                self.assertEqual(part['bytes'],len(raw));self.assertEqual(part['sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(part['ids'],[r['id'] for r in contents]);self.assertEqual(part['recordCount'],len(contents))
                recovered.extend(contents)
            self.assertEqual({r['id']:r for r in recovered},rows)
            self.assertEqual([r['id'] for r in recovered],[r['id'] for r in pipeline.load(data/'history.json')])

    def test_republication_changes_revision_without_changing_evidence_part_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, 'PART_LIMIT', 1000):
            data=Path(tmp);rows=self.rows()
            pipeline.write_archive(data,rows,{'GH':{}},publication={'revision':'a'*64})
            prior=pipeline.load(data/'history/GH-index.json')
            pipeline.write_archive(data,rows,{'GH':{}},publication={'revision':'b'*64})
            current=pipeline.load(data/'history/GH-index.json')
            self.assertEqual(prior['parts'],current['parts']);self.assertEqual(current['publicationRevision'],'b'*64)

    def test_unpublished_and_single_part_archives_retain_legacy_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);pipeline.write_archive(data,self.rows(),{'GH':{}},publication={'revision':'a'*64})
            self.assertIsInstance(pipeline.load(data/'history/GH.json'),list)
            self.assertFalse((data/'history/GH-index.json').exists())
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline,'PART_LIMIT',1000):
            data=Path(tmp);pipeline.write_archive(data,self.rows(),{'GH':{}})
            self.assertFalse((data/'history/GH-index.json').exists())

if __name__=='__main__':
    unittest.main()
