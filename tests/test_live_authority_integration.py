from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproof.core import ContractError
from reproof.live.authority import HELPER_VERSION, NATIVE_PROTOCOL_VERSION, HostAuthority, ProviderResult
from reproof.live.clock_sync import ClockReading
from reproof.live.model import Lab, LiveError
from reproof.live import providers
from reproof.live.providers import IosProvider
from reproof.storage import Lease


class Clock:
    clock_id = "integration-clock"
    boot_digest = "d" * 64

    def __init__(self):
        self.now = 1_000_000_000

    def read(self):
        return ClockReading(self.clock_id, self.boot_digest, self.now, 0)


def parent_grant(authority, clock, lifetime=60_000_000_000):
    received = authority.clock_sync.sample()
    clock.now += 10
    sent = authority.clock_sync.sample()
    mapping = authority.clock_sync.record_exchange(
        coordinator_clock_id="integration-coordinator",
        coordinator_send_ns=received.nanoseconds,
        host_received=received,
        host_sent=sent,
        coordinator_receive_ns=sent.nanoseconds,
        max_drift_ppm=0,
    )
    return authority.issue_parent_grant(
        mapping,
        grant_id="integration-grant",
        project_id="integration-project",
        controller_id="integration-controller",
        renewal_sequence=1,
        coordinator_deadline_ns=sent.nanoseconds + lifetime,
    )


class FencedProvider:
    def __init__(self):
        self.device_authority = None
        self.provider_incarnation = None
        self.permits = []
        self.closed = False
        self.block = None
        self.entered = None
        self.reject = False
        self.unknown = False
        self.close_entered = None
        self.close_block = None
        self.effects = []

    def bind_authority(self, device_authority, provider_incarnation):
        self.device_authority = device_authority
        self.provider_incarnation = provider_incarnation

    def start_authorized(self, session, lab, permit):
        self.device_authority.check_dispatch_permit(permit)
        self.permits.append(("startup", permit))
        self.session = session
        self.lab = lab
        lab.publish_frame(session["id"], b"<svg/>", "image/svg+xml", 400, 800)
        return {"ok": True}
    def execute_authorized(self, action, payload, permit, frame=None):
        self.device_authority.check_dispatch_permit(permit)
        self.permits.append((action, permit))
        if self.entered is not None:
            self.entered.set()
        if self.block is not None:
            self.block.wait(2)
        self.device_authority.check_dispatch_permit(permit)
        if self.unknown:
            raise TimeoutError("private provider detail")
        if self.reject:
            return {"ok": False, "outcome": "rejected", "code": "target_unavailable"}
        self.effects.append(permit.operation_id)
        self.lab.publish_frame(self.session["id"], b"<svg/>", "image/svg+xml", 400, 800)
        return {"ok": True, "timing": "best-effort"}

    def close_authorized(self, permit):
        self.device_authority.check_dispatch_permit(permit)
        self.permits.append(("cleanup", permit))
        if self.close_entered is not None:self.close_entered.set()
        if self.close_block is not None:self.close_block.wait(2)
        self.closed = True
        return {"ok": True}


class UnfencedProvider:
    def __init__(self):
        self.started = False

    def start(self, session, lab):
        self.started = True


class LiveAuthorityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.authority = HostAuthority(
            self.root / "authority.sqlite3",
            clock=self.clock,
            lease_directory=self.root / "leases",
        )
        self.provider = FencedProvider()
        descriptor = {
            "id": "alias-one", "name": "Synthetic fenced executor",
            "platform": "android", "kind": "android-live",
            "capabilities": {"actions": ["tap", "reset"], "inputMode": "gesture-batch"},
            "factory": lambda: self.provider,
            "_authority": {"deviceKind": "android", "physicalId": "physical-one"},
        }
        self.lab = Lab(
            [descriptor], self.root / "live",
            authority=self.authority,
            parent_grant=parent_grant(self.authority, self.clock),
        )

    def tearDown(self):
        try:
            self.lab.close_all()
        finally:
            self.authority.close()
            self.temporary.cleanup()

    def session(self):
        return self.lab.create_session("alias-one", "owner", "browser")

    def command(self, session, *, identifier="Visible-Command", sequence=1, x=.5):
        frame = self.lab.frame(session["id"])
        return {
            "controllerId": session["controllerId"], "epoch": session["epoch"],
            "sequence": sequence, "commandId": identifier,
            "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
            "action": "tap", "payload": {"x": x, "y": .5},
        }

    def test_start_input_and_cleanup_use_one_authority_and_stable_operation(self):
        session = self.session()
        command = self.command(session)
        receipt = self.lab.input(session["id"], "owner", command)
        duplicate = self.lab.input(session["id"], "owner", command)
        self.assertEqual(receipt, duplicate)
        input_permits = [permit for action, permit in self.provider.permits if action == "tap"]
        self.assertEqual(len(input_permits), 1)
        self.assertRegex(input_permits[0].operation_id, r"^operation_[0-9a-f]{40}$")
        self.assertEqual(input_permits[0].payload_digest,
                         self.authority.store.operation(input_permits[0].operation_id)["payload_digest"])
        with self.assertRaisesRegex(ContractError, "already leased|cutover is blocked"):
            with Lease("physical-one", self.root / "leases"):
                pass
        self.lab.close_session(session["id"], "owner")
        self.assertTrue(self.provider.closed)
        self.assertEqual([action for action, _ in self.provider.permits],
                         ["startup", "tap", "cleanup"])
        with Lease("physical-one", self.root / "leases"):
            pass

    def test_provider_callback_does_not_hold_session_lock(self):
        session = self.session()
        self.provider.entered = threading.Event()
        self.provider.block = threading.Event()
        errors = []
        thread = threading.Thread(
            target=lambda: self._input_capture(session, errors), daemon=True
        )
        thread.start()
        self.assertTrue(self.provider.entered.wait(1))
        inspected = []
        observer = threading.Thread(
            target=lambda: inspected.append(self.lab.get_session(session["id"])), daemon=True
        )
        observer.start();observer.join(1)
        self.assertFalse(observer.is_alive())
        self.assertEqual(inspected[0]["state"], "active")
        self.provider.block.set();thread.join(2)
        self.assertEqual(errors, [])

    def test_failure_revokes_inflight_permit_and_handoff(self):
        session=self.session();self.provider.entered=threading.Event();self.provider.block=threading.Event()
        errors=[]
        thread=threading.Thread(target=lambda:self._input_capture(session,errors),daemon=True)
        thread.start();self.assertTrue(self.provider.entered.wait(1))
        with self.assertRaises(LiveError) as pending:
            self.lab.claim(session['id'],'owner','replacement',session['epoch'])
        self.assertEqual(pending.exception.code,'injection_pending')
        self.lab.fail(session['id'],'Synthetic transport failure')
        self.provider.block.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.provider.effects,[])
        self.assertEqual(self.provider.device_authority.status,'quarantined')

    def test_native_descriptor_cannot_omit_authority_metadata(self):
        provider=UnfencedProvider()
        descriptor={'id':'unbound','name':'Unbound','platform':'android','kind':'android-live',
                    'capabilities':{'actions':['tap']},'factory':lambda:provider}
        with self.assertRaises(LiveError) as rejected:
            Lab([descriptor],self.root/'unbound')
        self.assertEqual(rejected.exception.code,'authority_configuration')
        self.assertFalse(provider.started)

    def _input_capture(self, session, errors):
        try:
            self.lab.input(session["id"], "owner", self.command(session))
        except Exception as error:
            errors.append(error)

    def test_clean_rejection_is_terminal_but_lost_ack_is_quarantined(self):
        session = self.session()
        self.provider.reject = True
        with self.assertRaises(LiveError) as rejected:
            self.lab.input(session["id"], "owner", self.command(session, identifier="reject"))
        self.assertEqual(rejected.exception.code, "input_rejected")
        self.assertEqual(self.provider.device_authority.status, "owned")

        self.provider.reject = False
        self.provider.unknown = True
        with self.assertRaises(LiveError) as unknown:
            self.lab.input(session["id"], "owner",
                           self.command(session, identifier="unknown", sequence=2))
        self.assertEqual(unknown.exception.code, "injection_unknown")
        self.assertEqual(self.provider.device_authority.status, "quarantined")
        self.lab.close_session(session["id"], "owner")
        with self.assertRaisesRegex(ContractError, "cutover is blocked"):
            with Lease("physical-one", self.root / "leases"):
                pass

    def test_native_grant_is_exactly_bound_and_never_outlives_host_permit(self):
        session=self.session();handle=self.provider.device_authority
        state=self.lab._session(session['id'])
        state['authoritySequence']+=1
        admission=handle.admit_operation(
            operation_id='operation_'+'a'*40,payload_digest='b'*64,
            session_id='session_'+session['id'],sequence=state['authoritySequence'])
        permit=handle.prepare_dispatch(admission,provider_incarnation=self.provider.provider_incarnation)
        handshake=handle.bind_native_handshake(
            permit,protocol_version=NATIVE_PROTOCOL_VERSION,helper_version=HELPER_VERSION,
            helper_incarnation=permit.helper_incarnation,provider_incarnation=permit.provider_incarnation,
            native_incarnation='native_test',native_clock_id='android-elapsed-realtime',native_time_ms=50_000)
        wire=handle.native_grant(permit,handshake).wire()
        self.assertEqual(wire['operationId'],permit.operation_id)
        self.assertEqual(wire['operationFingerprint'],permit.operation_fingerprint)
        remaining=(permit.deadline_ns-handshake.host_received_ns)//1_000_000
        self.assertEqual(
            wire['nativeDeadlineMs'],
            50_000 + remaining * (1_000_000-handshake.max_rate_error_ppm)//1_000_000
            - handshake.mapping_uncertainty_ms,
        )
        with self.assertRaisesRegex(ContractError,'incompatible'):
            handle.bind_native_handshake(
                permit,protocol_version=1,helper_version=HELPER_VERSION,
                helper_incarnation=permit.helper_incarnation,provider_incarnation=permit.provider_incarnation,
                native_incarnation='native_test',native_clock_id='android_elapsed',native_time_ms=50_000)
        for field in ('protocol_version','helper_version'):
            values=dict(
                protocol_version=NATIVE_PROTOCOL_VERSION,helper_version=HELPER_VERSION,
                helper_incarnation=permit.helper_incarnation,provider_incarnation=permit.provider_incarnation,
                native_incarnation='native_test',native_clock_id='android-elapsed-realtime',native_time_ms=50_000)
            values[field]=float(values[field])
            with self.subTest(field=field),self.assertRaisesRegex(ContractError,'incompatible'):
                handle.bind_native_handshake(permit,**values)
        with self.assertRaisesRegex(ContractError,'qualified'):
            handle.bind_native_handshake(
                permit,protocol_version=NATIVE_PROTOCOL_VERSION,helper_version=HELPER_VERSION,
                helper_incarnation=permit.helper_incarnation,provider_incarnation=permit.provider_incarnation,
                native_incarnation='native_test',native_clock_id='ios-mach-continuous',native_time_ms=50_000)
        handle.confirm_operation(permit,ProviderResult('receipt_native','succeeded','c'*64))

    def test_late_success_cannot_clear_durable_session_revocation(self):
        handle=self.authority.claim_device(
            device_kind='android',physical_id='physical-revoked',display_alias='revoked-alias',
            helper_incarnation='helper_revoked',parent_grant=self.lab.parent_grant)
        admission=handle.admit_operation(
            operation_id='operation_'+'f'*40,payload_digest='a'*64,
            session_id='session_revoked',sequence=1)
        permit=handle.prepare_dispatch(admission,provider_incarnation='provider_revoked')
        handle.revoke_dispatches()
        handle.confirm_operation(permit,ProviderResult('receipt_revoked','succeeded','b'*64))
        self.assertEqual(handle.status,'quarantined')
        self.assertEqual(self.authority.store.operation(permit.operation_id)['status'],'uncertain')
        handle.close()
        with self.assertRaisesRegex(ContractError,'cutover is blocked'):
            with Lease('physical-revoked',self.root/'leases'):pass

    def test_close_callback_does_not_hold_session_lock(self):
        session=self.session();self.provider.close_entered=threading.Event();self.provider.close_block=threading.Event()
        closed=[]
        thread=threading.Thread(target=lambda:closed.append(self.lab.close_session(session['id'],'owner')),daemon=True)
        thread.start();self.assertTrue(self.provider.close_entered.wait(1))
        inspected=[]
        observer=threading.Thread(target=lambda:inspected.append(self.lab.get_session(session['id'])),daemon=True)
        observer.start();observer.join(1)
        self.assertFalse(observer.is_alive());self.assertEqual(inspected[0]['state'],'draining')
        self.provider.close_block.set();thread.join(2)
        self.assertEqual(closed[0]['state'],'closed')

    def test_incompatible_legacy_provider_is_rejected_before_start_and_unlocks_cleanly(self):
        provider = UnfencedProvider()
        descriptor = {
            'id': 'legacy-alias', 'name': 'Unfenced provider',
            'platform': 'android', 'kind': 'android-live',
            'capabilities': {'actions': ['tap']},
            'factory': lambda: provider,
            '_authority': {'deviceKind': 'android', 'physicalId': 'physical-two'},
        }
        lab = Lab(
            [descriptor], self.root / 'legacy-live',
            authority=self.authority, parent_grant=self.lab.parent_grant,
        )
        try:
            session = lab.create_session('legacy-alias', 'owner', 'browser')
            self.assertEqual(session['state'], 'failed')
            self.assertFalse(provider.started)
            self.assertEqual(lab.list_devices()[0]['state'], 'available')
            with Lease('physical-two', self.root / 'leases'):
                pass
        finally:
            lab.close_all()

    def test_ios_startup_requires_terminal_ack_with_same_typed_live_grant(self):
        handle = self.authority.claim_device(
            device_kind='ios-simulator', physical_id='simulator-two',
            display_alias='simulator-alias', helper_incarnation='helper_ios',
            parent_grant=self.lab.parent_grant,
        )
        admission = handle.admit_operation(
            operation_id='operation_'+'c'*40, payload_digest='d'*64,
            session_id='session_ios', sequence=1,
        )
        permit = handle.prepare_dispatch(admission, provider_incarnation='provider_ios')
        provider = IosProvider('simulator-two', self.root, 'io.reproof.fixture', {})
        provider.device_authority = handle
        provider.provider_incarnation = 'provider_ios'
        provider._startup_permit = permit
        provider.handshake_ready = threading.Event()
        with provider._startup_lock:
            provider._startup_accepting = True
            provider._startup_deadline = time.monotonic() + providers.IOS_STARTUP_TIMEOUT_SECONDS
        ready = provider.bridge('ready', {
            'capabilities': {}, 'protocolVersion': NATIVE_PROTOCOL_VERSION,
            'helperVersion': HELPER_VERSION, 'helperIncarnation': permit.helper_incarnation,
            'hostIncarnation': permit.host_incarnation,
            'providerIncarnation': permit.provider_incarnation,
            'nativeIncarnation': 'native_ios', 'nativeClockId': 'ios-mach-continuous',
            'nativeTimeMs': self.clock.now // 1_000_000,
        })
        self.assertFalse(provider.handshake_ready.is_set())
        malformed = {'authority': dict(ready['authority'], sequence=True)}
        with self.assertRaises(LiveError) as rejected:
            provider.bridge('started', malformed)
        self.assertEqual(rejected.exception.code, 'authority_rejected')
        self.assertFalse(provider.handshake_ready.is_set())
        self.assertEqual(provider.bridge('started', {'authority': ready['authority']}), {'ok': True})
        self.assertTrue(provider.handshake_ready.is_set())
        pending={'event':threading.Event(),'result':None,'authority':ready['authority']}
        provider.pending[permit.operation_id]=pending
        mismatched=dict(ready['authority'],providerIncarnation='provider_other')
        with self.assertRaises(LiveError) as rejected_ack:
            provider.bridge('ack',{'id':permit.operation_id,'ok':True,'timing':'best-effort',
                                   'authority':mismatched})
        self.assertEqual(rejected_ack.exception.code,'authority_rejected')
        self.assertFalse(pending['event'].is_set())
        self.assertEqual(provider.bridge('ack',{'id':permit.operation_id,'ok':True,
                         'timing':'best-effort','authority':ready['authority']}),{'ok':True})
        self.assertTrue(pending['event'].is_set())
        handle.confirm_operation(permit, ProviderResult('receipt_ios', 'succeeded', 'e'*64))
        handle.close()


if __name__ == "__main__":
    unittest.main()
