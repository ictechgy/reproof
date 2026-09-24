"""Durable scope domains keep native-VM recovery out of signing/mobile work."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from reproof.execution.journal import RunDenied, RunStore


class RepairScopeJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'state'
        self.store = RunStore(self.root, environment_digest='a' * 64, disk_limit=128)
        # Unique scope avoids collisions with a prior process's authority marker.
        import hashlib
        self.scope = hashlib.sha256(str(self.root).encode()).hexdigest()

    def uncertain(self, kind):
        with self.store.repair_scope_lease(kind, self.scope), self.store.admit('operation', 'b' * 64, disk_bytes=128) as run:
            run.finish('failed', stopped=False)
        (self.root / 'runs/operation/termination.json').write_text(json.dumps({
            'schemaVersion': 1, 'operationId': 'operation', 'requestDigest': 'b' * 64, 'state': 'stopped'}))

    def test_vm_stop_record_cannot_recover_a_mobile_operation_after_restart(self):
        self.uncertain('mobile-device')
        restarted = RunStore(self.root, environment_digest='a' * 64, disk_limit=128)
        with self.assertRaises(RunDenied): restarted.reconcile('operation', 'b' * 64)
        self.assertEqual(restarted.status('operation')['state'], 'quarantined')
        self.assertEqual(restarted.status('operation')['reservedBytes'], 128)

    def test_vm_stop_record_cannot_recover_a_signing_operation_after_restart(self):
        self.uncertain('signing')
        restarted = RunStore(self.root, environment_digest='a' * 64, disk_limit=128)
        with self.assertRaises(RunDenied): restarted.reconcile('operation', 'b' * 64)
        self.assertEqual(restarted.status('operation')['state'], 'quarantined')

    def test_one_journal_cannot_change_between_device_signing_and_vm_domains(self):
        with self.store.repair_scope_lease('signing', self.scope): pass
        for lease in (self.store.repair_scope_lease('mobile-device', self.scope),
                      self.store.repair_scope_lease('signing', 'c' * 64),
                      self.store.machine_lease(self.scope)):
            with self.subTest(lease=type(lease).__name__), self.assertRaises(RunDenied):
                with lease: pass
        with self.store.repair_scope_lease('signing', self.scope): pass

    def test_read_only_availability_preserves_markers_and_denies_foreign_or_quarantined_scope(self):
        from reproof.storage import Lease
        marker = Lease('protected-repair-signing-'+self.scope).marker
        original = (self.root/'state.json').read_bytes()
        self.store.require_scope_available('signing', self.scope)
        self.assertEqual((self.root/'state.json').read_bytes(), original)
        self.assertFalse(marker.exists())
        with self.store.repair_scope_lease('signing', self.scope): pass
        marked = marker.read_bytes()
        other = RunStore(self.root.parent/'other', environment_digest='a'*64, disk_limit=128)
        with self.assertRaises(RunDenied): other.require_scope_available('signing', self.scope)
        self.assertEqual(marker.read_bytes(), marked)
        with self.store.repair_scope_lease('signing', self.scope), self.store.admit('busy','d'*64,disk_bytes=128):
            with self.assertRaises(RunDenied): self.store.require_scope_available('signing', self.scope)
        with self.assertRaises(RunDenied): self.store.require_scope_available('signing', self.scope)

    def test_scoped_vm_recovery_still_requires_exact_native_stop_record(self):
        with self.store.machine_lease(self.scope), self.store.admit('operation', 'b' * 64, disk_bytes=128) as run:
            run.finish('failed', stopped=False)
        with self.store.machine_lease(self.scope), self.assertRaises(RunDenied):
            self.store.reconcile('operation', 'b' * 64)
        (self.root / 'runs/operation/termination.json').write_text(json.dumps({
            'schemaVersion': 1, 'operationId': 'operation', 'requestDigest': 'b' * 64, 'state': 'stopped'}))
        with self.store.machine_lease(self.scope):
            result = self.store.reconcile('operation', 'b' * 64)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['reservedBytes'], 0)

    def test_legacy_uncertain_work_cannot_gain_a_recovery_domain_from_new_configuration(self):
        with self.store.admit('legacy', 'b' * 64, disk_bytes=128):
            pass
        for lease in (self.store.machine_lease(self.scope), self.store.repair_scope_lease('signing', self.scope)):
            with self.assertRaises(RunDenied):
                with lease: pass
        self.assertNotIn('scope', json.loads((self.root / 'state.json').read_text()))
        self.assertEqual(self.store.status('legacy')['state'], 'quarantined')
        self.assertEqual(self.store.status('legacy')['reservedBytes'], 128)

    def test_vm_recovery_cannot_borrow_a_machine_lease_from_another_thread_or_store(self):
        with self.store.machine_lease(self.scope), self.store.admit('operation', 'b' * 64, disk_bytes=128) as run:
            run.finish('failed', stopped=False)
        (self.root / 'runs/operation/termination.json').write_text(json.dumps({
            'schemaVersion': 1, 'operationId': 'operation', 'requestDigest': 'b' * 64, 'state': 'stopped'}))
        with self.assertRaises(RunDenied): self.store.reconcile('operation', 'b' * 64)
        restarted = RunStore(self.root, environment_digest='a' * 64, disk_limit=128)
        outcomes = []
        def foreign_thread():
            try: outcomes.append(self.store.reconcile('operation', 'b' * 64))
            except RunDenied: outcomes.append('denied')
        with self.store.machine_lease(self.scope):
            worker = threading.Thread(target=foreign_thread); worker.start(); worker.join(2)
            self.assertFalse(worker.is_alive()); self.assertEqual(outcomes, ['denied'])
            with self.assertRaises(RunDenied): restarted.reconcile('operation', 'b' * 64)
            self.assertEqual(self.store.status('operation')['reservedBytes'], 128)
            self.assertEqual(self.store.reconcile('operation', 'b' * 64)['state'], 'failed')

    def test_legacy_terminal_reconciliation_is_an_unchanged_read(self):
        with self.store.admit('legacy', 'b' * 64, disk_bytes=128) as run:
            run.finish('succeeded', stopped=True)
        path = self.root / 'state.json'; before = path.read_bytes()
        self.assertNotIn('scope', json.loads(before))
        self.assertEqual(self.store.reconcile('legacy', 'b' * 64)['state'], 'succeeded')
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(RunDenied): self.store.reconcile('legacy', 'c' * 64)

    def test_invalid_durable_scope_is_rejected_without_rewriting_state(self):
        with self.store.repair_scope_lease('mobile-device', self.scope): pass
        path = self.root / 'state.json'
        original = json.loads(path.read_text())
        for replacement in ({'kind': 'vm', 'scopeDigest': True},
                            {'kind': 'unregistered', 'scopeDigest': self.scope},
                            {'kind': 'signing', 'scopeDigest': self.scope, 'qualified': True}):
            changed = {**original, 'scope': replacement}; raw = json.dumps(changed); path.write_text(raw)
            with self.assertRaises(RunDenied): RunStore(self.root, environment_digest='a' * 64, disk_limit=128)
            self.assertEqual(path.read_text(), raw)


if __name__ == '__main__': unittest.main()
