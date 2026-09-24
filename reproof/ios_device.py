"""Physical iPhone adapter for the same protected sample replay/repair runner."""
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from .core import require
from .ios_core import APPLICATION_ID, PHYSICAL_EVIDENCE_KIND, validate_finalization
from .ios_runner import (IosSimulator, MAX_AUTO_JSON, _require_profile_fixture,
                         validate_ios_auto_diagnostics, validate_ios_auto_marker,
                         _leased_effect)
from .ios_storage import app_info
from .storage import Lease, read_json
from .live.iphone import select_iphone, _devicectl, public_device_status


class IosPhysicalDevice(IosSimulator):
    execution_environment = 'physical-iphone'

    def __init__(self, public_id=None):
        self.device = select_iphone(public_id)
        require(public_device_status(self.device)['ready'], 'Physical iPhone is not ready')
        self.udid = self.device.udid
        self.identity = self.device.public_id
        self.installed_build_id = None

    def lease(self):
        return self._lease_for('ios-device:' + self.udid)

    @_leased_effect
    def install(self, app):
        app = Path(app).resolve()
        expected = app_info(app)
        require((app / 'embedded.mobileprovision').is_file(), 'Physical fixture is not provisioned')
        signed = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
                                capture_output=True, timeout=20)
        require(signed.returncode == 0, 'Physical fixture signature is invalid')
        _devicectl('device', 'install', 'app', '--device', self.device.identifier, str(app), timeout=90)
        self.installed_build_id = expected['buildId']
        # The frozen XCTest runner verifies the embedded build ID at runtime.
        return {'applicationId': APPLICATION_ID, 'buildId': expected['buildId'], 'deviceId': self.identity,
                'executionEnvironment': self.execution_environment, 'evidenceKind': PHYSICAL_EVIDENCE_KIND,
                'installationAcknowledged': True, 'signatureVerified': True}

    @_leased_effect
    def stop(self):
        listing = _devicectl('device', 'info', 'processes', '--device', self.device.identifier)
        require(isinstance(listing.get('runningProcesses'), list), 'Device did not confirm process state')
        for process in listing['runningProcesses']:
            executable = process.get('executable', '')
            if isinstance(executable, str) and executable.endswith(('/ReproSample.app/ReproSample', '/ReproReplayTests-Runner.app/ReproReplayTests-Runner')):
                pid = process.get('processIdentifier')
                require(type(pid) is int and pid > 0, 'Invalid sample process identity')
                _devicectl('device', 'process', 'terminate', '--device', self.device.identifier, '--pid', str(pid))

    @_leased_effect
    def read_app_json(self, relative, *, max_bytes=MAX_AUTO_JSON, application_id=APPLICATION_ID):
        require(type(application_id) is str and len(application_id) <= 180
                and re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+', application_id),
                'Invalid selected iOS application identity')
        require(isinstance(relative, str), 'Unsafe iOS application JSON path')
        path = PurePosixPath(relative)
        require(not path.is_absolute() and '..' not in path.parts and str(path) == relative,
                'Unsafe iOS application JSON path')
        with tempfile.TemporaryDirectory(prefix='repro-device-capture-') as directory:
            root = Path(directory)
            path = root / 'value.json'
            _devicectl('device', 'copy', 'from', '--device', self.device.identifier,
                '--domain-type', 'appDataContainer', '--domain-identifier', application_id,
                '--source', 'Library/Application Support/Reproof/' + relative,
                '--destination', str(path), timeout=30)
            require(path.is_file() and not path.is_symlink() and path.stat().st_size <= max_bytes,
                    'Missing or oversized iOS application JSON')
            return read_json(path)

    def pin_auto_marker(self, expected_run_id, auto_profile, *, expected_fixture,
                        min_started_at=None, require_finalized=False):
        require(isinstance(expected_run_id, str) and auto_profile is not None,
                'Automatic capture requires a run id and profile')
        _require_profile_fixture(auto_profile, expected_fixture)
        marker = self.read_app_json('auto-session.json')
        build_id = self.installed_build_id
        require(isinstance(build_id, str), 'Installed iOS application build identity is unavailable')
        validate_ios_auto_marker(marker, auto_profile, run_id=expected_run_id,
                                 build_id=build_id, fixture=expected_fixture,
                                 min_started_at=min_started_at)
        if require_finalized:
            require(marker.get('finalized') is True, 'Automatic iOS session is not finalized')
        return marker

    def collect_capture(self, min_started_at=None, *, expected_run_id=None, auto_profile=None, expected_fixture=None):
        auto = expected_run_id is not None or auto_profile is not None
        auto_marker = None
        if auto:
            require(expected_run_id is not None and auto_profile is not None,
                    'Automatic capture requires a run id and profile')
            require(expected_fixture is not None, 'Automatic capture requires its expected fixture')
            auto_marker = self.pin_auto_marker(expected_run_id, auto_profile, expected_fixture=expected_fixture,
                                          min_started_at=min_started_at,
                                          require_finalized=True)
        capture = self.read_app_json('capture.json')
        session = capture.get('sessionId')
        require(isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session), 'Invalid capture session path')
        metadata = self.read_app_json(session + '/metadata.json')
        finalized = self.read_app_json(session + '/finalized.json')
        validate_finalization(capture, metadata, finalized, min_started_at)
        if auto:
            pinned = self.read_app_json('auto-session.json')
            require(pinned == auto_marker and pinned.get('sessionId') == capture.get('sessionId')
                    and pinned.get('endSequence') == capture.get('endSequence')
                    and pinned.get('fixture') == expected_fixture
                    and pinned.get('finalized') is True,
                    'Automatic capture marker differs from finalized capture')
            require(self.read_app_json('auto-session.json') == auto_marker,
                    'Automatic capture marker changed while collecting')
        return capture

    def collect_auto_diagnostics(self, capture, expected_run_id, auto_profile, *, expected_fixture):
        require(isinstance(capture, dict) and isinstance(expected_run_id, str) and auto_profile is not None,
                'Automatic diagnostics identity is invalid')
        require(expected_fixture is not None, 'Automatic diagnostics requires its expected fixture')
        marker = self.pin_auto_marker(expected_run_id, auto_profile, expected_fixture=expected_fixture,
                                      require_finalized=True)
        build_id = self.installed_build_id
        require(isinstance(build_id, str), 'Installed iOS application build identity is unavailable')
        session = capture.get('sessionId')
        require(isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session),
                'Invalid automatic diagnostics session path')
        require(marker.get('sessionId') == session and marker.get('endSequence') == capture.get('endSequence')
                and marker.get('fixture') == expected_fixture,
                'Automatic diagnostics marker differs from capture')
        diagnostics = self.read_app_json(session + '/diagnostics.json')
        require(self.read_app_json('auto-session.json') == marker,
                'Automatic diagnostics marker changed while collecting')
        return validate_ios_auto_diagnostics(diagnostics, capture, auto_profile,
                                             run_id=expected_run_id, build_id=build_id)
