from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproloop.android_profile import validate_app_profile
from reproloop.core import ContractError, digest
from reproloop.orchestrator import repair_job
from reproloop.repair import snapshot_source
from reproloop.storage import create_bundle, sha_file
from tests.test_android_profile import profile_document


class NumericEditTests(unittest.TestCase):
    def test_configured_function_changes_only_numeric_expression(self):
        from reproloop.repair import validate_numeric_expression
        before = 'object Stock {\n    fun unitsPerItem(): Int = 2\n}\n'
        after = before.replace('= 2', '= 1')
        validate_numeric_expression(before, after, 'unitsPerItem')
        for wrong in [after.replace('Stock', 'Changed'), after.replace('= 1', '= runCommand()'),
                      after.replace('unitsPerItem', 'different')]:
            with self.assertRaises(ContractError):
                validate_numeric_expression(before, wrong, 'unitsPerItem')


class ProfileRepairTests(unittest.TestCase):
    def test_profile_drives_build_edit_and_regression_without_changing_original(self):
        self.exercise()

    def test_explicit_resources_are_preserved_through_repair_and_not_sent_to_agent(self):
        self.exercise(explicit=True)

    def test_resource_mutation_by_regression_blocks_candidate_verification(self):
        self.exercise(explicit=True, mutate=True)

    def exercise(self, *, explicit=False, mutate=False):
        profile = validate_app_profile(profile_document())
        config = profile.data
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'source'
            product = source / config['edit']['path']
            product.parent.mkdir(parents=True)
            product.write_text('fun unitsPerItem() = 2\n')
            (source / 'build.gradle.kts').write_text('// protected fixture')
            binary = 'app/src/main/assets/logo.png'
            if explicit:
                (source / 'settings.gradle.kts').write_text('include(":app")\n')
                (source / 'app/build.gradle.kts').write_text('// owned app build\n')
                (source / binary).parent.mkdir(parents=True)
                (source / binary).write_bytes(b'\x89PNG\r\n\x1a\n\xff')
                config['sourceInputs'] = sorted([*snapshot_source(source), binary])
                profile = validate_app_profile(config)
            inputs = config.get('sourceInputs')
            apk = root / 'original.apk'; apk.write_bytes(b'original')
            proof = {'sourceDigest': digest(snapshot_source(source, source_inputs=inputs)), 'apkSha256': sha_file(apk),
                     'buildCompleted': True, 'appProfileDigest': profile.digest}
            capture = {'schemaVersion': 1, 'sessionId': 'profile-run', 'fixture': config['fixture'],
                'startState': config['startState'], 'truncated': False, 'lostEvents': False, 'endSequence': 1,
                'events': [{'id': 'e1', 'seq': 1, 'action': 'tap', 'target': 'commit', 'parameters': {}}]}
            bundle = create_bundle(capture, profile.oracle(), apk, root / 'bundle', proof, app_profile=profile)
            class Agent:
                def propose(self, files, scenario, feedback):
                    self.asserted = (list(files) == [config['edit']['path']] and scenario['appProfileDigest'] == profile.digest)
                    return [{'path': config['edit']['path'], 'old': '= 2', 'new': '= 1'}]
            agent = Agent()
            calls = []
            def build(workspace, **kwargs):
                self.assertEqual(kwargs['task'], config['build']['task'])
                self.assertEqual(kwargs['apk_relative'], config['build']['apk'])
                output = workspace / kwargs['apk_relative']; output.parent.mkdir(parents=True)
                output.write_bytes(b'patched')
                if explicit:self.assertEqual((workspace / binary).read_bytes(), (source / binary).read_bytes())
                return output, {'sourceDigest': digest(snapshot_source(workspace, source_inputs=inputs)),
                                'apkSha256': sha_file(output), 'buildCompleted': True}
            def regression(command, cwd, **kwargs):
                calls.append(command[-1])
                output = Path(cwd) / config['build']['regressionResults']; output.mkdir(parents=True)
                (output / 'TEST-Stock.xml').write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
                if mutate:(Path(cwd) / binary).write_bytes(b'mutated after build')
            def replay(device, bundle, apk, output, phase, repeats, *args, **kwargs):
                if mutate:self.assertEqual(phase, 'original')
                from tests.test_core import run
                return {'status': 'reproduced' if phase == 'original' else 'verified',
                        'runs': [run('bug' if phase == 'original' else 'expected') for _ in range(repeats)]}
            with patch('reproloop.orchestrator.build_android', build), \
                 patch('reproloop.orchestrator.run_command', regression), \
                 patch('reproloop.orchestrator.replay_suite', replay):
                result = repair_job(object(), bundle, source, root / 'repair', agent,
                    {'gradle': 'fake', 'java_home': 'fake', 'sdk_home': 'fake'}, app_profile=profile)
            self.assertEqual(result['status'], 'verification_failed' if mutate else 'verified')
            self.assertTrue(agent.asserted)
            self.assertEqual(calls, [config['build']['regressionTask']])
            self.assertEqual(product.read_text(), 'fun unitsPerItem() = 2\n')
            self.assertEqual(result['appProfileDigest'], profile.digest)


if __name__ == '__main__':
    unittest.main()
