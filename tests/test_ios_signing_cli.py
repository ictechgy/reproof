from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from reproof.execution.journal import RunDenied
from tests import test_ios_signing_operation as support


class IOSSigningRecoveryCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IOSSigningOperationTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root; self.operations = self.fixture.operations
        with self.operations.admit(self.fixture.context, self.fixture.request) as operation:
            self.operations.stage(operation, self.fixture.blobs)

    def config_file(self, **changes):
        value = self.operations.recovery_configuration()
        value.update(changes)
        path = self.root/'recovery.json'; path.write_text(json.dumps(value)); path.chmod(0o600)
        return path

    def command(self, *arguments):
        from reproof.cli import main
        output = io.StringIO()
        with redirect_stdout(output): code = main(['ios-signing', *arguments])
        return code, json.loads(output.getvalue())

    def test_export_is_secret_free_and_recovery_never_loads_signing_material_or_tools(self):
        from reproof.ios_signing_configuration import load_ios_signing_configuration
        path = self.config_file()
        serialized = path.read_text()
        for value in ('owned unverified profile', 'certificateChain', 'entitlements', 'pkcs12', 'password'):
            self.assertNotIn(value, serialized)
        with patch('reproof.ios_signing_inputs.IOSSigningOwnerTools.verify', side_effect=AssertionError('tools must not load')):
            config = load_ios_signing_configuration(path)
            reader = config.open_existing(); self.addCleanup(reader.close)
            self.assertTrue(reader.recovery_only)
            with self.assertRaises(Exception):
                with reader.admit(self.fixture.context, self.fixture.request): pass
            self.assertIsNone(reader.tools); self.assertIsNone(reader.definition)
            with reader.recovery(self.fixture.context.operation_id, self.fixture.request) as capability:
                with self.assertRaises(RunDenied):
                    reader.run_store.finish_signing_recovery(replace(capability), authority=reader)
                row = reader.run_store.finish_signing_recovery(capability, authority=reader)
            self.assertEqual((row['state'], row['reservedBytes']), ('failed', 0))

    def test_cli_status_is_read_only_and_recover_is_idempotent_and_request_bound(self):
        path = self.config_file(); operation = self.fixture.context.operation_id
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        code, report = self.command('status','--config',str(path),'--operation',operation)
        self.assertEqual(code, 0); self.assertEqual(report['status'], 'observed')
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        code, report = self.command('recover','--config',str(path),'--operation',operation,'--request-digest','0'*64)
        self.assertEqual(code, 2); self.assertGreater(self.fixture.run_store.status(operation)['reservedBytes'], 0)
        for expected in ('recovered', 'already-terminal'):
            code, report = self.command('recover','--config',str(path),'--operation',operation,
                '--request-digest',self.fixture.request)
            self.assertEqual(code, 0); self.assertEqual(report['status'], expected)
            self.assertEqual(report['reservedBytes'], 0)

    def test_changed_reference_or_journal_never_creates_replacement_state(self):
        from reproof.ios_signing_configuration import load_ios_signing_configuration
        for field, value in (('applicationId','other_app'), ('scopeDigest','0'*64),
                             ('ownerConfigurationSha256','0'*64), ('ownerRoot',str(self.root/'missing'))):
            with self.subTest(field=field):
                path = self.config_file(**{field:value})
                with self.assertRaises(Exception):
                    reader = load_ios_signing_configuration(path).open_existing()
                    try: reader.status(self.fixture.context.operation_id)
                    finally: reader.close()
        self.assertFalse((self.root/'missing').exists())
        path = self.config_file()
        reader = load_ios_signing_configuration(path).open_existing(); self.addCleanup(reader.close)
        original = self.operations.root/'configuration.json'
        original.write_bytes(original.read_bytes()+b' ')
        with self.assertRaises(Exception): reader.status(self.fixture.context.operation_id)
        self.assertGreater(self.fixture.run_store.status(self.fixture.context.operation_id)['reservedBytes'], 0)

    def test_unknown_fields_duplicate_keys_and_secret_like_options_are_rejected(self):
        path = self.config_file(qualified=True)
        code, report = self.command('status','--config',str(path),'--operation',self.fixture.context.operation_id)
        self.assertEqual(code, 2); self.assertEqual(report['status'], 'rejected')
        path = self.config_file()
        path.write_text(path.read_text().replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'))
        code, _ = self.command('status','--config',str(path),'--operation',self.fixture.context.operation_id)
        self.assertEqual(code, 2)

    def test_recovery_reference_cannot_accept_boolean_journal_version(self):
        from reproof import contracts
        from reproof.ios_signing_configuration import load_ios_signing_configuration
        reference = self.operations.recovery_configuration()
        original = self.operations.root/'configuration.json'
        previous = original.read_bytes()
        changed = json.loads(previous); changed['schemaVersion'] = True
        encoded = json.dumps(changed).encode()
        reference.update(ownerConfigurationSha256=hashlib.sha256(encoded).hexdigest(),
                         ownerDefinitionDigest=contracts.digest(changed))
        path = self.root/'changed-reference.json'
        path.write_text(json.dumps(reference)); path.chmod(0o600)
        try:
            original.write_bytes(encoded)
            with self.assertRaises(Exception):
                reader = load_ios_signing_configuration(path).open_existing()
                reader.close()
        finally: original.write_bytes(previous)

    def test_recovery_only_reader_cannot_dispatch_staging_signing_or_inspection(self):
        from reproof.ios_signing_configuration import load_ios_signing_configuration
        reader = load_ios_signing_configuration(self.config_file()).open_existing()
        self.addCleanup(reader.close)
        options = {'provisioning':None,'policy_document':{},'cancellation':threading.Event(),
                   'deadline_monotonic':time.monotonic()+5}
        with patch('reproof.ios_signing_execution.execute_sign') as sign, \
                patch('reproof.ios_signing_execution.execute_inspection') as inspect:
            with self.assertRaises(Exception): reader.stage(object(),self.fixture.blobs)
            with self.assertRaises(Exception): reader.sign(object(),self.fixture.blobs,material_resolver=None,**options)
            with self.assertRaises(Exception): reader.inspect(object(),self.fixture.context,self.fixture.blobs,**options)
            sign.assert_not_called(); inspect.assert_not_called()
        self.assertGreater(self.fixture.run_store.status(self.fixture.context.operation_id)['reservedBytes'],0)

    def test_cli_can_retry_after_cleanup_succeeds_but_final_journal_write_fails(self):
        from reproof.ios_signing_configuration import load_ios_signing_configuration
        path = self.config_file()
        reader = load_ios_signing_configuration(path).open_existing(); self.addCleanup(reader.close)
        operation = self.fixture.context.operation_id
        with reader.recovery(operation,self.fixture.request) as capability:
            with patch.object(reader.run_store,'_write',side_effect=RunDenied('owned interrupted commit')):
                with self.assertRaises(RunDenied):
                    reader.run_store.finish_signing_recovery(capability,authority=reader)
        self.assertFalse((reader.run_store.root/'runs'/operation).exists())
        self.assertGreater(reader.run_store.status(operation)['reservedBytes'],0)
        code,report = self.command('recover','--config',str(path),'--operation',operation,
            '--request-digest',self.fixture.request)
        self.assertEqual(code,0)
        self.assertEqual((report['status'],report['state'],report['reservedBytes']),('recovered','failed',0))
