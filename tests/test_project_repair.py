"""General product edits preserve the exact approved build and protected inputs."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from reproloop.execution.artifacts import BlobSet
from tests.test_fixture_allocations import project_document


PRODUCT = 'src/Checkout.swift'
BEFORE = b'func submit(ready: Bool) -> String {\n    if !ready { return "success" }\n    return "error"\n}\n'
EDIT = {'path': PRODUCT, 'old': 'if !ready', 'new': 'if ready'}


def source_fixture():
    project = project_document()
    project['recipes'].append({'id': 'build_app', 'kind': 'build',
                              'productFile': 'build/recipe.json', 'operation': 'build'})
    project['executionClasses'].append('build-guest')
    entries = [(PRODUCT, BEFORE), ('tests/CheckoutTests.swift', b'protected independent harness')]
    entries.extend((item['productFile'], b'{"trusted":true}')
                   for item in project['fixtures'] + project['recipes'])
    sources = BlobSet(tuple(entries))
    artifacts = BlobSet((('original.bin', b'original build artifact'),))
    project['builds'][0]['sourceDigest'] = sources.digest
    project['builds'][0]['artifactDigest'] = hashlib.sha256(artifacts.entries[0][1]).hexdigest()
    return project, sources, artifacts


class RepairSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project, self.sources, self.artifacts = source_fixture()
        self.sources.write_new(self.root / 'source')
        self.artifacts.write_new(self.root / 'original')

    def source(self, project=None, paths=None, protected=None):
        from reproloop.project_repair import RepairSource
        return RepairSource(project or self.project, self.root / 'source',
            source_paths=paths or tuple(path for path, _ in self.sources.entries),
            protected_paths=protected or ('tests/CheckoutTests.swift',),
            original_artifact_root=self.root / 'original',
            original_artifact_paths=('original.bin',), artifact_identity='file-sha256')

    def test_product_logic_beyond_numeric_expressions_is_isolated(self):
        source = self.source()
        frozen = source.freeze('original')
        candidate = source.apply(frozen, [EDIT])
        self.assertIn(b'if ready', dict(candidate.entries)[PRODUCT])
        self.assertEqual((self.root / 'source' / PRODUCT).read_bytes(), BEFORE)
        self.assertEqual(dict(candidate.entries)['tests/CheckoutTests.swift'],
                         dict(self.sources.entries)['tests/CheckoutTests.swift'])
        self.assertNotEqual(candidate.digest, frozen.digest)
        source.require_original(frozen, 'original')

    def test_noop_ambiguous_overlapping_and_undeclared_edits_are_denied(self):
        from reproloop.project_repair import RepairError
        source = self.source(); frozen = source.freeze('original')
        bad = [[], [dict(EDIT, new=EDIT['old'])], [dict(EDIT, old='return')],
               [EDIT, dict(EDIT, old='if !ready {', new='if ready {')],
               [dict(EDIT, path='new.swift')], [dict(EDIT, path='../source/' + PRODUCT)],
               [dict(EDIT, path='tests/CheckoutTests.swift', old='protected', new='passed')],
               [dict(EDIT, path='checks/ui.json', old='true', new='false')]]
        for edits in bad:
            with self.subTest(edits=edits), self.assertRaises(RepairError):
                source.apply(frozen, edits)
        self.assertEqual(BlobSet.from_directory(self.root / 'source',
            tuple(path for path, _ in frozen.entries)), frozen)

    def test_source_build_digest_mismatch_and_changed_original_are_denied(self):
        from reproloop.project_repair import RepairError
        project = copy.deepcopy(self.project); project['builds'][0]['sourceDigest'] = 'f' * 64
        with self.assertRaises(RepairError): self.source(project).freeze('original')
        source = self.source(); frozen = source.freeze('original')
        (self.root / 'original' / 'original.bin').write_bytes(b'changed original')
        with self.assertRaises(RepairError): source.require_original(frozen, 'original')

    def test_protected_recipes_cannot_be_omitted_from_the_sealed_source(self):
        from reproloop.project_repair import RepairError
        paths = tuple(path for path, _ in self.sources.entries if path != 'checks/ui.json')
        with self.assertRaises(RepairError): self.source(paths=paths)
        project = copy.deepcopy(self.project)
        project['editablePaths'].append('tests/CheckoutTests.swift')
        with self.assertRaises(RepairError): self.source(project)

    def test_symlink_secret_file_and_noncanonical_paths_are_rejected(self):
        from reproloop.project_repair import RepairError
        (self.root / 'source' / PRODUCT).unlink()
        (self.root / 'source' / PRODUCT).symlink_to(self.root / 'original' / 'original.bin')
        with self.assertRaises(RepairError): self.source().freeze('original')
        for path in ('.env', 'auth.json', 'src/../Checkout.swift', '/etc/passwd'):
            with self.subTest(path=path), self.assertRaises(RepairError):
                self.source(paths=tuple(p for p, _ in self.sources.entries) + (path,))

    def test_original_source_mutation_is_detected_before_accepting_a_patch(self):
        from reproloop.project_repair import RepairError
        source = self.source(); frozen = source.freeze('original')
        (self.root / 'source' / 'checks/ui.json').write_bytes(b'changed assertion')
        with self.assertRaises(RepairError): source.apply(frozen, [EDIT])


if __name__ == '__main__': unittest.main()
