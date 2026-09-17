import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from reproloop.execution.journal import RunStore
from reproloop.ios_mobile_configuration import (
    export_ios_mobile_configuration, load_ios_mobile_configuration,
)
from tests import test_ios_mobile_operation as support


class IOSMobilePreparationCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IOSMobileOperationTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with self.fixture.operations.admit(self.fixture.context, self.fixture.artifacts,
                                           self.fixture.baselines):
            pass
        self.path = self.fixture.root / 'mobile-recovery.json'
        self.path.write_text(json.dumps(export_ios_mobile_configuration(self.fixture.operations)))
        self.path.chmod(0o600)

    def command(self, *arguments):
        from reproloop.cli import main
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['ios-mobile', *arguments])
        return code, json.loads(output.getvalue())

    def test_reference_is_redacted_and_loader_reads_no_inputs_or_tools(self):
        reference = load_ios_mobile_configuration(self.path)
        self.assertNotIn(self.fixture.selected.udid, repr(reference))
        self.assertNotIn(self.fixture.selected.udid, json.dumps(reference.public()))
        with patch('reproloop.ios_mobile_configuration.open_regular',
                   side_effect=AssertionError('configuration loader must be the only file read')):
            # The patch applies only after construction; opening an existing
            # owner must use the journal APIs, not the input references.
            owner = reference.open_existing()
        owner.close()

    def test_status_is_read_only_and_recovery_is_request_bound_and_idempotent(self):
        before = {p.relative_to(self.fixture.root).as_posix(): p.read_bytes()
                  for p in self.fixture.root.rglob('*') if p.is_file()}
        code, report = self.command('status', '--config', str(self.path), '--operation',
                                    self.fixture.context.operation_id)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'observed')
        after = {p.relative_to(self.fixture.root).as_posix(): p.read_bytes()
                 for p in self.fixture.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        code, report = self.command('recover', '--config', str(self.path), '--operation',
                                    self.fixture.context.operation_id, '--request-digest', '0' * 64)
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'rejected')
        code, report = self.command('recover', '--config', str(self.path), '--operation',
                                    self.fixture.context.operation_id, '--request-digest',
                                    self.fixture.context.request_digest)
        self.assertEqual(code, 0)
        self.assertEqual((report['status'], report['reservedBytes']), ('recovered', 0))
        code, report = self.command('recover', '--config', str(self.path), '--operation',
                                    self.fixture.context.operation_id, '--request-digest',
                                    self.fixture.context.request_digest)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'already-terminal')

    def test_subprocess_registration_and_errors_are_redacted(self):
        self.fixture.operations.close()
        command = [sys.executable, '-m', 'reproloop.cli', 'ios-mobile', 'status',
                   '--config', str(self.path), '--operation', self.fixture.context.operation_id]
        completed = subprocess.run(command, cwd=Path(__file__).parents[1], text=True,
                                   capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0)
        report = json.loads(completed.stdout)
        self.assertEqual(report['status'], 'observed')
        bad = [sys.executable, '-m', 'reproloop.cli', 'ios-mobile', 'status',
               '--config', str(self.fixture.root / 'missing.json'), '--operation',
               self.fixture.context.operation_id]
        completed = subprocess.run(bad, cwd=Path(__file__).parents[1], text=True,
                                   capture_output=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(str(self.fixture.root), completed.stdout)
        self.assertNotIn(self.fixture.selected.udid, completed.stdout)

    def test_alternate_journal_is_rejected_without_creating_state(self):
        value = json.loads(self.path.read_text())
        value['ownerRoot'] = str(self.fixture.root / 'missing-owner')
        alternate = self.fixture.root / 'alternate.json'
        alternate.write_text(json.dumps(value)); alternate.chmod(0o600)
        code, report = self.command('status', '--config', str(alternate), '--operation',
                                    self.fixture.context.operation_id)
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'rejected')
        self.assertFalse((self.fixture.root / 'missing-owner').exists())

    def test_native_bound_operation_is_reserved_for_native_recovery(self):
        from reproloop.ios_mobile_operation import IOSMobileOperationStore
        observed = {'schemaVersion': 1, 'operationId': self.fixture.context.operation_id,
                    'requestDigest': self.fixture.context.request_digest,
                    'configurationDigest': '0' * 64, 'scopeDigest': '1' * 64,
                    'roles': {}, 'runState': 'quarantined', 'reservedBytes': 1,
                    'executionAuthority': 'none', 'deviceCleanupConfirmed': False,
                    'nativeOwnership': {'state': 'bound', 'bindingDigest': '2' * 64,
                                        'ownershipGeneration': 1, 'deviceCleanupConfirmed': False}}
        with patch.object(IOSMobileOperationStore, 'status', return_value=observed), \
                patch.object(IOSMobileOperationStore, 'preparation_recovery',
                             side_effect=AssertionError('native recovery must stay quarantined')):
            code, report = self.command('recover', '--config', str(self.path), '--operation',
                                        self.fixture.context.operation_id, '--request-digest',
                                        self.fixture.context.request_digest)
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'rejected')
        self.assertEqual(report['error']['code'], 'ios_mobile_native_recovery_reserved')

    def test_tampered_inner_definition_is_rejected(self):
        value = json.loads(self.path.read_text())
        value['definition']['scopeDigest'] = '0' * 64
        tampered = self.fixture.root / 'tampered-definition.json'
        tampered.write_text(json.dumps(value)); tampered.chmod(0o600)
        code, report = self.command('status', '--config', str(tampered), '--operation',
                                    self.fixture.context.operation_id)
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'rejected')

    def test_unsuccessful_owner_close_is_not_reported_as_success(self):
        from reproloop.ios_mobile_operation import IOSMobileOperationStore
        with patch.object(IOSMobileOperationStore, 'close', return_value=False):
            code, report = self.command('status', '--config', str(self.path), '--operation',
                                        self.fixture.context.operation_id)
        self.assertEqual(code, 2)
        self.assertEqual(report['status'], 'rejected')

    def test_cancellation_before_recovery_does_not_consume_reservation(self):
        from reproloop import ios_mobile_cli
        with patch.object(ios_mobile_cli, '_handlers', side_effect=lambda event: event.set() or {}):
            code, report = self.command('recover', '--config', str(self.path), '--operation',
                                        self.fixture.context.operation_id, '--request-digest',
                                        self.fixture.context.request_digest)
        self.assertEqual(code, 130)
        self.assertEqual(report['status'], 'interrupted')
        self.assertGreater(self.fixture.runs.status(self.fixture.context.operation_id)['reservedBytes'], 0)


if __name__ == '__main__':
    unittest.main()
