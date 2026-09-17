from pathlib import Path
import tempfile
import unittest
from reproloop.repair import apply_edits, validate_sample_expression
from reproloop.core import ContractError


class PatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
        (self.root/'app').mkdir();(self.root/'app/main.kt').write_text('val increment = 2\n')
        (self.root/'verifier.py').write_text('check = True\n')
    def tearDown(self):self.t.cleanup()
    def test_all_changes_validate_before_any_write(self):
        edits=[{'path':'app/main.kt','old':'increment = 2','new':'increment = 1'},
               {'path':'verifier.py','old':'True','new':'False'}]
        with self.assertRaises(ContractError):apply_edits(self.root,edits,{'app/main.kt'})
        self.assertIn('increment = 2',(self.root/'app/main.kt').read_text())
    def test_only_allowlisted_product_source_changes(self):
        changed=apply_edits(self.root,[{'path':'app/main.kt','old':'increment = 2','new':'increment = 1'}],{'app/main.kt'})
        self.assertEqual(changed,['app/main.kt']);self.assertIn('increment = 1',(self.root/'app/main.kt').read_text())
    def test_path_escape_links_and_ambiguous_edits_rejected(self):
        (self.root/'app/link.kt').symlink_to(self.root/'verifier.py')
        for path in ['../verifier.py','app/link.kt','/tmp/test.kt']:
            with self.subTest(path=path),self.assertRaises(ContractError):
                apply_edits(self.root,[{'path':path,'old':'True','new':'False'}],{path})
        with self.assertRaises(ContractError):
            apply_edits(self.root,[{'path':'app/main.kt','old':'not present','new':'x'}],{'app/main.kt'})

class ExpressionBoundaryTests(unittest.TestCase):
    def test_expression_only_change(self):
        validate_sample_expression('fun increment() = 2\n','fun increment() = 1\n')
    def test_arbitrary_agent_code_never_reaches_host_build(self):
        before='fun increment() = 2\n'
        for after in ['fun increment() = Runtime.getRuntime().exec("bad").waitFor()\n',
                      'fun increment() = 1\nval other = "changed"\n']:
            with self.assertRaises(ContractError):validate_sample_expression(before,after)

if __name__=='__main__':unittest.main()
