"""Authenticated recovery uses public input loading and real owned SDK execution."""
from contextlib import contextmanager
from dataclasses import replace
import copy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.live.access import AccessController, AccessStore
from reproof.live.configuration import issue_bounded_project_grant
from reproof.live.issue_configuration import IssueRuntimeBundle
from reproof.live.issue_workflow import IssueWorkflow, ProjectIssueRuntime
from reproof.live.model import LiveError
from reproof.repair_configuration import ProtectedServiceConfiguration
from tests import test_android_recovery_finalization as support
from tests.test_protected_service_configuration import configuration, issue_configuration
from tests.test_protected_mobile_inputs import blob, sha
from tests.g4_support import runtime_policy


class ProtectedRecoveryServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AndroidRecoveryFinalizationTests.setUpClass()
        cls.addClassCleanup(support.AndroidRecoveryFinalizationTests.doClassCleanups)

    def setUp(self):
        self.f=support.AndroidRecoveryFinalizationTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups);self.f.setUp()
        config=self.f.f.config;self.lab=config.lab;self.root=self.f.operations.root.parent
        self.access_store=AccessStore(self.root/'recovery-access');self.addCleanup(self.access_store.close)
        store=self.access_store;store.bootstrap_administrator('admin')
        store.register_project('admin',config.registration.project)
        store.assign_device('admin',config.device_id,project_id=config.registration.project['id'])
        self.tokens={};self.principals={}
        for name,role in (('operator','operator'),('viewer','viewer'),('outsider',None)):
            store.create_identity('admin',name)
            if role:store.grant_membership('admin',config.registration.project['id'],name,role)
            self.tokens[name]=store.issue_principal_credential('admin',name,lifetime_seconds=600)['token']
            self.principals[name]=store.authenticate_principal(self.tokens[name])
        self.access=AccessController(store);self.access.bind_project(config.registration)
        self.lab.project_grant_provider=lambda registration: issue_bounded_project_grant(
            self.lab.authority,registration.project['id'],lifetime_seconds=180)
        self.document=configuration(self.root);row=self.document['profiles'][0]
        row.update(id='protected-android',projectId=config.registration.project['id'],
            projectDigest=config.registration.project_digest,applicationId=config.application_id,
            originalBuildId=config.original_build_id,platform='android',deviceId=config.device_id,
            runtimePolicyDigest=config.runtime_policy_digest)
        for phase in ('build','mobile'):row[phase]['route']['projectDigest']=row['projectDigest']
        row['mobile']['route'].update(platform='android',artifactPolicyId='signed-apk',
            environmentDigest=self.f.runs.environment_digest)
        row['mobile']['journal'].update(root=str(self.f.runs.root),environmentDigest=self.f.runs.environment_digest,
                                       diskBudgetBytes=self.f.runs.disk_limit)
        row['mobile']['ownerRoot']=str(self.f.operations.root)
        row['signing']['policy'].update(platform='android',tool='host-apksigner-fixed')
        row['signing']['policy'].pop('provisioningReferenceId')
        row['validation']['plan']['projectDigest']=row['projectDigest']
        self.issue=issue_configuration(row,runtime_policy())
        self.bundle=IssueRuntimeBundle(self.root/'recovery-issues')
        runtime=ProjectIssueRuntime(config.registration,config.service,config.preparations,runtime_policy(),
                                    tuple(self.issue['projects'][0]['validationRecipeIds']))
        self.bundle.workflow=IssueWorkflow(self.bundle.root/'workflow',self.lab,self.access,[runtime])
        self.addCleanup(self.bundle.close)
        profile=self.root/'recovery-runtime-profile.json';profile.write_text(json.dumps(config.original_profile.data));profile.chmod(0o600)
        definition={'schemaVersion':1,'kind':'android-mobile-definition-v1','owner':config.owner,'serial':config.serial,
            'runtimeProfile':{'path':str(profile),'sha256':sha(profile)},
            'originalApk':blob(config.original_apk),'helperApk':blob(config.helper_apk),
            'tools':{'adbPath':str(config.tools.adb),'adbSha256':config.tools.adb_digest,
                'packageInspectorPath':str(config.tools.package_inspector),'packageInspectorSha256':config.tools.package_inspector_digest},
            'preparations':[{'fixtureId':item.plan.fixture_id,'payloadDigest':contracts.digest(item.payload)} for item in config.preparations],
            'adbEndpoint':{'socketPath':str(config.adb_endpoint.socket_path),'serverVersion':config.adb_endpoint.server_version,
                           'sandboxSha256':config.adb_endpoint.sandbox_sha256},
            'nativeGuardian':{'path':str(config.native_guardian.path),'sha256':config.native_guardian.sha256}}
        path=Path(row['mobile']['definition']['path']);path.write_text(json.dumps(definition));path.chmod(0o600)
        row['mobile']['definition']['sha256']=sha(path)
        self.configuration=ProtectedServiceConfiguration(self.document)
        from reproof.live.protected_recovery import ProtectedRecoveryService
        self.recovery=ProtectedRecoveryService(self.configuration,self.issue,self.bundle)
        self.addCleanup(lambda:self.recovery.close(deadline_monotonic=time.monotonic()+10))
        if not getattr(self,'keep_owner_open',False):
            self.f.device.close()  # The failed prior owner has already stopped.

    def start(self,**changes):
        values=dict(profile_id='protected-android',operation_id=self.f.f.operation.operation_id,
            request_digest=self.f.f.operation.request_digest,request_id='recovery-request',timeout_seconds=30)
        values.update(changes)
        return self.recovery.start(self.principals['operator'],**values)

    def wait(self,job):
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            current=self.recovery.job(self.principals['operator'],job['id'])
            if current['state'] in ('succeeded','failed','cancelled'):return current
            time.sleep(.02)
        self.fail('Owned recovery did not finish')

    def test_status_uses_original_configuration_without_device_effects(self):
        status=self.recovery.status(self.principals['viewer'],'protected-android',self.f.f.operation.operation_id)
        self.assertEqual(status['operation']['requestDigest'],self.f.f.operation.request_digest)
        self.assertGreater(status['reservedBytes'],0)
        self.assertEqual(self.f.helper.server.requests,[])
        self.assertNotIn(self.f.f.config.serial,json.dumps(status))
        self.assertNotIn(str(self.root),json.dumps(status))

    def test_recovery_startup_inventory_does_not_probe_or_offer_normal_device_effects(self):
        from reproof.protected_mobile_inputs import recovery_device_descriptors
        with patch('subprocess.Popen',side_effect=AssertionError('startup dispatched a process')), \
                patch('socket.socket',side_effect=AssertionError('startup contacted a device')):
            devices=recovery_device_descriptors(self.configuration)
        self.assertEqual([device['id'] for device in devices],[self.f.f.config.device_id])
        self.assertEqual(devices[0]['capabilities']['actions'],[])
        self.assertTrue(devices[0]['capabilities']['recoveryOnly'])
        self.assertEqual(devices[0]['capabilities']['applicationIdentity'],self.f.f.config.original_profile.application_identity)
        with self.assertRaises(LiveError):self.lab._check_device_admission(devices[0])
        with self.assertRaises(LiveError):devices[0]['factory']()

    def test_authenticated_recovery_releases_original_scope_and_returns_device(self):
        result=self.wait(self.start())
        self.assertEqual(result['state'],'succeeded',result)
        self.assertTrue(result['result']['ownershipReleased'])
        self.assertEqual(result['result']['reservedBytes'],0)
        self.assertEqual(self.lab.devices[self.f.f.config.device_id]['state'],'available')
        self.assertEqual(self.lab._device_reservations,{})
        self.assertEqual(self.lab._retained_device_scopes,{})

    def test_viewer_and_outsider_cannot_start_recovery(self):
        for role in ('viewer','outsider'):
            with self.subTest(role=role),self.assertRaises(LiveError):
                self.recovery.start(self.principals[role],profile_id='protected-android',
                    operation_id=self.f.f.operation.operation_id,request_digest=self.f.f.operation.request_digest,
                    request_id='not-authorized',timeout_seconds=30)
        self.assertEqual(self.f.helper.server.requests,[])

    def test_wrong_original_request_keeps_reservation_and_device_quarantined(self):
        result=self.wait(self.start(request_digest='0'*64))
        self.assertEqual(result['state'],'failed')
        self.assertEqual(self.f.helper.server.requests,[])
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)

    def test_request_identity_is_idempotent_and_cannot_change_binding(self):
        first=self.start()
        again=self.start()
        self.assertEqual(first['id'],again['id'])
        with self.assertRaises(LiveError):self.start(request_digest='0'*64)
        self.assertEqual(self.wait(first)['state'],'succeeded')

    def test_revocation_during_helper_status_prevents_final_release(self):
        original=self.f.helper.server.helper_response
        def revoke(headers,body):
            value=original(headers,body)
            self.access_store.revoke_membership('admin',self.f.f.config.registration.project['id'],'operator','operator')
            return value
        self.f.helper.server.helper_response=revoke
        job=self.start();deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            result=self.recovery.job(self.principals['viewer'],job['id'])
            if result['state'] in ('failed','cancelled'):break
            time.sleep(.02)
        self.assertIn(result['state'],('failed','cancelled'))
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)
        self.assertEqual(self.lab.devices[self.f.f.config.device_id]['state'],'quarantined')

    def test_forged_principal_cannot_reuse_a_known_credential_identity(self):
        forged=replace(self.principals['operator'],_issuer=object())
        with self.assertRaises(LiveError):
            self.recovery.start(forged,profile_id='protected-android',operation_id=self.f.f.operation.operation_id,
                request_digest=self.f.f.operation.request_digest,request_id='forged',timeout_seconds=30)
        self.assertEqual(self.f.helper.server.requests,[])

    def test_active_retained_owner_cannot_be_taken_over(self):
        owner=ProtectedRecoveryServiceTests(methodName='runTest');owner.keep_owner_open=True
        self.addCleanup(owner.doCleanups);owner.setUp()
        job=owner.wait(owner.start())
        self.assertEqual(job['state'],'failed')
        self.assertFalse(owner.f.device._closed)
        self.assertEqual(owner.f.helper.server.requests,[])

    def test_cancellation_before_claim_preserves_original_reservation(self):
        ready=threading.Event();proceed=threading.Event()
        original=self.recovery._operations
        @contextmanager
        def paused(profile):
            with original(profile) as operations:
                ready.set();proceed.wait(5)
                yield operations
        with patch.object(self.recovery,'_operations',side_effect=paused):
            job=self.start()
            try:
                self.assertTrue(ready.wait(3))
                self.recovery.cancel(self.principals['operator'],job['id'])
            finally:proceed.set()
            result=self.wait(job)
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(self.f.helper.server.requests,[])
        self.assertGreater(self.f.runs.status(self.f.f.operation.operation_id)['reservedBytes'],0)

    def test_startup_composition_registers_recovery_without_starting_device_effects(self):
        from reproof.live.protected_recovery import compose_recovery_workflow
        configured=copy.deepcopy(self.issue)
        plans=self.f.f.config.preparations
        recipes={row['id']:row for row in self.f.f.config.registration.project['fixtures']}
        configured['projects'][0]['fixtures']=[{
            'applicationId':item.plan.application_id,'fixtureId':item.plan.fixture_id,
            'endpointId':recipes[item.plan.cleanup_recipe_id]['endpointId'],
            'baseUrl':f'http://127.0.0.1:{self.f.f.f.f.remote.port}',
            'checkRecipeIds':list(item.plan.check_recipe_ids),'cleanupRecipeId':item.plan.cleanup_recipe_id,
            'payload':item.payload} for item in plans]
        bundle=compose_recovery_workflow(self.lab,self.access,self.configuration,configured,
                                         root=self.root/'startup-issues')
        self.addCleanup(bundle.close)
        self.assertIsNotNone(bundle.protected_recovery)
        self.assertIsNone(bundle.workflow.repairs)
        self.assertEqual(bundle.protected_recovery.profiles(self.principals['viewer'])['profiles'][0]['profileId'],
                         'protected-android')
        self.assertEqual(self.f.helper.server.requests,[])
