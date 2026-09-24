import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from reproof.android_profile import sample_app_profile, validate_app_profile
from reproof.core import ContractError
from reproof.instrumentation import render_runtime_config
from reproof.ios_instrumentation import prepare_ios_instrumentation
from tests.test_ios_instrumentation import minimal_project


class AppLogProfileTests(unittest.TestCase):
    def test_screen_targets_do_not_change_native_replay_contract(self):
        original=sample_app_profile()
        document=original.data;document['screenTargets']={'main_panel':'main','detail_panel':'details'}
        selected=validate_app_profile(document)
        self.assertEqual(selected.native(),original.native())
        self.assertNotEqual(selected.digest,original.digest)
        for invalid in [{'bad target':'main'}, {'main_panel':'private title'}, {'a':'same','b':'same'}, []]:
            document['screenTargets']=invalid
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):validate_app_profile(document)

    def test_generated_runtime_has_explicit_bounded_log_configuration(self):
        original=sample_app_profile();document=original.data;document['screenTargets']={'main_panel':'main'}
        selected=validate_app_profile(document)
        generated=render_runtime_config(selected,[])
        self.assertIn('const val APP_LOGS_ENABLED = false',generated)
        self.assertIn('const val SCREEN_TARGETS_JSON',generated)
        self.assertIn('main_panel',generated)

    def test_ios_new_debug_preparation_advertises_log_schema_without_changing_original_info(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';minimal_project(source)
            before=(source/'Sample/Info.plist').read_bytes()
            prepare_ios_instrumentation(source,root/'prepared')
            original=plistlib.loads(before)
            generated=plistlib.loads((root/'prepared/source/ReproofInstrumentation/Info.plist').read_bytes())
            self.assertEqual(generated['ReproAppLogSchemaVersion'],1)
            self.assertNotIn('ReproAppLogSchemaVersion',original)
            self.assertEqual((source/'Sample/Info.plist').read_bytes(),before)

if __name__=='__main__':unittest.main()
