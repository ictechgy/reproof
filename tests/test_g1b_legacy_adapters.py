"""Legacy mutation adapters must honor the shared canonical device lease."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reproloop.core import ContractError
from reproloop.device import AdbDevice
from reproloop.ios_device import IosPhysicalDevice
from reproloop.ios_runner import IosSimulator
from reproloop.live.authority import HostAuthority, issue_local_parent_grant
from reproloop.live.clock_sync import ClockReading
from reproloop.storage import PACKAGE


class Clock:
    def read(self):
        return ClockReading('legacy-adapter-clock', 'd' * 64, 1_000_000_000, 0)


class G1bLegacyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='g1b-legacy-adapter-')
        self.root = Path(self.temporary.name)
        self.temp_patch = patch('tempfile.gettempdir', return_value=str(self.root))
        self.temp_patch.start()
        self.authority = HostAuthority(clock=Clock())
        self.grant = issue_local_parent_grant(
            self.authority, lifetime_ns=60_000_000_000)

    def tearDown(self):
        self.authority.close()
        self.temp_patch.stop()
        self.temporary.cleanup()

    def own(self, kind, physical_id):
        return self.authority.claim_device(
            device_kind=kind, physical_id=physical_id,
            helper_incarnation='parent-helper', parent_grant=self.grant)

    def test_android_install_is_rejected_before_adb_effect(self):
        serial = 'synthetic-android-parent'
        self.own('android', serial)
        effects = []

        def command(_device, args, timeout=None):
            if args == ['probe-adb', 'devices']:
                return ('List of devices attached\n' + serial + '\tdevice\n').encode()
            effects.append(list(args))
            return b'Success\n'

        with patch.object(AdbDevice, '_command', command):
            device = AdbDevice(serial=serial, adb='probe-adb')
            with patch.object(device, 'apk_package', return_value=PACKAGE), \
                 patch.object(device, 'installation_proof', return_value={'ok': True}):
                with self.assertRaises(ContractError):
                    device.install(self.root / 'synthetic.apk')
        self.assertEqual(effects, [])

    def test_simulator_stop_is_rejected_before_simctl_effect(self):
        udid = '11111111-2222-3333-4444-555555555555'
        self.own('ios-simulator', udid)
        effects = []

        def command(args, cwd, **kwargs):
            if args[2:5] == ['list', 'devices', '--json']:
                return json.dumps({'devices': {'synthetic-runtime': [
                    {'udid': udid, 'isAvailable': True, 'state': 'Booted'}]}})
            effects.append(list(args))
            return ''

        with patch('reproloop.ios_runner.run_command', side_effect=command):
            device = IosSimulator(udid)
            with self.assertRaises(ContractError):
                device.stop()
        self.assertEqual(effects, [])

    def test_physical_iphone_stop_is_rejected_before_devicectl_effect(self):
        physical_id = 'synthetic-iphone-parent'
        self.own('ios-physical', physical_id)
        effects = []
        descriptor = SimpleNamespace(
            udid=physical_id, identifier='synthetic-device-id',
            public_id='synthetic-public-id')

        def devicectl(*args, **kwargs):
            if args[:3] == ('device', 'info', 'processes'):
                return {'runningProcesses': [{
                    'executable': '/synthetic/ReproSample.app/ReproSample',
                    'processIdentifier': 12345}]}
            effects.append(list(args))
            return {}

        with patch('reproloop.ios_device.select_iphone', return_value=descriptor), \
             patch('reproloop.ios_device.public_device_status', return_value={'ready': True}), \
             patch('reproloop.ios_device._devicectl', side_effect=devicectl):
            device = IosPhysicalDevice('synthetic-public-id')
            with self.assertRaises(ContractError):
                device.stop()
        self.assertEqual(effects, [])


if __name__ == '__main__':
    unittest.main()
