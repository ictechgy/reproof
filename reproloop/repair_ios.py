"""Fixed iOS protected install, retained replay, and measured app cleanup."""
from contextlib import contextmanager
import copy
from dataclasses import replace
import hashlib
import threading
import time

from . import contracts
from .execution.artifacts import BlobSet
from .ios_mobile_callbacks import IOSNativeCallbackCoordinator
from .ios_mobile_finalization import discard_native_staged
from .ios_mobile_g4 import IOSG4Error, IOSG4Provider
from .ios_mobile_helper import IOSHelperChannel, command_payload
from .ios_mobile_inputs import IOSBaselineReference, IOSMobileInputsConfig
from .ios_mobile_native import prepared_app
from .ios_mobile_operation import IOSMobileOperationStore
from .ios_mobile_xctest import IOSXCTestRunner
from .ios_profile import validate_ios_profile
from .ios_sanitation import IOSSanitationPolicy, policy_from_app
from .qualification import require_candidate_binding
from .repair_callbacks import CallbackCancellation
from .repair_execution import RepairExecutionError
from .repair_mobile import (MobileContext, MobileInstallationObservation, MobileFailureObservation,
    MobileCleanupObservation, TrustedMobileAdapter)


def _require(value, code='mobile_unqualified'):
    if not value:raise RepairExecutionError(code)


def _active(cancellation, deadline):
    _require(not cancellation.is_set(),'cancelled')
    _require(time.monotonic()<deadline,'mobile_timeout')


class _OwnerCancellation(CallbackCancellation):
    def __init__(self,parent,stopping):
        super().__init__(parent);self._owner_stopping=stopping

    def is_set(self):return self._owner_stopping.is_set() or super().is_set()


@contextmanager
def _segment(sink, name):
    """cleanup 기기 dispatch 구간의 경과 시간을 기록한다.

    실패해도 그때까지의 경과는 남는다 — 계측이 cleanup의 안전 결정을 바꾸지 않는다.
    """
    entry={'step':name}
    start=time.monotonic()
    try:yield entry
    finally:
        entry['elapsedMs']=round((time.monotonic()-start)*1000,1)
        sink.append(entry)


