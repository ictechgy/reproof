import json
from pathlib import Path
import unittest
from unittest.mock import patch

from reproof.source_snapshot_json import bounded_json

ROOT = Path(__file__).resolve().parents[1]


class SourceSnapshotMetadataTests(unittest.TestCase):
    def test_preflight_rejects_allocation_shapes_before_json_decoder(self):
        samples = [b'['*4+b']'*4,
                   b'['+b'0,'*100+b'0]',
                   b'{"value":"'+b'x'*4097+b'"}',
                   b'{"value":'+b'9'*100+b'}']
        for raw in samples:
            with self.subTest(raw=raw[:30]), patch('reproof.source_snapshot_json.json.loads') as decoder:
                with self.assertRaises(ValueError):bounded_json(raw,max_items=1,max_depth=3)
                decoder.assert_not_called()

    def test_duplicate_nonfinite_and_malformed_metadata_rejected(self):
        for raw in (b'{"key":1,"key":2}', b'{"key":NaN}', b'{"key":Infinity}',
                    b'{"key":"broken}', b'{"key":[]'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                bounded_json(raw,max_items=2,max_depth=3)

    def test_unicode_and_escaped_delimiters_are_not_container_tokens(self):
        value = {'paths':['폴더/화면.swift','a\\b','braces{}[],:quote"']}
        for ascii_only in (True,False):
            raw = json.dumps(value,ensure_ascii=ascii_only).encode()
            self.assertEqual(bounded_json(raw,max_items=4,max_depth=2),value)

    def test_existing_transfer_and_selection_limits_are_unchanged(self):
        from reproof.execution.artifacts import BlobSet,ArtifactError
        from reproof.execution.wire import MAX_FILES,MAX_TRANSFER_BYTES,CHUNK_BYTES
        from reproof.source_input_policy import input_paths
        from reproof.core import ContractError
        self.assertEqual((MAX_FILES,MAX_TRANSFER_BYTES,CHUNK_BYTES),(1024,64*1024*1024,32*1024))
        with self.assertRaises(ContractError):input_paths([f'f{i}.swift' for i in range(901)])
        with self.assertRaises(ArtifactError):BlobSet(tuple((f'f{i}.swift',b'') for i in range(1025)))

    def test_portable_ci_requires_real_scale_and_contract_tests(self):
        inventory = json.loads((ROOT/'tests/release_inventory.json').read_text())
        goal = inventory['goals']['SOURCE_SNAPSHOT']
        self.assertTrue(goal['available'])
        modules = [name for check in goal['checks'] for name in check['command'][3:]]
        self.assertIn('tests.test_source_snapshot_scale',modules)
        self.assertIn('--goal SOURCE_SNAPSHOT_SCALE --effects filesystem,process',
                      (ROOT/'.github/workflows/portable-tests.yml').read_text())
        self.assertIn("if: matrix.python-version == '3.12'",
                      (ROOT/'.github/workflows/portable-tests.yml').read_text())
        macos = (ROOT/'.github/workflows/ios-sdk-compile.yml').read_text()
        self.assertIn('--goal SOURCE_SNAPSHOT_CONTRACTS',macos)
        self.assertIn('source-snapshot-contracts.json',macos)
        self.assertNotIn('--goal SOURCE_SNAPSHOT_SCALE',macos)


if __name__ == '__main__':unittest.main()
