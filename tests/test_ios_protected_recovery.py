"""Registered iOS recovery service and CLI over owned native protocol doubles."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.live.access import AccessController, AccessStore
from reproof.live.configuration import issue_bounded_project_grant
from reproof.live.issue_configuration import IssueRuntimeBundle
from reproof.live.issue_workflow import IssueWorkflow, ProjectIssueRuntime
from reproof.live.protected_recovery import ProtectedRecoveryService
from reproof.live.server import LiveServer
from reproof.protected_mobile_inputs import recovery_device_descriptors
from reproof.repair_configuration import ProtectedServiceConfiguration
from tests import test_ios_recovery_execution as support
from tests.test_protected_service_configuration import configuration, issue_configuration
from tests.g4_support import runtime_policy


class IOSProtectedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = support.IOSRecoveryExecutionTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.config = config = self.f.fixture.config
        self.root = self.f.fixture.root
        self.lab = config.lab
        self.lab.authority = self.f.authority
        self.lab.project_grant_provider = lambda registration: issue_bounded_project_grant(
            self.f.authority, registration.project['id'], lifetime_seconds=180)
        self.f.device.close()
        self.access_store = AccessStore(self.root/'recovery-access')
        self.addCleanup(self.access_store.close)
        store = self.access_store
        store.bootstrap_administrator('admin')
        store.register_project('admin', config.registration.project)
        store.assign_device('admin', config.device_id, project_id=config.registration.project['id'])
        self.principals = {}; self.tokens = {}
        for name, role in (('operator', 'operator'), ('viewer', 'viewer')):
            store.create_identity('admin', name)
            store.grant_membership('admin', config.registration.project['id'], name, role)
            self.tokens[name] = store.issue_principal_credential('admin', name, lifetime_seconds=600)['token']
            self.principals[name] = store.authenticate_principal(self.tokens[name])
        self.access = AccessController(store); self.access.bind_project(config.registration)
        document = configuration(self.root)
        row = document['profiles'][0]
        row.update(projectId=config.registration.project['id'], projectDigest=config.registration.project_digest,
            applicationId=config.application_id, originalBuildId=config.original_build_id,
            platform='ios', deviceId=config.device_id, runtimePolicyDigest=config.runtime_policy_digest)
        for phase in ('build', 'mobile'):
            row[phase]['route']['projectDigest'] = row['projectDigest']
        row['validation']['plan']['projectDigest'] = row['projectDigest']
        row['mobile']['route']['environmentDigest'] = self.f.fixture.runs.environment_digest
        row['mobile']['journal'].update(root=str(self.f.fixture.runs.root),
            environmentDigest=self.f.fixture.runs.environment_digest,
            diskBudgetBytes=self.f.fixture.runs.disk_limit)
        row['mobile']['ownerRoot'] = str(self.f.operations.root)
        self.issue = issue_configuration(row, runtime_policy())
        self.bundle = IssueRuntimeBundle(self.root/'recovery-issues')
        runtime = ProjectIssueRuntime(config.registration, config.service, config.preparations,
            runtime_policy(), tuple(self.issue['projects'][0]['validationRecipeIds']))
        self.bundle.workflow = IssueWorkflow(self.bundle.root/'workflow', self.lab, self.access, [runtime])
        self.addCleanup(self.bundle.close)
        def reference(path):
            return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        def write(name, value):
            path = self.root/name
            path.write_text(json.dumps(value)); path.chmod(0o600)
            return reference(path)
        self.definition = {'schemaVersion': 1, 'kind': 'ios-mobile-definition-v1', 'owner': config.owner,
            'udid': config.query.udid, 'coreDeviceIdentifier': config.query.identifier,
            'runtimeProfile': write('recovery-profile.json', config.original_profile.data),
            'query': {'devicectlPath': str(config.query.tools.devicectl), 'devicectlSha256': config.query.tools.sha256,
                'workRoot': str(config.query.work_root),
                'nativeGuardian': {'path': str(config.query.native_guardian.path), 'sha256': config.query.native_guardian.sha256}},
            'baselines': [{'role': item.role, 'bundleId': item.bundle_id,
                'archive': {**reference(item.path), 'bytes': item.bytes}} for item in config.baselines],
            'preparations': [{'fixtureId': item.plan.fixture_id, 'payloadDigest': contracts.digest(item.payload)}
                             for item in config.preparations],
            'xctest': {'xcodebuildPath': str(config.xctest.xcodebuild), 'xcodebuildSha256': config.xctest.sha256,
                'developerRoot': str(config.xctest.developer_root), 'template': reference(config.xctest.template.path),
                'port': config.xctest.port},
            'sanitation': write('recovery-sanitation.json', config.sanitation.data)}
        row['mobile']['definition'] = write('recovery-mobile-definition.json', self.definition)
        self.configuration = ProtectedServiceConfiguration(document)
        self.service = ProtectedRecoveryService(self.configuration, self.issue, self.bundle)
        self.addCleanup(self.service.close)

    def test_inert_ios_inventory_and_original_status(self):
        with patch('subprocess.Popen', side_effect=AssertionError('metadata launched process')), \
                patch('socket.socket', side_effect=AssertionError('metadata used network')):
            devices = recovery_device_descriptors(self.configuration)
            status = self.service.status(self.principals['viewer'], 'protected-ios', self.f.fixture.context.operation_id)
            rows = self.service.operations(self.principals['viewer'], 'protected-ios')
        self.assertEqual(devices[0]['kind'], 'ios-physical')
        self.assertEqual(devices[0]['capabilities']['actions'], [])
        self.assertGreater(status['reservedBytes'], 0)
        self.assertEqual(len(rows['operations']), 1)
        self.assertNotIn(self.config.query.udid, json.dumps(status))

    def test_ios_cli_uses_authenticated_service_and_preserves_viewer_denial(self):
        server = LiveServer(self.lab, access=self.access, issue_workflow=self.bundle.workflow,
            protected_recovery=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close():
            server.close_operations(); server.shutdown(); thread.join(3); server.server_close()
        self.addCleanup(close)
        def cli(action, *args):
            result = subprocess.run([sys.executable, '-m', 'reproof', 'protected-service', action,
                '--server', server.origin, '--credential-stdin', '--profile', 'protected-ios',
                '--operation', self.f.fixture.context.operation_id, *args],
                input=self.tokens['viewer']+'\n', text=True, capture_output=True, timeout=15)
            self.assertNotIn(self.tokens['viewer'], result.stdout+result.stderr)
            self.assertNotIn(self.config.query.udid, result.stdout+result.stderr)
            return result
        status = cli('ios-status')
        self.assertEqual(status.returncode, 0, status.stdout+status.stderr)
        self.assertEqual(json.loads(status.stdout)['kind'], 'protected-ios-recovery')
        denied = cli('ios-recover', '--request-digest', self.f.fixture.context.request_digest)
        self.assertNotEqual(denied.returncode, 0)
        self.assertGreater(self.f.fixture.runs.status(self.f.fixture.context.operation_id)['reservedBytes'], 0)

    def test_service_completes_measured_recovery_and_releases_original_reservation(self):
        job = self.service.start(self.principals['operator'], profile_id='protected-ios',
            operation_id=self.f.fixture.context.operation_id,
            request_digest=self.f.fixture.context.request_digest, request_id='ios-recovery-service', timeout_seconds=90)
        deadline = time.monotonic()+95
        while time.monotonic() < deadline:
            job = self.service.job(self.principals['operator'], job['id'])
            if job['state'] in ('succeeded', 'failed', 'cancelled'):
                break
            time.sleep(.05)
        self.assertEqual(job['state'], 'succeeded', job)
        self.assertEqual(job['result']['reservedBytes'], 0)
        self.assertEqual(self.lab.devices[self.config.device_id]['state'], 'available')
