"""Pure preflight tests: native processes and every socket are inert doubles."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.live import android_recovery_preflight as preflight
from tests import test_android_recovery_birth as birth_fixtures


class _Socket:
    next_port = 42000
    def __init__(self, family, _):
        self.family = family;self.closed = False
    def bind(self, endpoint):
        if isinstance(endpoint, tuple):
            port = endpoint[1]
            if not port:
                port = self.next_port;type(self).next_port += 1
            self.endpoint = ('127.0.0.1', port)
        else:
            self.endpoint = endpoint
    def listen(self, _):pass
    def getsockname(self):return self.endpoint
    def close(self):self.closed = True


class SandboxPreflightTests(unittest.TestCase):
    def setUp(self):
        fixture = birth_fixtures.AndroidRecoveryBirthTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.plan = fixture.plan()
        self.probe = fixture.file('tools/public-probe')
        self.cancel = threading.Event()
        self.calls = []

    def _run(self, owner, argv, environment):
        owner.check();self.calls.append((list(argv), dict(environment)))
        return dict(preflight._EXPECTED) if '-p' in argv else dict.fromkeys(preflight._EXPECTED, 1)

    def context(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(preflight.sys, 'platform', 'darwin'))
        stack.enter_context(patch.object(preflight.PinnedFile, 'verify'))
        stack.enter_context(patch.object(preflight.socket, 'socket', _Socket))
        stack.enter_context(patch.object(preflight, '_run', self._run))
        return stack

    def test_three_policy_controls_and_negative_cases_before_birth(self):
        with self.context():
            result = preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(set(result['policyDigests']), {'server', 'emulator', 'client'})
        self.assertTrue(result['derivativeProbe'])
        self.assertFalse(result['actualAdbDiscoveryVerified'])
        self.assertFalse(Path(self.plan.output).exists())
        for argv, env in self.calls:
            self.assertNotIn('ADB_SERVER_SOCKET', env)
            self.assertNotIn('ANDROID_SERIAL', env)
            self.assertNotIn('5037', argv)
            if '-p' in argv:
                policy = argv[2]
                self.assertIn('(deny network*)', policy)
                self.assertIn('(allow process-exec (literal '+json.dumps(self.probe.path)+'))', policy)
                self.assertNotIn('(allow network*)', policy)

    def test_denial_or_missing_positive_sentinel_fails_and_retains_evidence(self):
        for field in preflight._EXPECTED:
            with self.subTest(field=field), self.context():
                good = self._run
                def bad(owner, argv, environment):
                    result = good(owner, argv, environment)
                    if '-p' in argv:result[field] = 1-result[field]
                    return result
                with patch.object(preflight, '_run', bad), self.assertRaises(preflight.PreflightIncomplete) as caught:
                    preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
                self.assertTrue(caught.exception.owner.root.is_dir())
                self.assertFalse(Path(self.plan.output).exists())

    def test_control_failure_is_not_misreported_as_enforcement(self):
        with self.context(), patch.object(preflight, '_run', return_value=dict(preflight._EXPECTED)), \
             self.assertRaises(preflight.PreflightIncomplete):
            preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)

    def test_platform_cancel_and_type_rejection_before_socket(self):
        with patch.object(preflight.socket, 'socket') as sock:
            with patch.object(preflight.sys, 'platform', 'linux'), self.assertRaises(ContractError):
                preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
            self.cancel.set()
            with patch.object(preflight.sys, 'platform', 'darwin'), self.assertRaises(ContractError):
                preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
        sock.assert_not_called()

    def test_occupied_selected_port_fails_without_connecting_to_it(self):
        class Occupied(_Socket):
            def bind(self, endpoint):raise OSError('synthetic occupied selected port')
        with self.context(), patch.object(preflight.socket, 'socket', Occupied), \
             self.assertRaises(preflight.PreflightIncomplete):
            preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
        self.assertEqual(self.calls, [])

    def test_namespace_replacement_is_detected_and_foreign_preserved(self):
        with self.context():
            def changed(owner, argv, environment):
                old = owner.root.with_name(owner.root.name+'-moved')
                owner.root.rename(old);owner.root.mkdir(mode=0o700)
                (owner.root/'foreign').write_bytes(b'preserve')
                owner.check()
            with patch.object(preflight, '_run', changed), self.assertRaises(preflight.PreflightIncomplete) as caught:
                preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
        self.assertEqual((caught.exception.owner.root/'foreign').read_bytes(), b'preserve')

    def test_interrupt_collects_owned_resources_without_starting_birth(self):
        with self.context(), patch.object(preflight, '_run', side_effect=KeyboardInterrupt), \
             self.assertRaises(preflight.PreflightIncomplete) as caught:
            preflight.run_sandbox_preflight(self.plan, self.probe, cancellation=self.cancel)
        self.assertEqual(caught.exception.owner.listeners, [])
        self.assertIsNone(caught.exception.owner.fd)
        self.assertFalse(Path(self.plan.output).exists())

    def test_ambiguous_fd_close_is_never_retried_or_reported_clean(self):
        root = Path(self.plan.output).parent
        fd = os.open(root, os.O_RDONLY|os.O_DIRECTORY)
        owner = preflight._Owner(root, fd, self.cancel, time.monotonic()+10)
        real_close = os.close
        foreign = []
        def close_then_error(value):
            real_close(value)
            replacement = os.open(root, os.O_RDONLY|os.O_DIRECTORY)
            foreign.append(replacement)
            self.assertEqual(replacement, value)
            raise OSError('synthetic close outcome uncertainty')
        try:
            with patch.object(preflight.os, 'close', close_then_error):
                self.assertFalse(owner.collect())
            self.assertFalse(owner.collect())
            os.fstat(foreign[0])
        finally:
            for selected in foreign:real_close(selected)

    def test_public_source_matches_embedded_fixed_source(self):
        source = Path(__file__).resolve().parents[1]/'scripts/android-recovery-sandbox-probe.c'
        self.assertEqual(source.read_text(), preflight.PROBE_SOURCE)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), preflight.PROBE_SOURCE_DIGEST)
        self.assertIn('/usr/bin/true', preflight.PROBE_SOURCE)
        self.assertNotIn('system(', preflight.PROBE_SOURCE)
        self.assertNotIn('5037', preflight.PROBE_SOURCE)

    def test_run_parses_only_bounded_exact_boolean_integers(self):
        root = Path(self.plan.output).parent
        fd = os.open(root, os.O_RDONLY|os.O_DIRECTORY)
        owner = preflight._Owner(root, fd, self.cancel, time.monotonic()+10)
        self.addCleanup(owner.collect)
        class Child:
            def __init__(self):
                self.process = SimpleNamespace(poll=lambda:0, returncode=0)
                self.collectors = [SimpleNamespace(data=json.dumps(dict(preflight._EXPECTED)).encode())]
            def start(self, *args):pass
            def bounded(self):return True
            def collect(self, _):return True
        with patch.object(preflight, '_Child', Child):
            self.assertEqual(preflight._run(owner, ['fixed-probe'], {}), preflight._EXPECTED)
        class Invalid(Child):
            def __init__(self):
                super().__init__();self.collectors[0].data = b'{"allowedRead":true}'
        with patch.object(preflight, '_Child', Invalid), self.assertRaises(ContractError):
            preflight._run(owner, ['fixed-probe'], {})

    def test_native_boundary_failures_keep_registered_child_for_collection(self):
        root = Path(self.plan.output).parent
        for failure in ('returncode', 'oversized', 'collect', 'cancel', 'partial-start'):
            with self.subTest(failure=failure):
                cancel = threading.Event()
                fd = os.open(root, os.O_RDONLY|os.O_DIRECTORY)
                owner = preflight._Owner(root, fd, cancel, time.monotonic()+10)
                class Child:
                    def __init__(self):
                        self.process = SimpleNamespace(poll=lambda: None if failure == 'cancel' else 0,
                                                       returncode=1 if failure == 'returncode' else 0)
                        self.collectors = [SimpleNamespace(data=json.dumps(dict(preflight._EXPECTED)).encode())]
                        self.collected = 0
                    def start(self, *args):
                        if failure == 'cancel':cancel.set()
                        if failure == 'partial-start':raise OSError('synthetic partial Popen failure')
                    def bounded(self):return failure != 'oversized'
                    def collect(self, _):
                        self.collected += 1
                        return failure != 'collect'
                try:
                    with patch.object(preflight, '_Child', Child), self.assertRaises((ContractError, OSError)):
                        preflight._run(owner, ['fixed-probe'], {})
                    self.assertEqual(len(owner.children), 1)
                    owner.collect()
                    self.assertGreaterEqual(owner.children[0].collected, 1)
                finally:
                    if owner.fd is not None:
                        os.close(owner.fd);owner.fd = None