class IOSTrustedMobileAdapter:
    def __init__(self, config, *, operations):
        _require(type(config) is IOSMobileInputsConfig and type(operations) is IOSMobileOperationStore)
        _require(config.xctest is not None and type(config.sanitation) is IOSSanitationPolicy
            and operations.definition == config.definition)
        config.validate()
        self.config,self.operations=config,operations
        self._lock=threading.RLock();self._changed=threading.Condition(self._lock)
        self._stopping=threading.Event();self._phase=None
        self._context=None;self._scope=None;self._native=None;self._native_manager=None;self._coordinator=None
        self._candidate_profile=None;self._candidate_identity=None;self._installed=False
        self._issues={};self._completed=set();self._service_unknown=False
        self._cleanup_timing=None

    @property
    def cleanup_timing(self):
        """가장 최근 cleanup의 구간별 경과 기록. 아직 cleanup이 없으면 None."""
        return None if self._cleanup_timing is None else copy.deepcopy(self._cleanup_timing)

    def trusted_adapter(self, *, adapter_id):
        return TrustedMobileAdapter(adapter_id,self.config.scope_digest,self.config.device_id,
            self.config.registration.project_digest,self.config.runtime_policy_digest,
            self.install,self.replay,self.cleanup)

    def _check_context(self, context):
        _require(type(context) is MobileContext,'mobile_policy_mismatch')
        _require(context.project_digest==self.config.registration.project_digest
            and context.application_id==self.config.application_id
            and context.scope_digest==self.config.scope_digest
            and context.runtime_policy_digest==self.config.runtime_policy_digest,'mobile_policy_mismatch')
        return self.operations.require_operation(context._operation_binding,context)

    @contextmanager
    def _callback(self, context, phase):
        self._check_context(context)
        with self._changed:
            _require(self._phase is None,'mobile_quarantined')
            _require(phase=='cleanup' or not self._stopping.is_set(),'mobile_unavailable')
            if phase=='install':
                _require(self._context is None and context.digest not in self._completed
                    and len(self._completed)<65536,'mobile_unavailable')
                self._context=context
            else:
                _require(self._context is not None and self._context.digest==context.digest,'mobile_quarantined')
            self._phase=phase
        try:yield
        finally:
            with self._changed:self._phase=None;self._changed.notify_all()

    def _candidate_build_id(self, context):
        return 'candidate_'+contracts.digest({'operation':context.operation_id,'request':context.request_digest})[:32]

    def _derive_candidate_profile(self, context, operation):
        app=prepared_app(self._native,'candidate')
        policy=policy_from_app(app._source)
        _require(policy is not None and policy.digest==self.config.sanitation.digest,'artifact_invalid')
        intent,_=self.operations._records(context.operation_id,self._native._directory)
        row=intent['roles']['candidate']
        identity=IOSBaselineReference('candidate',self.config.original_profile.bundle,
            operation.archive_path('candidate'),row['sha256'],row['bytes']).read()[1]
        data=self.config.original_profile.data
        data['buildId']=self._candidate_build_id(context)
        data['artifact'].update(kind='ios-ipa',sha256=context.artifact_digest,bytes=row['bytes'],
            bundleVersion=identity['bundleVersion'],bundleBuild=identity['bundleBuild'],
            provenanceDigest=contracts.digest({'kind':'protected-ios-candidate','contextDigest':context.digest}))
        self._candidate_profile=validate_ios_profile(data)

    def _permit(self, kind, payload, provider):
        return self.config.lab.prepare_retained_scope_exact_effect(self._scope,owner=self.config.owner,
            kind=kind,payload_digest=contracts.digest(payload),provider_incarnation=provider)

    def _confirm(self, permit, evidence, *, status='succeeded'):
        self.config.lab.confirm_retained_scope_effect(self._scope,permit,owner=self.config.owner,
            status=status,result_digest=contracts.digest(evidence))

    def _settled(self):
        if self._service_unknown:return False
        if self._native is None:return True
        try:
            with self.operations._changed:
                return all(client.active_processes==0 for client in self.operations._native_clients
                    if client.native_owner is self._native)
        except Exception:return False

    def _failure(self, context, code):
        settled=self._settled()
        return MobileFailureObservation(context.digest,code,contracts.digest({
            'contextDigest':context.digest,'code':code,'effectsSettled':settled}),settled)

    def _install(self, kind, cancellation, deadline):
        installer=self.config.query.open_installer(native_owner=self._native)
        permit=None
        try:
            payload=installer.payload(kind)
            permit=self._permit(kind.replace('-','_'),payload,'provider_ios_scope')
            receipt=installer.run(kind,permit=permit,cancellation=cancellation,deadline_monotonic=deadline)
            observation=installer.observe_installed(receipt,cancellation=cancellation,deadline_monotonic=deadline)
            self._confirm(permit,{'install':receipt.public(),'identity':observation.public()})
            return observation
        except Exception:
            if permit is not None and self._settled():
                try:self._confirm(permit,{'kind':kind,'status':'failed'},status='failed')
                except Exception:self._service_unknown=True
            raise
        finally:_require(installer.close(deadline_monotonic=deadline),'mobile_quarantined')

    def install(self, context, artifacts, *, cancellation, deadline_monotonic):
        with self._callback(context,'install'):
            cancellation=_OwnerCancellation(cancellation,self._stopping)
            try:
                _active(cancellation,deadline_monotonic);self.config.validate()
                _require(type(artifacts) is BlobSet and len(artifacts.entries)==1
                    and artifacts.entries[0][0]=='candidate.ipa'
                    and hashlib.sha256(artifacts.entries[0][1]).hexdigest()==context.artifact_digest,'artifact_invalid')
                operation=self._check_context(context)
                for role in self.operations._roles:
                    self.operations.prepare(operation,role,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
                _active(cancellation,deadline_monotonic)
                self._scope=self.config.lab.begin_retained_device_scope(self.config.device_id,self.config.owner,
                    'reservation_'+contracts.digest(context.operation_id)[:32],self.config.registration,
                    application_id=context.application_id,build_id=self.config.original_build_id)
                self._native_manager=self.operations.native_owner(operation,self._scope._reservation._authority_handle)
                self._native=self._native_manager.__enter__()
                self._coordinator=IOSNativeCallbackCoordinator(self._native)
                with self._coordinator.callback(operation,'install') as token:
                    try:
                        self._derive_candidate_profile(context,operation)
                        self._candidate_identity=self._install('install-candidate',cancellation,deadline_monotonic)
                        _active(cancellation,deadline_monotonic)
                        self._installed=True
                        result=MobileInstallationObservation(context.digest,context.artifact_digest,context.application_id,
                            context.scope_digest,contracts.digest(self._candidate_identity.public()),True)
                        token.complete(result.evidence_digest)
                    except Exception:
                        result=self._failure(context,'mobile_install_failed');token.fail(result.evidence_digest)
                    return result
            except Exception:return self._failure(context,'mobile_install_failed')

    def replay(self, context, execution, number, *, cancellation, deadline_monotonic):
        with self._callback(context,'replay'):
            _require(self._installed and self._coordinator is not None,'mobile_quarantined')
            cancellation=_OwnerCancellation(cancellation,self._stopping)
            with self._coordinator.callback(context._operation_binding,'replay',number) as token:
                runner=None
                try:
                    _active(cancellation,deadline_monotonic)
                    _require(type(number) is int and number==len(self._issues)+1 and number<=3,'mobile_policy_mismatch')
                    execution=self.config.service.registry.require_execution(execution)
                    build=require_candidate_binding(execution.candidate_binding,project_digest=context.project_digest,
                        application_id=context.application_id,build_id=self._candidate_build_id(context))
                    _require(build['artifactDigest']==context.artifact_digest and build['sourceDigest']==context.source_digest
                        and execution.approved.runtime_policy_digest==context.runtime_policy_digest,'mobile_policy_mismatch')
                    profile=self._candidate_profile
                    self.config.lab.validate_retained_device_scope(self._scope,owner=self.config.owner,
                        device_id=self.config.device_id,registration=self.config.registration,
                        application_id=context.application_id,build_id=build['id'],candidate_binding=execution.candidate_binding,
                        _candidate_identity=profile.application_identity,_candidate_profile=profile.data)
                    runner=IOSXCTestRunner(self.config.xctest,self.config.query,self._native)
                    launch=runner.prepare(role='candidate',iteration=number,application_id=context.application_id,
                        profile_digest=profile.digest,actions=tuple(profile.data['capabilities']['actions']),
                        cancellation=cancellation,deadline_monotonic=deadline_monotonic)
                    provider=IOSG4Provider(runner,launch,self._candidate_identity,profile,self._native)
                    binding=self.config.lab.bind_retained_startup(self._scope,owner=self.config.owner,provider=provider,
                        logical_payload={'kind':'ios-physical','applicationIdentity':profile.application_identity},
                        native_payload=launch.payload,provider_incarnation=launch.payload['providerIncarnation'])
                    issue_id='mobile_'+contracts.digest({'context':context.digest,'attempt':number})[:40]
                    self._issues[number]=issue_id
                    result=provider.run_replay(self.config.service,execution,registration=self.config.registration,
                        device_id=self.config.device_id,owner=self.config.owner,controller_id='candidate_'+str(number),
                        preparations=self.config.preparations,issue_id=issue_id,device_scope=self._scope,
                        _candidate_identity=profile.application_identity,_candidate_profile=profile.data,
                        _startup_binding=binding,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
                    _require(self._native._sanitation_results.get((contracts.digest(launch.payload),'cleanup')) is not None,
                        'mobile_quarantined')
                    _require(runner.close(deadline_monotonic=deadline_monotonic),'mobile_quarantined')
                    token.complete(contracts.digest(result.public()))
                    return result
                except Exception as error:
                    if isinstance(error,IOSG4Error) and error.unknown:self._service_unknown=True
                    if runner is not None and not runner.close(deadline_monotonic=deadline_monotonic):
                        self._service_unknown=True
                    result=self._failure(context,'mobile_replay_failed');token.fail(result.evidence_digest)
                    return result

    def _issues_clean(self):
        try:
            return all(record.get('deviceCleanup')=='complete'
                and all(item.get('status')=='complete' for item in record.get('cleanup',[]))
                for record in (self.config.service.get(identifier) for identifier in self._issues.values()))
        except Exception:return False

    def _sanitize_original(self, identity, cancellation, deadline, sink):
        runner=IOSXCTestRunner(self.config.xctest,self.config.query,self._native)
        reader=None
        try:
            profile=self.config.original_profile
            with _segment(sink,'xctest-prepare'):
                launch=runner.prepare(role='original',iteration=1,application_id=self.config.application_id,
                    profile_digest=profile.digest,actions=tuple(profile.data['capabilities']['actions']),
                    cancellation=cancellation,deadline_monotonic=deadline)
            provider=launch.payload['providerIncarnation']
            with _segment(sink,'session-start'):
                startup=self._permit('original_sanitation_start',launch.payload,provider)
                session=runner.start(launch,permit=startup,cancellation=cancellation,deadline_monotonic=deadline)
            helper=IOSHelperChannel(session)
            with _segment(sink,'helper-handshake'):
                helper.handshake(startup,cancellation=cancellation,deadline_monotonic=deadline)
            with _segment(sink,'helper-activate'):
                helper.activate(startup,identity,cancellation=cancellation,deadline_monotonic=deadline)
            reader=self.config.query.open_runtime_reader(native_owner=self._native)
            with _segment(sink,'initial-observation'):
                initial=reader.read(launch,cancellation=cancellation,deadline_monotonic=deadline)
            self._confirm(startup,initial.public())
            with _segment(sink,'cleanup-command'):
                cleanup=self._permit('original_sanitation_cleanup',command_payload('cleanup',{}),provider)
                helper.command('cleanup',{},cleanup,cancellation=cancellation,deadline_monotonic=deadline)
            with _segment(sink,'cleanup-observation'):
                observed=reader.read(launch,stage='cleanup',cancellation=cancellation,deadline_monotonic=deadline)
            with _segment(sink,'reader-close'):
                _require(reader.close(deadline_monotonic=deadline),'mobile_quarantined');reader=None
            with _segment(sink,'helper-shutdown'):
                result=helper.shutdown(cleanup,cancellation=cancellation,deadline_monotonic=deadline)
                _require(result.get('ok') is True and result['target']['terminationConfirmed']
                    and result['helper']['terminationConfirmed'] and result['host']['terminated'],'mobile_quarantined')
                self._confirm(cleanup,{'native':result,'sanitation':observed.public()})
                self._native.device.confirm_native_cleanup(cleanup)
            return observed
        finally:
            if reader is not None:reader.close(deadline_monotonic=deadline)
            with _segment(sink,'runner-close'):
                _require(runner.close(deadline_monotonic=deadline),'mobile_quarantined')

    def _close_native(self):
        if self._coordinator is not None:
            self._coordinator.close();self._coordinator=None
        if self._native is not None:
            self._native_manager.__exit__(None,None,None)
            self._native=None;self._native_manager=None

    def _quarantine(self):
        self._service_unknown=True
        if self._scope is None:return
        lab=self.config.lab
        try:
            with lab.lock:
                reservation,device=lab._require_retained_scope(self._scope,owner=self.config.owner,
                    device_id=self.config.device_id)
                handle=reservation._authority_handle
                if handle is not None:handle.revoke_dispatches()
                device.update(state='quarantined',sessionId=self._scope.reservation_id,
                    quarantineReason='ios_cleanup_unconfirmed')
                lab._persist_devices()
        except Exception:pass

    def cleanup(self, context, *, cancellation, deadline_monotonic):
        _require(type(context) is MobileContext,'mobile_policy_mismatch')
        evidence={'processes':False,'fixtures':False,'sanitation':False,'scopeReleased':False}
        timing=[]
        self._cleanup_timing=timing
        try:
            with self._callback(context,'cleanup'):
                _active(cancellation,deadline_monotonic)
                _require(self._native is not None and self._coordinator is not None and not self._service_unknown,'mobile_quarantined')
                self._coordinator.request_cleanup(context._operation_binding)
                with self._coordinator.callback(context._operation_binding,'cleanup') as token:
                    with _segment(timing,'fixture-verify'):
                        evidence['fixtures']=self._issues_clean()
                        _require(evidence['fixtures'] and self._settled(),'mobile_quarantined')
                    with _segment(timing,'restore-original-install'):
                        identity=self._install('restore-original',cancellation,deadline_monotonic)
                    with _segment(timing,'original-sanitation') as span:
                        span['segments']=sub=[]
                        sanitation=self._sanitize_original(identity,cancellation,deadline_monotonic,sub)
                    evidence['sanitation']=True;evidence['sanitationEvidenceDigest']=sanitation.evidence_digest
                    with _segment(timing,'post-sanitation-settled'):
                        evidence['processes']=self._settled()
                        _require(evidence['processes'],'mobile_quarantined')
                    with _segment(timing,'native-disposal'):
                        disposal=discard_native_staged(self._native,token,cancellation=cancellation,deadline_monotonic=deadline_monotonic)
                        self.operations.run_store.consume_ios_native_disposal(self._native,token,sanitation,disposal,
                            cancellation=cancellation,deadline_monotonic=deadline_monotonic)
                        evidence['disposalDigest']=disposal
                        token.complete(contracts.digest(evidence))
                with _segment(timing,'native-close'):
                    self._close_native()
                with _segment(timing,'scope-release'):
                    self.config.service.release_retained_device_scope(self._scope,owner=self.config.owner)
                evidence['scopeReleased']=True
                with self._changed:
                    self._completed.add(context.digest);self._context=None;self._scope=None
                    self._candidate_profile=None;self._candidate_identity=None;self._installed=False
                    self._issues.clear()
        except Exception:
            if (self._context is not None and self._context.digest==context.digest
                    and self._context._operation_binding is context._operation_binding):
                self._quarantine()
        return MobileCleanupObservation(context.digest,contracts.digest(evidence),evidence['processes'],
            evidence['fixtures'],evidence['sanitation'],evidence['scopeReleased'])

    def revoke(self):self._stopping.set()

    def close(self, *, deadline_monotonic=None):
        deadline=time.monotonic()+5 if deadline_monotonic is None else deadline_monotonic
        self.revoke()
        with self._changed:
            while self._phase is not None and time.monotonic()<deadline:
                self._changed.wait(max(0,min(.02,deadline-time.monotonic())))
            if self._phase is not None:return False
        try:self._close_native()
        except Exception:return False
        stopped=self.operations.close(deadline_monotonic=deadline)
        return stopped and self._context is None and self._scope is None
