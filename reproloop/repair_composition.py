"""Process-owned protected repair assembly and exact issue-runtime registration.

Only trusted local Python composition may register executors. A public runtime
file selects a profile ID; it cannot construct a callback, load a module, import
qualification, or supply signing material. The same authority that measures a
VM remains alive with its build/sign/mobile chain until the service closes.
"""
from __future__ import annotations

import copy
from pathlib import Path
import threading
import time

from . import contracts
from .execution.artifacts import ArtifactError, ArtifactValidationAuthority
from .execution.backend import ExecutionDenied, QualificationAuthority
from .execution.journal import RunStore
from .execution.protocol import validate_execution_route, validate_external_validation_plan
from .execution.qualification import qualify_backend, qualify_host_build
from .execution.resources import GuestBundle, HostBuildBundle, ResourceError
from .execution.runtime import HostBuildBackend, MacOSVirtualizationBackend
from .execution.wire import MAX_TRANSFER_BYTES
from .repair_execution import ProtectedBuildSupervisor, RepairExecutionError, _require
from .repair_verification import ProtectedRepairExecutor


def _device_binding(device):
    _require(type(device) is dict and type(device.get('capabilities')) is dict, 'repair_configuration')
    capabilities = device['capabilities']
    return contracts.digest({'id': device.get('id'), 'kind': device.get('kind'),
        'platform': device.get('platform'), 'identity': capabilities.get('applicationIdentity'),
        'profileDigest': capabilities.get('applicationProfileDigest'),
        'nativeAuthority': device.get('_authority'), 'remoteAuthority': device.get('_remoteAuthority', False)})


