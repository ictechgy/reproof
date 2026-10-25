"""Android startup diagnostics using only deterministic clocks and wire doubles."""
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from reproof.core import ContractError
from reproof.live import android_live as android
from reproof.live.model import ANDROID_STARTUP_DIAGNOSTICS, LiveError


class Clock:
    now=0.0
    def monotonic(self):return self.now


class Event:
    def __init__(self,clock):self.clock=clock;self.set_called=False;self.waits=[]
    def set(self):self.set_called=True
    def wait(self,timeout):
        self.waits.append(timeout);self.clock.now+=timeout
        return self.set_called


class Stop:
    def __init__(self,clock,provider):self.clock=clock;self.provider=provider;self.stopped=False
    def set(self):self.stopped=True
    def is_set(self):return self.stopped
    def wait(self,timeout):
        if self.stopped or self.provider._startup_result=='ok':return True
        self.clock.now+=timeout
        return False


class InlineThread:
    def __init__(self,*,target,**kwargs):self.target=target
    def start(self):self.target()


class AndroidStartupWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.device=mock.Mock()
        self.device.apk_identity.return_value={'package':android.HELPER,'versionCode':android.HELPER_VERSION}
        self.device.adb_call.return_value=b'12345'
        self.device.adb='fake-adb';self.device.serial='never-used'
        with mock.patch.object(android,'AdbDevice',return_value=self.device), mock.patch.object(android,'sha_file',return_value='a'*64):
            self.provider=android.AndroidLiveProvider('never-used','helper.apk','sample.apk')
        p=self.provider
        p.device_authority=mock.Mock();p.lease=mock.MagicMock()
        p.stop=Stop(self.clock,p)
        self.permit=SimpleNamespace(helper_incarnation='helper',host_incarnation='host',provider_incarnation='provider')
        self.process=mock.Mock();self.process.poll.return_value=None
        self.transport=mock.Mock();self.transport.call.side_effect=lambda *a,**k:self.metadata()
        self.lab=mock.Mock();self.installs=[]
        def install(apk,package,permit):
            self.installs.append(package)
            self.clock.now+=40 if package==android.HELPER else 30
        p._install=install
        self.patches=[mock.patch.object(android.time,'monotonic',self.clock.monotonic),
            mock.patch.object(android.threading,'Event',side_effect=lambda:Event(self.clock)),
            mock.patch.object(android.threading,'Thread',InlineThread),
            mock.patch.object(android.subprocess,'run',return_value=SimpleNamespace(returncode=0)),
            mock.patch.object(android.subprocess,'Popen',return_value=self.process),
            mock.patch.object(android,'UsbBridgeClient',return_value=self.transport),
            mock.patch('reproof.live.native_frame_clock.NativeFrameClock.for_session',return_value=None)]
        for patch in self.patches:patch.start();self.addCleanup(patch.stop)

    def metadata(self):
        return {'profileDigest':None,'nativeDigest':self.provider.app_profile.native_digest,'ready':True}

    def start(self):return self.provider.start_authorized({'id':'synthetic'},self.lab,self.permit)

    def assert_failure(self,code):
        with self.assertRaises(LiveError) as error:self.start()
        self.assertEqual(error.exception.code,code)
        self.assertEqual(str(error.exception),ANDROID_STARTUP_DIAGNOSTICS[code])
        self.assertNotIn(self.provider.target_package,self.installs)
        self.assertFalse(self.provider.target_launched)
        self.assertTrue(self.provider.stop.is_set())
        self.lab.fail.assert_not_called()

    def test_success_deadline_starts_after_install_and_does_not_cover_target_install(self):
        self.assertEqual(self.start(),{'ok':True})
        self.assertEqual(self.provider._startup_deadline,55)
        self.assertGreater(self.clock.now,70)
        self.assertTrue(self.provider.target_launched)
        self.assertEqual(self.installs,[android.HELPER,self.provider.target_package])
        self.provider.device_authority.check_dispatch_permit.assert_called_with(self.permit)

    def test_process_exit_is_distinct_and_never_launches_target(self):
        self.process.poll.return_value=1
        self.assert_failure('android_startup_process_exited')

    def test_metadata_and_bind_rejection_are_sanitized(self):
        self.transport.call.return_value={}
        self.transport.call.side_effect=lambda *a,**k:{'nativeDigest':'private-payload'}
        self.assert_failure('android_startup_metadata_rejected')

    def test_bind_contract_rejection_with_valid_permit(self):
        self.provider.device_authority.bind_native_handshake.side_effect=ContractError('private token')
        self.assert_failure('android_startup_metadata_rejected')

    def test_original_permit_expiry_during_bind_is_not_protocol_mismatch(self):
        authority=self.provider.device_authority
        def bind(*a,**k):
            authority.check_dispatch_permit.side_effect=ContractError('private permit')
            raise ContractError('private permit')
        authority.bind_native_handshake.side_effect=bind
        self.assert_failure('android_startup_authority_invalid')
        self.assertTrue(all(call.args==(self.permit,) for call in authority.check_dispatch_permit.call_args_list))

    def test_transport_refusal_retries_to_shared_deadline_with_bounded_timeout(self):
        remaining=[]
        def refuse(*a,timeout,**k):
            remaining.append((timeout,55-self.clock.now))
            raise ConnectionRefusedError('private endpoint')
        self.transport.call.side_effect=refuse
        self.assert_failure('android_startup_transport_unavailable')
        self.assertGreater(len(remaining),1)
        self.assertTrue(all(0<t<=min(2,r) for t,r in remaining))
        self.assertLess(self.clock.now,55.026)

    def test_transient_transport_refusal_can_recover(self):
        self.transport.call.side_effect=[ConnectionRefusedError('private endpoint'),self.metadata()]
        self.assertEqual(self.start(),{'ok':True})
        self.assertEqual(self.transport.call.call_count,2)

    def test_late_response_and_late_binding_never_launch(self):
        def late(*a,**k):self.clock.now=55;return self.metadata()
        self.transport.call.side_effect=late
        self.assert_failure('android_startup_timeout')
        self.provider.device_authority.bind_native_handshake.assert_not_called()

    def test_binding_crossing_deadline_is_rejected(self):
        def late(*a,**k):self.clock.now=55;return object()
        self.provider.device_authority.bind_native_handshake.side_effect=late
        self.assert_failure('android_startup_timeout')
        self.assertIsNone(self.provider.native_handshake)

    def test_cancellation_during_status_is_distinct(self):
        def cancel(*a,**k):self.provider.stop.set();return self.metadata()
        self.transport.call.side_effect=cancel
        self.assert_failure('android_startup_cancelled')

    def test_missing_transport_is_distinct(self):
        with mock.patch.object(android,'UsbBridgeClient',return_value=None):
            self.assert_failure('android_startup_transport_unavailable')

    def test_managed_transport_device_error_is_not_metadata_rejection(self):
        self.transport.call.side_effect=android.DeviceError('private transport information')
        self.assert_failure('android_startup_transport_unavailable')
        self.assertEqual(self.transport.call.call_count,1)

    def test_managed_transport_endpoint_error_is_not_metadata_rejection(self):
        self.transport.call.side_effect=android.AdbEndpointError()
        self.assert_failure('android_startup_transport_unavailable')
        self.assertEqual(self.transport.call.call_count,1)

    def test_permit_expiry_during_helper_install_is_distinct(self):
        def install(*args):
            self.installs.append(android.HELPER)
            self.provider.device_authority.check_dispatch_permit.side_effect=ContractError('private permit')
        self.provider._install=install
        self.assert_failure('android_startup_authority_invalid')

    def test_process_exiting_during_status_never_binds_or_launches(self):
        def exit_during_status(*a,**k):
            self.process.poll.return_value=1
            return self.metadata()
        self.transport.call.side_effect=exit_during_status
        self.assert_failure('android_startup_process_exited')
        self.provider.device_authority.bind_native_handshake.assert_not_called()

    def test_failure_winning_during_bind_cannot_be_replaced_by_success(self):
        def bind(*a,**k):
            with self.provider._startup_lock:
                self.provider._finish_startup_locked('android_startup_cancelled')
            return object()
        self.provider.device_authority.bind_native_handshake.side_effect=bind
        self.assert_failure('android_startup_cancelled')
        self.assertIsNone(self.provider.native_handshake)

    def test_cancellation_during_target_install_cannot_launch(self):
        install=self.provider._install
        def cancel_after_install(apk,package,permit):
            install(apk,package,permit)
            if package==self.provider.target_package:self.provider.stop.set()
        self.provider._install=cancel_after_install
        with self.assertRaises(LiveError) as error:self.start()
        self.assertEqual(error.exception.code,'startup_unknown')
        self.assertFalse(self.provider.target_launched)

    def test_instrumentation_transport_start_failure_is_sanitized(self):
        with mock.patch.object(android.subprocess,'Popen',side_effect=OSError('private command')):
            self.assert_failure('android_startup_transport_unavailable')

    def test_invalid_response_after_deadline_is_timeout_not_metadata(self):
        def late(*a,**k):self.clock.now=55;return {'private':'payload'}
        self.transport.call.side_effect=late
        self.assert_failure('android_startup_timeout')

    def test_cancellation_during_target_ready_wait_cannot_send_launch(self):
        install=self.provider._install
        def general_after_install(apk,package,permit):
            install(apk,package,permit)
            if package==self.provider.target_package:self.provider.general_profile=True
        self.provider._install=general_after_install
        self.provider.helper_ready=mock.Mock()
        def cancel_wait(timeout):self.provider.stop.set();return True
        self.provider.helper_ready.wait.side_effect=cancel_wait
        self.provider._execute=mock.Mock()
        with self.assertRaises(LiveError) as error:self.start()
        self.assertEqual(error.exception.code,'startup_unknown')
        self.provider._execute.assert_not_called()

    def test_event_only_wakeup_cannot_authorize_success(self):
        p=self.provider;p.process=self.process;p._startup_permit=self.permit
        p.handshake_ready=Event(self.clock);p.handshake_ready.set();p._startup_deadline=1
        with self.assertRaises(LiveError) as error:p._wait_startup()
        self.assertEqual(error.exception.code,'android_startup_metadata_rejected')
        self.assertIsNone(p.native_handshake)
        self.assertLess(self.clock.now,1.026)

    def test_waiter_timeout_without_poller_uses_same_absolute_deadline(self):
        p=self.provider;p.process=self.process;p._startup_permit=self.permit
        p.handshake_ready=Event(self.clock);p._startup_deadline=1
        with self.assertRaises(LiveError) as error:p._wait_startup()
        self.assertEqual(error.exception.code,'android_startup_timeout')
        self.assertEqual(self.clock.now,1)

    def test_first_terminal_failure_wins_over_later_success_and_failure(self):
        p=self.provider;p.handshake_ready=Event(self.clock)
        with p._startup_lock:
            p._finish_startup_locked('android_startup_process_exited')
            p._finish_startup_locked('ok')
            p._finish_startup_locked('android_startup_timeout')
        self.assertEqual(p._startup_result,'android_startup_process_exited')
        self.assertTrue(p.handshake_ready.set_called)


if __name__=='__main__':unittest.main()
