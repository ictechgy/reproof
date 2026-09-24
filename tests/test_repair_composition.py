"""Service composition tests; VM/device/signature boundaries are explicit doubles."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from reproof import contracts
from reproof.execution.artifacts import ArtifactValidationAuthority
from reproof.execution.backend import QualificationAuthority
from reproof.execution.journal import RunStore
from reproof.execution.resources import provision
from reproof.live.access import AccessController, AccessStore
from reproof.live import issue_configuration
from reproof.repair_execution import RepairExecutionError
from tests.g9_execution_support import SyntheticRepairExecution
from tests.g9_support import RepairEnvironment
from tests.test_execution_protocol import build_route
from tests.test_execution_qualification import ProbeVMDouble
from tests.test_execution_resources import resource_inputs
from tests.test_issue_configuration import configuration
from tests.test_project_repair import EDIT


class BuildCompositionTests(unittest.TestCase):
    def setUp(self):
        from reproof.repair_composition import ProtectedRepairComposition
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        metadata, paths = resource_inputs(self.root)
        self.bundle = provision(self.root / 'bundle', metadata=metadata, resources=paths)
        self.store = RunStore(self.root / 'state', environment_digest=self.bundle.environment_digest, disk_limit=1024)
        self.composition = ProtectedRepairComposition(); self.addCleanup(self.composition.close)
        self.plan = {'schemaVersion': 1, 'id': 'validation', 'projectDigest': 'a' * 64,
            'checks': [{'id': 'external-ui', 'recipeId': 'regression', 'kind': 'external-observation',
                        'evidenceSourceId': 'device-observer'}], 'candidateReports': 'supplemental-only'}
        self.route = build_route(); self.route.update(projectDigest='a' * 64, recipeId='build',
            environmentDigest=self.bundle.environment_digest, validationPlanId='validation',
            cleanupPolicyId='dispose-overlay')
        self.artifacts = ArtifactValidationAuthority()
        self.artifacts.register('bounded-artifacts', paths=('product.bin',), max_bytes=4096, checker=lambda _: True)
        for name in ('stop_confirmed', 'network_denied', 'bounded_output_denied'):
            self.enterContext(mock.patch.object(ProbeVMDouble, name, True))
        self.enterContext(mock.patch('reproof.execution.qualification.NativeVM', ProbeVMDouble))

    def build(self, **changes):
        arguments = dict(bundle=self.bundle, store=self.store, route=self.route, validation_plan=self.plan,
            artifact_authority=self.artifacts, application_id='app')
        return self.composition.qualify_build(**{**arguments, **changes})

    def test_live_qualification_is_retained_for_the_builder_and_revoked_on_close(self):
        builder = self.build()
        self.assertIs(builder.backend.authority, self.composition.authority)
        self.assertIsNotNone(builder.qualification)
        self.assertEqual(builder.ready(project_digest='a' * 64, recipe_id='build',
            validation_plan_digest=contracts.digest(self.plan)), builder.definition_digest)
        report = self.composition.status()
        self.assertEqual([row['mode'] for row in report['builds'][0]['probes']],
                         ['containment', 'hold', 'oversize', 'forged-report'])
        self.assertNotIn('qualification', report['builds'][0])
        self.composition.close(); self.composition.close()
        with self.assertRaises(RepairExecutionError):
            builder.ready(project_digest='a' * 64, recipe_id='build', validation_plan_digest=contracts.digest(self.plan))
        with self.assertRaises(RepairExecutionError): self.build()

    def test_failed_probe_cannot_leave_an_executable_builder(self):
        ProbeVMDouble.network_denied = False
        with self.assertRaises(RepairExecutionError): self.build()
        report = self.composition.status()['builds'][0]
        self.assertEqual(report['status'], 'blocked-unqualified')
        self.assertFalse(report['qualified'])

    def test_mismatched_recipe_environment_and_plan_are_rejected_before_vm_dispatch(self):
        for changed in ({'recipeId': 'not-in-bundle'}, {'environmentDigest': 'f' * 64},
                        {'validationPlanId': 'unregistered'}, {'projectDigest': 'c' * 64}):
            with self.subTest(changed=changed), mock.patch('reproof.execution.qualification.NativeVM',
                    side_effect=AssertionError('invalid configuration dispatched')):
                with self.assertRaises(RepairExecutionError): self.build(route={**self.route, **changed})
        self.assertEqual(self.composition.status()['builds'], [])

    def test_imported_qualification_flags_are_not_a_composition_authority(self):
        from reproof.repair_composition import ProtectedRepairComposition
        with self.assertRaises(RepairExecutionError): ProtectedRepairComposition(authority={'qualified': True})
        with self.assertRaises(RepairExecutionError): self.composition.register('profile', {'verified': True})

    def test_close_cancels_and_waits_for_native_qualification_cleanup(self):
        self._close_during_qualification(ignore_cancel=False)

    def test_close_retains_unfinished_qualification_for_a_bounded_retry(self):
        self._close_during_qualification(ignore_cancel=True)

    def _close_during_qualification(self, *, ignore_cancel):
        from reproof.execution.native import NativeError
        entered = threading.Event(); release = threading.Event(); stopped = threading.Event()
        class WaitingVMDouble(ProbeVMDouble):
            def __init__(self, *args, **kwargs):
                self.cancel = kwargs['cancel']
                super().__init__(*args, **kwargs)
            def wait_ready(self):
                entered.set()
                while not release.wait(.01):
                    if not ignore_cancel and self.cancel.is_set(): break
                raise NativeError('controlled qualification cancellation')
            def stop(self, **kwargs):
                result = super().stop(**kwargs); stopped.set(); return result
        outcomes = []
        def qualify():
            try: outcomes.append(self.build())
            except Exception as error: outcomes.append(error)
        with mock.patch('reproof.execution.qualification.NativeVM', WaitingVMDouble):
            worker = threading.Thread(target=qualify); worker.start()
            try:
                self.assertTrue(entered.wait(2))
                if ignore_cancel:
                    with self.assertRaises(RepairExecutionError): self.composition.close(timeout_seconds=.05)
                    self.assertTrue(worker.is_alive())
                    self.assertEqual(self.composition.status()['qualificationsRunning'], 1)
                else:
                    self.composition.close(timeout_seconds=2)
                    self.assertTrue(stopped.is_set())
                    self.assertEqual(self.composition.status()['qualificationsRunning'], 0)
            finally:
                release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1); self.assertIsInstance(outcomes[0], RepairExecutionError)
        self.composition.close(timeout_seconds=1)
        self.assertTrue(self.composition.status()['closed'])
        self.assertFalse(self.composition.status()['builds'][0]['qualified'])


class IssueRepairCompositionTests(unittest.TestCase):
    def setUp(self):
        from reproof.repair_composition import ProtectedRepairComposition
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)
        store = AccessStore(self.env.root / 'access'); self.addCleanup(store.close)
        store.bootstrap_administrator('admin'); store.register_project('admin', self.env.project)
        store.assign_device('admin', 'device', project_id=self.env.project['id'])
        self.access = AccessController(store); self.access.bind_project(self.env.registration)
        patch = self.env.root / 'proposal.json'; patch.write_text(json.dumps({'edits': [EDIT]}))
        self.document = configuration(self.env)
        self.document['repairStorageBytes'] = 128 * 1024 * 1024
        self.document['projects'][0]['repair'] = {
            'sourceRoot': str(self.env.root / 'source'), 'sourcePaths': [p for p, _ in self.env.source_blobs.entries],
            'protectedPaths': ['tests/CheckoutTests.swift'], 'originalArtifactRoot': str(self.env.root / 'original-build'),
            'originalArtifactPaths': ['original.bin'], 'artifactIdentity': 'file-sha256',
            'buildRecipeId': 'build_app', 'agent': {'kind': 'local-patch', 'patchFile': str(patch)},
            'protectedProfileId': 'owned-execution'}
        path = self.env.root / 'runtime.json'; path.write_text(json.dumps(self.document))
        self.loaded = issue_configuration.load_issue_configuration(path)
        self.bundle = issue_configuration.compose_issue_workflow(self.env.lab, self.access, self.loaded,
            root=self.env.root / 'composed', defer_repairs=True)
        self.addCleanup(self.bundle.close)
        self.synthetic = SyntheticRepairExecution(self.env); self.addCleanup(self.synthetic.close)
        self.composition = ProtectedRepairComposition(authority=self.synthetic.authority)
        self.addCleanup(self.composition.close)

    def executor(self, *, same_runner=True):
        from reproof.repair_mobile import ProtectedMobileSupervisor
        from reproof.repair_verification import ProtectedRepairExecutor
        previous = self.synthetic.mobile()
        mobile = ProtectedMobileSupervisor(authority=previous.authority, qualification=previous.qualification,
            route=previous.route, validation_plan=previous.validation_plan, signer=previous.signer,
            validators=previous.validators, adapter=previous.adapter,
            runner=self.bundle.workflow.runtimes['checkout'].service.runner if same_runner else previous.runner,
            store=previous.store)
        return ProtectedRepairExecutor(self.synthetic.builder, self.synthetic.signer, mobile)

    def attach(self, document=None):
        return issue_configuration.compose_issue_repairs(self.bundle, document or self.loaded,
            protected_repairs=self.composition)

    def test_deferred_service_attaches_the_exact_local_executor_without_source_reads(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        with mock.patch('reproof.project_repair.RepairSource.freeze', side_effect=AssertionError('premature read')):
            self.attach()
        runtime = self.bundle.workflow.repairs.runtimes['checkout']
        self.assertIs(runtime.executor, executor)
        self.assertTrue(runtime.availability()['verificationAvailable'])
        self.bundle.close()
        with self.assertRaises(RepairExecutionError): executor.ready(project_digest=self.env.registration.project_digest,
            build_recipe_id='build_app', validation_recipe_ids=('regression_ui',))

    def test_selected_profile_must_exist_locally_and_cannot_use_another_authority(self):
        from reproof.repair_composition import ProtectedRepairComposition
        foreign = ProtectedRepairComposition(authority=QualificationAuthority()); self.addCleanup(foreign.close)
        with self.assertRaises(RepairExecutionError): foreign.register('owned-execution', self.executor())
        with self.assertRaises(contracts.ContractError): self.attach()
        self.assertIsNone(self.bundle.workflow.repairs)

    def test_other_runner_project_device_app_and_artifact_identity_cannot_be_attached(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        runtime = self.bundle.workflow.runtimes['checkout']
        source = self.env.source
        bad_runtime = copy.copy(runtime)
        object.__setattr__(bad_runtime, 'service', self.env.service)
        with self.assertRaises(RepairExecutionError):
            self.composition.executor_for('owned-execution', bad_runtime, source, build_recipe_id='build_app')
        for field, replacement in (('artifact_identity', 'tree-sha256'), ('project_digest', 'f' * 64)):
            changed = copy.copy(source); setattr(changed, field, replacement)
            with self.subTest(field=field), self.assertRaises(RepairExecutionError):
                self.composition.executor_for('owned-execution', runtime, changed, build_recipe_id='build_app')
        with mock.patch.dict(self.env.lab.devices, {'device': {**self.env.device, 'platform': 'android'}}):
            with self.assertRaises(RepairExecutionError):
                self.composition.executor_for('owned-execution', runtime, source, build_recipe_id='build_app')
        with self.assertRaises(RepairExecutionError):
            self.composition.executor_for('owned-execution', runtime, source, build_recipe_id='unregistered')

    def test_runtime_policy_drift_and_mutated_executor_fail_before_job_registration(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        executor.builder.application_id = 'other_app'
        with self.assertRaises(contracts.ContractError): self.attach()
        self.assertIsNone(self.bundle.workflow.repairs)

    def test_duplicate_registration_and_second_service_owner_are_rejected(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        with self.assertRaises(RepairExecutionError): self.composition.register('second-profile', executor)
        self.attach()
        with self.assertRaises(contracts.ContractError): self.attach()
        with self.assertRaises(RepairExecutionError): self.composition.claim(object())

    def test_executor_retrieval_requires_the_exact_claimed_service_runtime(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        runtime = self.bundle.workflow.runtimes['checkout']
        with self.assertRaises(RepairExecutionError):
            self.composition.executor_for('owned-execution', runtime, self.env.source, build_recipe_id='build_app')
        self.composition.claim(self.bundle)
        self.assertIs(self.composition.executor_for('owned-execution', runtime, self.env.source,
            build_recipe_id='build_app', owner=self.bundle), executor)
        for selected, owner in ((runtime, object()), (copy.copy(runtime), self.bundle)):
            with self.assertRaises(RepairExecutionError):
                self.composition.executor_for('owned-execution', selected, self.env.source,
                    build_recipe_id='build_app', owner=owner)

    def test_same_platform_wrong_installed_identity_fails_before_repair_jobs(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        original = copy.deepcopy(self.env.device['capabilities']['applicationIdentity'])
        try:
            self.env.device['capabilities']['applicationIdentity']['bundle'] = 'com.other.application'
            with self.assertRaises(contracts.ContractError): self.attach()
            self.assertIsNone(self.bundle.workflow.repairs)
        finally:
            self.env.device['capabilities']['applicationIdentity'] = original

    def test_unassigned_device_is_rejected_by_live_service_administration(self):
        executor = self.executor(); self.composition.register('owned-execution', executor)
        empty = AccessStore(self.env.root / 'unassigned-access'); self.addCleanup(empty.close)
        empty.bootstrap_administrator('admin'); empty.register_project('admin', self.env.project)
        unassigned = AccessController(empty); unassigned.bind_project(self.env.registration)
        self.bundle.workflow.access = unassigned
        with self.assertRaises(contracts.ContractError): self.attach()
        self.assertIsNone(self.bundle.workflow.repairs)


if __name__ == '__main__': unittest.main()