class ProtectedRepairComposition:
    """Own live capabilities; retain only inert summaries for status reporting.

    ``qualify_build`` runs the fixed native probes, without accepting a saved
    qualification record. ``register`` accepts an already assembled local
    executor, including an independently qualified mobile adapter. The issue
    service must cancel and join its jobs before closing this owner. Closing
    revokes capabilities; it never reports native termination or frees an
    uncertain operation's durable reservation.
    """
    def __init__(self, *, authority=None):
        _require(authority is None or type(authority) is QualificationAuthority, 'repair_configuration')
        self.authority = authority if authority is not None else QualificationAuthority()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._cleanup_lock = threading.Lock()
        self._cancellation = threading.Event()
        self._closed = False
        self._owner = None
        self._registrations = {}
        self._profile_ids = set()
        self._backend_scopes = set()
        self._building = set()
        self._build_reports = []
        self._android_signing = []
        self._ios_signing = []
        self._android_mobile = []
        self._ios_mobile = []
        self._validation_secrets = []

    def qualify_build(self, *, bundle, store, route, validation_plan, artifact_authority,
                      application_id, artifact_identity='file-sha256', ttl_ms=3600000):
        """Verify immutable inputs and measure build qualification in this process.

        A ``GuestBundle`` requires sealed-VM qualification; a ``HostBuildBundle``
        is the operator's explicit non-isolated choice and only measures the
        pinned host toolchain, process termination and cleanup.
        """
        _require(type(bundle) in (GuestBundle, HostBuildBundle) and type(store) is RunStore
            and type(artifact_authority) is ArtifactValidationAuthority, 'repair_configuration')
        try:
            host = type(bundle) is HostBuildBundle
            execution_class = 'host-build' if host else 'build-guest'
            checked_route = validate_execution_route(route)
            checked_plan = validate_external_validation_plan(validation_plan)
            contracts.validate_id(application_id)
            recipe = bundle.recipe(checked_route['recipeId'])
            _require(checked_route['executionClass'] == execution_class
                and bundle.metadata['environment']['executionClass'] == execution_class
                and checked_route['environmentDigest'] == bundle.environment_digest == store.environment_digest
                and checked_route['projectDigest'] == checked_plan['projectDigest']
                and checked_route['validationPlanId'] == checked_plan['id']
                and recipe['executionClass'] == execution_class
                and recipe['artifactPolicyId'] == checked_route['artifactPolicyId']
                and recipe['cleanupPolicyId'] == checked_route['cleanupPolicyId']
                and artifact_identity in {'file-sha256', 'tree-sha256'}, 'repair_configuration')
            scope = (checked_route['backendId'], execution_class, bundle.environment_digest)
            with self._lock:
                _require(not self._closed and self._owner is None and scope not in self._building,
                         'repair_configuration')
                trusted_plan = self.authority.register_validation_plan(checked_plan)
                trusted_route = self.authority.register_execution_route(checked_route)
                self._building.add(scope)
                self._backend_scopes.add(scope)
            try:
                measure = qualify_host_build if host else qualify_backend
                outcome = measure(checked_route['backendId'], self.authority, bundle, store,
                    ttl_ms=ttl_ms, cancellation=self._cancellation)
                with self._lock:
                    self._build_reports.append(copy.deepcopy(outcome.report))
                    if self._closed:
                        self.authority.revoke_backend(*scope)
                    _require(not self._closed and outcome.qualification is not None, 'build_unqualified')
                    backend = (HostBuildBackend if host else MacOSVirtualizationBackend)(
                        checked_route['backendId'], self.authority, bundle, store)
                    return ProtectedBuildSupervisor(backend, qualification=outcome.qualification,
                        route=trusted_route, validation_plan=trusted_plan, artifact_authority=artifact_authority,
                        application_id=application_id, artifact_identity=artifact_identity)
            finally:
                with self._lock:
                    self._building.discard(scope)
                    self._changed.notify_all()
        except (contracts.ContractError, ExecutionDenied, ResourceError, OSError):
            raise RepairExecutionError('repair_configuration') from None

    def adopt_android_signing(self, signer, inspector):
        """Own one fixed Android signer/inspector pair and its explicit material.

        Adoption grants no build, signature or mobile qualification. The caller
        must finish local assembly before transferring this owner to a service.
        """
        from .repair_android_signing import AndroidApkInspector, AndroidApkSigner
        _require(type(signer) is AndroidApkSigner and type(inspector) is AndroidApkInspector
            and signer.identity == inspector.identity and signer.tools == inspector.tools
            and signer.policy_digest == inspector.policy_digest
            and signer.max_apk_bytes == inspector.max_apk_bytes, 'repair_configuration')
        with self._lock:
            _require(not self._closed and self._owner is None and len(self._android_signing) < 128
                and all(signer is not pair[0] and inspector is not pair[1]
                        for pair in self._android_signing), 'repair_configuration')
            self._android_signing.append((signer, inspector, signer.resolver))

    def adopt_android_mobile(self, adapter):
        """Own the fixed Android adapter through cancellation and restoration."""
        from .repair_android import AndroidTrustedMobileAdapter
        _require(type(adapter) is AndroidTrustedMobileAdapter, 'repair_configuration')
        adapter.config.validate()
        with self._lock:
            _require(not self._closed and self._owner is None
                     and len(self._android_mobile) < 128
                     and all(item is not adapter for item in self._android_mobile),
                     'repair_configuration')
            self._android_mobile.append(adapter)

    def adopt_ios_mobile(self, adapter):
        """Own the fixed iOS adapter through cancellation and restoration."""
        from .repair_ios import IOSTrustedMobileAdapter
        _require(type(adapter) is IOSTrustedMobileAdapter, 'repair_configuration')
        adapter.config.validate()
        with self._lock:
            _require(not self._closed and self._owner is None
                     and len(self._ios_mobile) < 128
                     and all(item is not adapter for item in self._ios_mobile),
                     'repair_configuration')
            self._ios_mobile.append(adapter)

    def configure_android_signing(self, builder, *, tools, material_resolver, identity,
                                  policy_document, store, work_root, artifact_policy_id='signed-apk',
                                  max_apk_bytes=MAX_TRANSFER_BYTES, timeout_seconds=120):
        """Assemble the fixed signer and independent inspector for a live builder.

        The certificate defines the canonical signing scope. Changing a policy
        ID, material reference or application cannot escape an uncertain key's
        existing journal. Secret material stays in the supplied local resolver.
        """
        from .repair_android_signing import (
            AndroidApkInspector, AndroidApkSigner, AndroidSigningError,
            AndroidSigningIdentity, AndroidSigningMaterialResolver, AndroidSigningTools,
        )
        from .repair_signing import TrustedSigningSupervisor
        _require(type(builder) is ProtectedBuildSupervisor and builder.backend.authority is self.authority
            and type(tools) is AndroidSigningTools and type(identity) is AndroidSigningIdentity
            and type(material_resolver) is AndroidSigningMaterialResolver and type(store) is RunStore
            and builder.application_id == identity.application_id, 'repair_configuration')
        signer = inspector = None
        with self._lock:
            _require(not self._closed and self._owner is None, 'repair_configuration')
            try:
                builder.ready(project_digest=builder.route.project_digest, recipe_id=builder.route.recipe_id,
                    validation_plan_digest=builder.validation_plan.definition_digest)
                policy = self.authority.register_signing_policy(policy_document)
                root = Path(work_root)
                signer = AndroidApkSigner(tools, material_resolver, identity, policy_document,
                                          root / 'sign', max_apk_bytes=max_apk_bytes)
                inspector = AndroidApkInspector(tools, identity, policy_document,
                    root / 'inspect', max_apk_bytes=max_apk_bytes)
                artifacts = ArtifactValidationAuthority()
                artifacts.register(artifact_policy_id, paths=('candidate.apk',), max_bytes=max_apk_bytes,
                                   checker=inspector.accepts)
                scope = contracts.digest({'kind': 'android-apk-signing',
                                           'certificateSha256': identity.certificate_sha256})
                supervisor = TrustedSigningSupervisor(builder, authority=self.authority, policy=policy,
                    policy_document=policy_document, signer=signer, inspector=inspector,
                    artifact_authority=artifacts, artifact_policy_id=artifact_policy_id,
                    store=store, scope_digest=scope, timeout_seconds=timeout_seconds)
                supervisor.ready()
                self.adopt_android_signing(signer, inspector)
                return supervisor
            except (contracts.ContractError, ArtifactError, AndroidSigningError, ExecutionDenied, RepairExecutionError,
                    OSError, TypeError, ValueError):
                if signer is not None: signer.close()
                if inspector is not None: inspector.close()
                raise RepairExecutionError('repair_configuration') from None

    def configure_android_signing_owner(self, builder, *, tools, material_resolver, identity,
                                        policy_document, store, work_root, artifact_policy_id='signed-apk',
                                        max_apk_bytes=MAX_TRANSFER_BYTES, timeout_seconds=120):
        """Assemble signing with durable intent and the fixed in-process JVM owner."""
        from .repair_android_signing import AndroidSigningIdentity, AndroidSigningMaterialResolver
        from .repair_android_signing_owner import AndroidSigningOwnerSigner, AndroidSigningOwnerInspector
        from .repair_signing_recovery import MIN_OPERATION_BYTES, SigningOperationStore, SigningOwnerTools
        from .repair_signing import TrustedSigningSupervisor
        _require(type(builder) is ProtectedBuildSupervisor and builder.backend.authority is self.authority
            and type(tools) is SigningOwnerTools and type(identity) is AndroidSigningIdentity
            and type(material_resolver) is AndroidSigningMaterialResolver and type(store) is RunStore
            and store.disk_limit >= MIN_OPERATION_BYTES
            and builder.application_id == identity.application_id, 'repair_configuration')
        signer = inspector = operations = None
        with self._lock:
            _require(not self._closed and self._owner is None, 'repair_configuration')
            try:
                builder.ready(project_digest=builder.route.project_digest, recipe_id=builder.route.recipe_id,
                              validation_plan_digest=builder.validation_plan.definition_digest)
                policy = self.authority.register_signing_policy(policy_document)
                scope = contracts.digest({'kind': 'android-apk-signing',
                                           'certificateSha256': identity.certificate_sha256})
                operations = SigningOperationStore(store, scope, tools, identity, Path(work_root))
                signer = AndroidSigningOwnerSigner(operations, material_resolver, policy_document,
                                                   max_apk_bytes=max_apk_bytes)
                inspector = AndroidSigningOwnerInspector(operations, policy_document,
                                                         max_apk_bytes=max_apk_bytes)
                artifacts = ArtifactValidationAuthority()
                artifacts.register(artifact_policy_id, paths=('candidate.apk',), max_bytes=max_apk_bytes,
                                   checker=inspector.accepts)
                supervisor = TrustedSigningSupervisor(builder, authority=self.authority, policy=policy,
                    policy_document=policy_document, signer=signer, inspector=inspector,
                    artifact_authority=artifacts, artifact_policy_id=artifact_policy_id,
                    store=store, scope_digest=scope, timeout_seconds=timeout_seconds, operations=operations)
                supervisor.ready()
                _require(len(self._android_signing) < 128, 'repair_configuration')
                self._android_signing.append((signer, inspector, material_resolver))
                return supervisor
            except BaseException as error:
                if signer is not None: signer.close()
                if inspector is not None: inspector.close()
                if operations is not None: operations.close()
                if not isinstance(error, Exception):
                    raise
                raise RepairExecutionError('repair_configuration') from None

    def configure_ios_signing_owner(self, builder, *, tools, material_resolver, definition,
                                    provisioning, policy_document, store, work_root,
                                    artifact_policy_id='signed-ios-ipa', timeout_seconds=120):
        from .execution.artifacts import ArtifactValidationAuthority
        from .execution.journal import RunStore
        from .execution.wire import MAX_TRANSFER_BYTES
        from .ios_signing_inputs import (IOSSigningOwnerTools, IOSSigningMaterialResolver,
            IOSSigningDefinition, IOSSigningProvisioning)
        from .ios_signing_operation import IOSSigningOperationStore
        from .repair_ios_signing_owner import IOSSigningOwnerSigner, IOSSigningOwnerInspector
        from .repair_signing import TrustedSigningSupervisor
        _require(type(builder) is ProtectedBuildSupervisor and builder.backend.authority is self.authority
            and type(tools) is IOSSigningOwnerTools and tools.guardian is not None
            and type(material_resolver) is IOSSigningMaterialResolver and type(definition) is IOSSigningDefinition
            and type(provisioning) is IOSSigningProvisioning and type(store) is RunStore
            and builder.application_id == definition.identity.application_id, 'repair_configuration')
        signer = inspector = operations = None
        with self._lock:
            _require(not self._closed and self._owner is None, 'repair_configuration')
            try:
                builder.ready(project_digest=builder.route.project_digest, recipe_id=builder.route.recipe_id,
                              validation_plan_digest=builder.validation_plan.definition_digest)
                checked = definition.validate_policy(policy_document)
                policy = self.authority.register_signing_policy(checked)
                operations = IOSSigningOperationStore(store, tools, definition, Path(work_root))
                signer = IOSSigningOwnerSigner(operations, material_resolver, provisioning, checked)
                inspector = IOSSigningOwnerInspector(operations, provisioning, checked)
                artifacts = ArtifactValidationAuthority()
                artifacts.register(artifact_policy_id, paths=('candidate.ipa',), max_bytes=MAX_TRANSFER_BYTES,
                                   checker=inspector.accepts)
                supervisor = TrustedSigningSupervisor(builder, authority=self.authority, policy=policy,
                    policy_document=checked, signer=signer, inspector=inspector, artifact_authority=artifacts,
                    artifact_policy_id=artifact_policy_id, store=store, scope_digest=operations.scope_digest,
                    timeout_seconds=timeout_seconds, operations=operations)
                supervisor.ready()
                _require(len(self._ios_signing) < 128, 'repair_configuration')
                self._ios_signing.append((signer, inspector, material_resolver))
                return supervisor
            except BaseException as error:
                if signer is not None: signer.close()
                if inspector is not None: inspector.close()
                if operations is not None: operations.close()
                if not isinstance(error, Exception):
                    raise
                raise RepairExecutionError('repair_configuration') from None

    def configure_android_mobile(self, *, config, signer, validators=None, qualification,
                                 route_document, store, work_root, adapter_id,
        timeout_seconds=600, cleanup_timeout_seconds=30, validation_inputs=None, validation_secrets=None):
        """Bind a fixed Android adapter and its durable journal to this authority.

        Qualification must already be a live capability measured by this
        authority. This assembly does not import probe results or qualify a
        general Android device merely because its profile is valid.
        """
        from .repair_android import AndroidMobileAdapterConfig, AndroidTrustedMobileAdapter
        from .repair_android_operation import AndroidOperationStore
        from .repair_mobile import ProtectedMobileSupervisor
        from .repair_signing import TrustedSigningSupervisor
        from .validation import TrustedValidationAuthority
        from .protected_validation import ValidationSecretRegistry
        from .protected_validation_inputs import AndroidValidationInputs
        _require(type(config) is AndroidMobileAdapterConfig
            and type(signer) is TrustedSigningSupervisor and signer.authority is self.authority
            and type(store) is RunStore,
            'repair_configuration')
        if validation_inputs is None:
            _require(type(validators) is TrustedValidationAuthority and validation_secrets is None,'repair_configuration')
        else:
            _require(validators is None and type(validation_inputs) is AndroidValidationInputs
                and type(validation_secrets) is ValidationSecretRegistry,'repair_configuration')
        operations = adapter = None
        with self._lock:
            _require(not self._closed and self._owner is None and len(self._android_mobile) < 128,
                     'repair_configuration')
            try:
                if validation_inputs is None:
                    config.validate()
                    validation_plan = validators.plan
                else:
                    validation_inputs.validate_binding(config)
                    validation_secrets.require_owner(self)
                    validation_plan = validation_inputs.plan
                route = self.authority.register_execution_route(route_document)
                plan = self.authority.register_validation_plan(validation_plan)
                _require(route.execution_class == 'mobile-device' and route.platform == 'android'
                    and signer.policy.platform == 'android' and signer.builder.application_id == config.application_id
                    and route.application_id == config.application_id
                    and route.project_digest == config.registration.project_digest
                    and route.environment_digest == store.environment_digest, 'repair_configuration')
                operations = AndroidOperationStore(store, config, Path(work_root))
                adapter = AndroidTrustedMobileAdapter(config, operations=operations)
                if validation_inputs is not None:
                    validators = validation_inputs.bind(adapter,validation_secrets)
                supervisor = ProtectedMobileSupervisor(authority=self.authority, qualification=qualification,
                    route=route, validation_plan=plan, signer=signer, validators=validators,
                    adapter=adapter.trusted_adapter(adapter_id=adapter_id), runner=config.service.runner,
                    store=store, operations=operations, timeout_seconds=timeout_seconds,
                    cleanup_timeout_seconds=cleanup_timeout_seconds)
                supervisor.ready(project_digest=config.registration.project_digest,
                    runtime_policy_digest=config.runtime_policy_digest,
                    validation_recipe_ids=[item['recipeId'] for item in validators.plan['checks']])
                self.adopt_android_mobile(adapter)
                if validation_secrets is not None:
                    validation_secrets.claim(self)
                    if all(item is not validation_secrets for item in self._validation_secrets):
                        self._validation_secrets.append(validation_secrets)
                self._backend_scopes.add((route.backend_id, route.execution_class, route.environment_digest))
                return supervisor
            except (contracts.ContractError, OSError, TypeError, ValueError, RuntimeError):
                deadline = time.monotonic() + 5
                if adapter is not None:
                    adapter.close(deadline_monotonic=deadline)
                elif operations is not None:
                    operations.close(deadline_monotonic=deadline)
                raise RepairExecutionError('repair_configuration') from None

    def configure_ios_mobile(self, *, config, signer, validators=None, qualification,
                             route_document, store, work_root, adapter_id,
                             timeout_seconds=600, cleanup_timeout_seconds=30,
                             validation_inputs=None, validation_secrets=None):
        """Bind one exact iOS adapter and its durable preparation journal.

        As with Android, qualification and validation secrets are live
        capabilities supplied by the caller; this method never loads either
        from serialized issue input.
        """
        from .ios_mobile_inputs import IOSMobileInputsConfig
        from .ios_mobile_operation import IOSMobileOperationStore
        from .repair_ios import IOSTrustedMobileAdapter
        from .repair_mobile import ProtectedMobileSupervisor
        from .repair_signing import TrustedSigningSupervisor
        from .validation import TrustedValidationAuthority
        from .protected_validation import ValidationSecretRegistry
        from .protected_validation_inputs import IOSValidationInputs
        _require(type(config) is IOSMobileInputsConfig
            and type(signer) is TrustedSigningSupervisor and signer.authority is self.authority
            and type(store) is RunStore
            and config.xctest is not None and config.sanitation is not None,
            'repair_configuration')
        if validation_inputs is None:
            _require(type(validators) is TrustedValidationAuthority and validation_secrets is None,
                     'repair_configuration')
        else:
            _require(validators is None and type(validation_inputs) is IOSValidationInputs
                and type(validation_secrets) is ValidationSecretRegistry,
                'repair_configuration')
        operations = adapter = None
        with self._lock:
            _require(not self._closed and self._owner is None and len(self._ios_mobile) < 128,
                     'repair_configuration')
            try:
                if validation_inputs is None:
                    config.validate()
                    validation_plan = validators.plan
                else:
                    validation_inputs.validate_binding(config)
                    validation_secrets.require_owner(self)
                    validation_plan = validation_inputs.plan
                route = self.authority.register_execution_route(route_document)
                plan = self.authority.register_validation_plan(validation_plan)
                _require(route.execution_class == 'mobile-device' and route.platform == 'ios'
                    and signer.policy.platform == 'ios'
                    and signer.builder.application_id == config.application_id
                    and route.application_id == config.application_id
                    and route.project_digest == config.registration.project_digest
                    and route.environment_digest == store.environment_digest,
                    'repair_configuration')
                operations = IOSMobileOperationStore(store, config.definition, Path(work_root))
                adapter = IOSTrustedMobileAdapter(config, operations=operations)
                if validation_inputs is not None:
                    validators = validation_inputs.bind(adapter, validation_secrets)
                supervisor = ProtectedMobileSupervisor(authority=self.authority, qualification=qualification,
                    route=route, validation_plan=plan, signer=signer, validators=validators,
                    adapter=adapter.trusted_adapter(adapter_id=adapter_id), runner=config.service.runner,
                    store=store, operations=operations, timeout_seconds=timeout_seconds,
                    cleanup_timeout_seconds=cleanup_timeout_seconds)
                supervisor.ready(project_digest=config.registration.project_digest,
                    runtime_policy_digest=config.runtime_policy_digest,
                    validation_recipe_ids=[item['recipeId'] for item in validators.plan['checks']])
                self.adopt_ios_mobile(adapter)
                if validation_secrets is not None:
                    validation_secrets.claim(self)
                    if all(item is not validation_secrets for item in self._validation_secrets):
                        self._validation_secrets.append(validation_secrets)
                self._backend_scopes.add((route.backend_id, route.execution_class, route.environment_digest))
                return supervisor
            except (contracts.ContractError, OSError, TypeError, ValueError, RuntimeError):
                deadline = time.monotonic() + 5
                if adapter is not None:
                    adapter.close(deadline_monotonic=deadline)
                elif operations is not None:
                    operations.close(deadline_monotonic=deadline)
                raise RepairExecutionError('repair_configuration') from None

    def register(self, profile_id, executor):
        """Register one exact local chain per project revision, before binding."""
        try:
            contracts.validate_id(profile_id)
            _require(type(executor) is ProtectedRepairExecutor
                and executor.builder.backend.authority is self.authority
                and executor.signer.authority is self.authority
                and executor.mobile.authority is self.authority, 'repair_configuration')
            project_digest = executor.builder.route.project_digest
            _require(project_digest == executor.mobile.route.project_digest
                == executor.mobile.adapter.project_digest, 'repair_configuration')
            device = executor.mobile.runner.lab.devices.get(executor.device_id)
            device_binding = _device_binding(device)
            with self._lock:
                _require(not self._closed and self._owner is None and not self._building
                    and len(self._registrations) < 128 and project_digest not in self._registrations
                    and profile_id not in self._profile_ids, 'repair_configuration')
                self._registrations[project_digest] = (profile_id, executor, executor.definition_digest,
                    device, device_binding, device.get('factory'))
                self._profile_ids.add(profile_id)
                for route in (executor.builder.route, executor.mobile.route):
                    self._backend_scopes.add((route.backend_id, route.execution_class, route.environment_digest))
        except contracts.ContractError:
            raise RepairExecutionError('repair_configuration') from None

    def claim(self, owner):
        """Transfer this composition to one service bundle, without authorizing work."""
        from .live.issue_configuration import IssueRuntimeBundle
        with self._lock:
            _require(not self._closed and type(owner) is IssueRuntimeBundle and self._owner is None
                and owner.workflow is not None and not owner.workflow._closed
                and not self._building, 'repair_configuration')
            self._owner = owner

    def executor_for(self, profile_id, runtime, source, *, build_recipe_id, owner=None):
        from .live.issue_workflow import ProjectIssueRuntime
        from .project_repair import RepairSource
        _require(type(runtime) is ProjectIssueRuntime and type(source) is RepairSource, 'repair_configuration')
        with self._lock:
            _require(not self._closed and owner is not None and owner is self._owner
                and owner.workflow is not None and not owner.workflow._closed
                and any(runtime is item for item in owner.workflow.runtimes.values()), 'repair_configuration')
            row = self._registrations.get(source.project_digest)
            _require(row is not None and row[0] == profile_id, 'repair_configuration')
            executor = row[1]
            builder, signer, mobile = executor.builder, executor.signer, executor.mobile
            application = next((item for item in source.project['applications']
                                if item['id'] == builder.application_id), None)
            device = runtime.service.lab.devices.get(executor.device_id)
            _require(executor.definition_digest == row[2]
                and builder.backend.authority is self.authority and signer.authority is self.authority
                and mobile.authority is self.authority
                and source.project_digest == runtime.registration.project_digest
                and builder.route.recipe_id == build_recipe_id
                and builder.artifact_identity == source.artifact_identity
                and mobile.runner is runtime.service.runner
                and mobile.runner.lab is runtime.service.lab
                and mobile.adapter.runtime_policy_digest == contracts.digest(runtime.runtime_policy)
                and set(runtime.validation_recipe_ids) == {item['recipeId'] for item in mobile.validators.plan['checks']}
                and application is not None and device is not None
                and device is row[3] and _device_binding(device) == row[4] and device.get('factory') is row[5]
                and application['platform'] == device['platform'] == signer.policy.platform == mobile.route.platform
                and application['id'] == signer.policy.application_id == mobile.route.application_id,
                'repair_configuration')
            try:
                access = owner.workflow.access
                _require(access.registration(source.project['id']) is runtime.registration
                    and source.project['id'] in access.store.assignment_project_ids(executor.device_id),
                    'repair_configuration')
                identity = device['capabilities'].get('applicationIdentity', {})
                original = next((build for build in source.project['builds']
                    if build['applicationId'] == application['id']
                    and build['artifactDigest'] == identity.get('artifactDigest')), None)
                _require(original is not None, 'repair_configuration')
                runtime.service.lab._validate_release_selection(executor.device_id, runtime.registration,
                    application['id'], original['id'])
            except contracts.ContractError:
                raise RepairExecutionError('repair_configuration') from None
            return executor

    def status(self):
        with self._lock:
            return {'schemaVersion': 1, 'closed': self._closed,
                'builds': copy.deepcopy(self._build_reports),
                'qualificationsRunning': len(self._building),
                'cleanupPending': len(self._android_signing) + len(self._ios_signing) + len(self._android_mobile)
                    + len(self._ios_mobile)
                    + len(self._validation_secrets),
                'profiles': sorted(row[0] for row in self._registrations.values())}

    def revoke(self):
        """Fence new work immediately; live resource owners remain retryable."""
        with self._lock:
            self._closed = True
            self._cancellation.set()
            for adapter in self._android_mobile:
                adapter.revoke()
            for adapter in self._ios_mobile:
                adapter.revoke()
            for scope in self._backend_scopes:
                self.authority.revoke_backend(*scope)
            self._registrations.clear()
            self._profile_ids.clear()

    def close(self, *, timeout_seconds=10):
        _require(type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 30,
                 'repair_configuration')
        self.revoke()
        deadline = time.monotonic() + timeout_seconds
        with self._changed:
            while self._building and time.monotonic() < deadline:
                self._changed.wait(max(0, deadline - time.monotonic()))
            _require(not self._building, 'repair_cleanup_unknown')
        acquired = self._cleanup_lock.acquire(timeout=max(0, deadline - time.monotonic()))
        _require(acquired, 'repair_cleanup_unknown')
        try:
            with self._lock:
                resources = tuple(self._android_signing) + tuple(self._ios_signing)
                mobile_resources = tuple(self._android_mobile) + tuple(self._ios_mobile)
                validation_secrets = tuple(self._validation_secrets)
            clean = True
            for adapter in mobile_resources:
                try:
                    clean = adapter.close(deadline_monotonic=deadline) is True and clean
                except Exception:
                    clean = False
            for signer, inspector, _ in resources:
                for resource in (signer, inspector):
                    try:
                        terminated = resource.close(deadline_monotonic=deadline)
                        clean = terminated is True and resource.active_processes == 0 and clean
                    except Exception:
                        clean = False
            _require(clean, 'repair_cleanup_unknown')
            for registry in validation_secrets:
                try:
                    _require(time.monotonic() < deadline,'repair_cleanup_unknown')
                    registry.close(timeout_seconds=max(.001,deadline-time.monotonic()))
                except Exception:
                    raise RepairExecutionError('repair_cleanup_unknown') from None
            # A shared resolver cannot be erased until all its process owners
            # have stopped. Unknown owners remain available for a later close.
            for _, _, resolver in resources:
                resolver.close()
            with self._lock:
                self._android_signing.clear()
                self._ios_signing.clear()
                self._android_mobile.clear()
                self._ios_mobile.clear()
                self._validation_secrets.clear()
        finally:
            self._cleanup_lock.release()


__all__ = ['ProtectedRepairComposition']
