import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.source_snapshot_cli import load_selection, main


class SourceSnapshotCliTests(unittest.TestCase):
    def test_selection_is_closed_bounded_and_not_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); selection = root/'selection.json'
            document = {'schemaVersion': 1, 'kind': 'reproof-source-selection', 'paths': ['src/One.swift']}
            selection.write_text(json.dumps(document))
            self.assertEqual(load_selection(selection), document['paths'])
            for raw in ('{"schemaVersion":1,"schemaVersion":1}',
                        json.dumps(dict(document, discover=True)),
                        json.dumps(dict(document, schemaVersion=True)),
                        json.dumps(dict(document, paths=[])),
                        json.dumps(dict(document, paths=['a']*32769))):
                selection.write_text(raw)
                with self.assertRaises(ContractError):load_selection(selection)
            selection.write_text(json.dumps(document))
            link = root/'link.json';link.symlink_to(selection)
            with self.assertRaises(ContractError):load_selection(link)
            hard = root/'hard.json';os.link(selection, hard)
            with self.assertRaises(ContractError):load_selection(selection)

    def test_cli_roundtrip_is_local_and_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();source = root/'source';source.mkdir()
            (source/'chosen.swift').write_bytes(b'plain synthetic bytes')
            (source/'not-selected.swift').write_bytes(b'not selected')
            (source/'.env').write_bytes(b'synthetic credential exclusion marker')
            selection = root/'selection.json'
            selection.write_text(json.dumps({'schemaVersion':1,'kind':'reproof-source-selection',
                                             'paths':['chosen.swift']}))
            snapshot = root/'snapshot';copy = root/'copy'
            actions = [
                ['capture','--source',str(source),'--selection',str(selection),'--output',str(snapshot)],
                ['verify','--snapshot',str(snapshot)],
                ['materialize','--snapshot',str(snapshot),'--output',str(copy)],
            ]
            results = []
            with patch('subprocess.Popen', side_effect=AssertionError('No process execution')), \
                    patch('socket.socket', side_effect=AssertionError('No network access')):
                for arguments in actions:
                    stream = io.StringIO()
                    with contextlib.redirect_stdout(stream):
                        self.assertEqual(main(arguments), 0)
                    result = json.loads(stream.getvalue());results.append(result)
                    self.assertEqual(result['status'], 'complete')
                    self.assertNotIn('chosen.swift', stream.getvalue())
                    self.assertNotIn('plain synthetic', stream.getvalue())
            self.assertEqual(len({result['digest'] for result in results}), 1)
            self.assertEqual((copy/'chosen.swift').read_bytes(), (source/'chosen.swift').read_bytes())
            self.assertFalse((copy/'not-selected.swift').exists())
            self.assertFalse((copy/'.env').exists())

    def test_cli_invalid_budget_or_selection_never_creates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();output = root/'output';source = root/'source';source.mkdir()
            selection = root/'selection.json'
            selection.write_text(json.dumps({'schemaVersion':1,'kind':'reproof-source-selection',
                                             'paths':['../escape']}))
            for extra in ([], ['--max-files','32769'], ['--max-bytes',str(256*1024*1024+1)]):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(main(['capture','--source',str(source),'--selection',str(selection),
                                           '--output',str(output),*extra]),2)
                self.assertFalse(output.exists())
                self.assertNotIn('../escape',stream.getvalue())

    def test_main_dispatches_without_native_tools(self):
        from reproof.cli import main as root_main
        with patch('reproof.source_snapshot_cli.main', return_value=7) as handler:
            self.assertEqual(root_main(['source-snapshot','verify','--snapshot','synthetic']),7)
        handler.assert_called_once_with(['verify','--snapshot','synthetic'])


if __name__ == '__main__':unittest.main()
