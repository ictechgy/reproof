"""Thread-safe local sessions, fenced controllers and immutable gesture recordings."""
from __future__ import annotations
import base64
import copy
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
from .. import contracts
from ..core import ContractError,digest
from ..storage import read_json,write_json
from .authority import ProviderResult, canonical_device_fingerprint
from .retained_startup import RetainedStartupBinding

MAX_VIDEO_SINKS = 32


@dataclass(frozen=True, slots=True)
class TrustedDeviceReservation:
    reservation_id: str
    device_id: str
    owner: str = field(repr=False, compare=False)
    project_digest: str
    application_id: str
    build_id: str
    _authority_handle: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)
    _authority_grant: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TrustedDeviceScope:
    """Opaque owner capability retained across a sequence of fresh sessions."""
    scope_id: str
    reservation_id: str
    device_id: str
    owner: str = field(repr=False, compare=False)
    project_digest: str
    application_id: str
    build_id: str
    _reservation: TrustedDeviceReservation = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)


class LiveError(ContractError):
    def __init__(self,code,message,status=409):
        super().__init__(message);self.code=code;self.status=status


class _KnownInputRejection(LiveError):
    pass

def check(value,code,message,status=409):
    if not value:raise LiveError(code,message,status)


def public_id(value):
    check(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value),'invalid_argument','Invalid identifier',400)
    return value


def validate_gesture(action,payload):
    check(isinstance(payload,dict),'invalid_argument','Invalid input payload',400)
    keys={'tap':{'x','y'},'long_press':{'x','y','durationMs'},
          'swipe':{'fromX','fromY','toX','toY','durationMs'},'text':{'value'},
          'home':set(),'back':set(),'reset':set(),
          'rotate':{'orientation'},'launch':{'applicationId'},'terminate':{'applicationId'},
          'pointer':{'phase','pointerId','x','y'}}
    check(action in keys and set(payload)==keys[action],'unsupported_operation','Unsupported gesture or parameters',400)
    if action=='pointer':
        check(payload['phase'] in {'down','move','up','cancel'},'invalid_argument','Invalid pointer phase',400)
        check(type(payload['pointerId']) is int and 0<=payload['pointerId']<=4,'invalid_argument','Invalid pointer ID',400)
    for key in ('x','y','fromX','fromY','toX','toY'):
        if key in payload:
            v=payload[key];check(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1,'invalid_argument','Coordinates must be normalized',400)
    if 'durationMs' in payload:
        v=payload['durationMs'];check(type(v) is int and 50<=v<=3000,'invalid_argument','Gesture duration must be 50–3000 ms',400)
    if action=='text':
        value=payload['value'];check(isinstance(value,str) and len(value)<=256,'invalid_argument','Text exceeds 256 characters',400)
        try:value.encode('utf-8')
        except UnicodeError:raise LiveError('invalid_argument','Text is not valid Unicode',400)
    if action=='rotate':
        check(payload['orientation'] in {'portrait','landscape-left','landscape-right'},
              'invalid_argument','Invalid orientation',400)
    if action in {'launch','terminate'}:
        public_id(payload['applicationId'])


class Lab:
    def __init__(self,devices,output,*,idle_timeout=120,max_session_seconds=900,clock=None,
                 authority=None,parent_grant=None,recording_clock_sync=None,
                 recording_wall_clock_ms=None,delegated_authority_only=False,
                 project_grant_provider=None):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.devices={d['id']:dict(d,state='available',sessionId=None) for d in devices}
        check(type(idle_timeout) is int and 10<=idle_timeout<=3600,'invalid_timeout','Idle timeout must be 10–3600 seconds',400)
        check(type(max_session_seconds) is int and idle_timeout<=max_session_seconds<=7200,'invalid_timeout','Session lifetime must be at least the idle timeout and at most 7200 seconds',400)
        self.idle_timeout=idle_timeout;self.max_session_seconds=max_session_seconds;self.clock=clock or time.monotonic
        check((project_grant_provider is None or callable(project_grant_provider))
              and not (project_grant_provider is not None and (
                  authority is None or parent_grant is not None or delegated_authority_only))
              and type(delegated_authority_only) is bool
              and ((authority is None)==(parent_grant is None)
                   or (authority is not None and parent_grant is None
                       and (delegated_authority_only or project_grant_provider is not None))),
              'authority_configuration',
              'Host authority and parent grant must be configured together',400)
        self._owns_authority=False
        def native_kind(device):
            kind=device.get('kind')
            return (kind in {'android-live','android-adb','ios-simulator','ios-physical'}
                    or (isinstance(kind,str) and kind.startswith('ios-physical-')))
        for device in self.devices.values():
            if not native_kind(device):continue
            mode=device.get('capabilities',{}).get('authorityMode')
            if device.get('_remoteAuthority') is True:
                check(mode=='shared-v2','authority_configuration',
                      'Remote native provider lacks shared authority metadata',400)
            elif '_authority' not in device:
                check(mode=='legacy-offline-v1','authority_configuration',
                      'Native provider lacks canonical authority metadata',400)
        native_devices=[device for device in self.devices.values() if '_authority' in device]
        for device in native_devices:
            value=device['_authority']
            check(isinstance(value,dict) and set(value)=={'deviceKind','physicalId'}
                  and value['deviceKind'] in {'android','ios-physical','ios-simulator'}
                  and isinstance(value['physicalId'],str) and bool(value['physicalId']),
                  'authority_configuration','Invalid trusted device inventory',400)
            capabilities=device.setdefault('capabilities',{})
            check(capabilities.get('authorityMode','shared-v2')=='shared-v2',
                  'authority_configuration','Canonical provider mode is incompatible',400)
            capabilities['authorityMode']='shared-v2'
        # Shared native mode never manufactures a coordinator grant. The
        # embedding service must inject its process-local HostAuthority and
        # verified parent grant; create_session rejects safely when absent.
        self.authority=authority;self.parent_grant=parent_grant
        self.project_grant_provider=project_grant_provider
        self.remote_inventory=None
        self._connected_device_ids=None
        self.delegated_authority_only=delegated_authority_only
        self._recording_clock_sync=recording_clock_sync
        self._recording_wall_clock_ms=recording_wall_clock_ms
        self._recording_budget=None;self._evidence_store=None;self._recording_store=None
        self._evidence_closed=False
        self._video_sinks=[]
        self._device_reservation_issuer=object();self._device_reservations={}
        self._retained_scope_issuer=object();self._retained_device_scopes={}
        self._retained_scope_sequences={}
        self._retained_startup_issuer=object();self._retained_startup_bindings={}
        self.release_recording_owners={}
        self.sessions={};self.recordings={};self.lock=threading.RLock();self.base_url=''
        self.maintenance_stop=threading.Event();self.maintenance_thread=None;self.device_history={};self.recording_load_errors=[]
        state_path=self.output/'device-state.json'
        if state_path.exists():
            try:
                stored_states=read_json(state_path)
                check(isinstance(stored_states,dict),'invalid_state','Invalid device state journal')
            except (ContractError,ValueError,TypeError,OSError):stored_states={key:{'state':'unknown'} for key in self.devices}
            self.device_history=copy.deepcopy(stored_states)
            for key,device in self.devices.items():
                old=stored_states.get(key,{})
                if not isinstance(old,dict) or old.get('state','available')!='available':
                    device.update(state='quarantined',quarantineReason='previous_session_unfinished')
        for path in (self.output/'recordings').glob('*.json'):
            try:
                from .recordings import validate_recording
                stored=read_json(path);r=validate_recording(stored['recording'])
                check(r['id']==path.stem and isinstance(stored['owner'],str),'invalid_recording','Recording path or owner mismatch')
                self.recordings[r['id']]={'owner':stored['owner'],'data':r}
            except (ContractError,KeyError,TypeError,ValueError,OSError):self.recording_load_errors.append(path.stem)
    def register_recording_project(self,project,collection_policy,*,
                                   capacity_bytes=1024*1024*1024,
                                   journal_headroom_bytes=16*1024*1024):
        """Register trusted local project policy; JSON alone remains inert."""
        with self.lock:
            if self._recording_store is None:
                try:
                    from .clock_sync import ClockSynchronizer
                    from .disk_budget import DiskBudget
                    from .evidence_store import EvidenceStore
                    from .recording_session import RecordingStore
                    sync=(self._recording_clock_sync or
                          (self.authority.clock_sync if self.authority is not None else ClockSynchronizer()))
                    root=self.output/'evidence-v1'
                    self._recording_budget=DiskBudget(
                        root/'budget',capacity_bytes=capacity_bytes,
                        journal_headroom_bytes=journal_headroom_bytes,
                        _allow_capacity_growth=True)
                    self._evidence_store=EvidenceStore(root/'objects',self._recording_budget)
                    self._recording_store=RecordingStore(
                        root/'recordings',self._evidence_store,sync,
                        wall_clock_ms=self._recording_wall_clock_ms)
                    self._evidence_closed=False
                except Exception:
                    self.close_evidence_store()
                    raise LiveError('recording_unavailable','Durable recording storage is unavailable') from None
            store=self._recording_store
        try:registration=store.register_project(project,collection_policy)
        except Exception:raise LiveError('invalid_recording_policy','Trusted recording policy is invalid',400) from None
        pending=store.pending_video_recoveries(registration.project_digest)
        video_root=self.output/'evidence-v1'/'video'
        if pending or (video_root/'video.sqlite3').exists():
            from .video import VideoCatalog
            catalog=VideoCatalog(video_root,self._evidence_store)
            try:
                catalog.reconcile_prebindings(store,registration.project_digest)
                for recording_id in store.pending_video_recoveries(registration.project_digest):
                    catalog.recover_transient(store.recovery_session(recording_id))
            except Exception:
                raise LiveError('recording_unavailable','Interrupted video recovery is unavailable',409) from None
            finally:catalog.close()
        return registration
    def create_video_sink(self,*,helper,limits=None,backpressure_policy='gap',
                          fault_mode='none',source_mode='transient-spool-v2'):
        """Create a trusted G3 sink; the recording identity is bound later.

        ``helper`` is local trusted composition input, never recording wire data.
        Segment work paths are derived below the Lab-owned evidence root.
        """
        with self.lock:
            check(self._recording_store is not None and self._evidence_store is not None,
                  'recording_unavailable','Durable recording storage is unavailable')
            check(len(self._video_sinks)<MAX_VIDEO_SINKS,
                  'recording_unavailable','Video sink process limit is reached')
            try:
                from .video import VideoFrameSink, VideoLimits
                if limits is None:
                    limits=(VideoLimits.recording_profile() if source_mode=='transient-spool-v2'
                            else VideoLimits())
                sink=VideoFrameSink(
                    self._evidence_store,self.output/'evidence-v1'/'video',
                    helper=Path(helper),limits=limits,
                    backpressure_policy=backpressure_policy,fault_mode=fault_mode,
                    source_mode=source_mode)
            except Exception:
                raise LiveError('recording_unavailable','Video encoder is unavailable') from None
            self._video_sinks.append(sink)
            return sink

    def create_issue_session_service(self,fixtures,*,root=None,
                                     scenario_registry=None,
                                     scenario_runner=None):
        """Compose the trusted G4 service without accepting wire executors."""
        from .issue_sessions import IssueSessionService
        return IssueSessionService(
            self,fixtures,root=root,scenario_registry=scenario_registry,
            scenario_runner=scenario_runner)
    def _validate_release_selection(self,device_id,registration,application_id,
                                    build_id,authority_grant=None,*,candidate_binding=None,
                                    _identity_override=None,_profile_override=None):
        check(self._recording_store is not None,'recording_unavailable',
              'Durable recording storage is unavailable')
        try:
            registration=self._recording_store._require_registration(registration)
            device=self.devices.get(device_id);check(device is not None,'not_found','Device not found',404)
            project=registration.project
            application=next((item for item in project['applications'] if item['id']==application_id),None)
            build=next((item for item in project['builds'] if item['id']==build_id),None)
            if candidate_binding is not None:
                from ..qualification import require_candidate_binding
                build=require_candidate_binding(candidate_binding,project_digest=registration.project_digest,
                    application_id=application_id,build_id=build_id)
            identity=(copy.deepcopy(_identity_override) if _identity_override is not None else
                      device.get('capabilities',{}).get('applicationIdentity'))
            check(application is not None and build is not None
                  and build['applicationId']==application_id,
                  'recording_identity','Application build is not registered',400)
            check(application['platform']==device.get('platform'),
                  'recording_identity','Application platform does not match device',409)
            check(isinstance(identity,dict)
                  and identity.get('bundle')==application['bundle']
                  and identity.get('artifactDigest')==build['artifactDigest'],
                  'recording_identity','Installed application build does not match registration',409)
            profile=(_profile_override if _profile_override is not None else
                     device.get('capabilities',{}).get('applicationProfile'))
            if profile is not None:
                profile_digest=(digest(profile) if _profile_override is not None else
                                device['capabilities'].get('applicationProfileDigest'))
                check(isinstance(profile,dict)
                      and isinstance(profile_digest,str)
                      and digest(profile)==profile_digest
                      and identity.get('applicationProfileDigest')==profile_digest
                      and profile.get('projectId')==project['id']
                      and profile.get('projectDigest')==registration.project_digest
                      and profile.get('applicationId')==application_id
                      and profile.get('buildId')==build_id
                      and profile.get('platform')==application['platform']
                      and profile.get('bundle',profile.get('package'))
                          == application['bundle']
                      and isinstance(profile.get('artifact'),dict)
                      and profile['artifact'].get('sha256')
                          == build['artifactDigest'],
                      'recording_identity',
                      'Runtime application profile does not match registration',409)
                observations=profile.get('capabilities',{}).get('observations')
                check(type(observations) is list
                      and all(type(category) is str and project['evidencePolicy'].get(category) is True
                              for category in observations),
                      'capture_suppressed','Runtime observations exceed the trusted collection policy',403)
            native_kind=(device.get('kind') in {
                'android-live','android-adb','ios-simulator','ios-physical'
            } or (isinstance(device.get('kind'),str)
                  and device['kind'].startswith('ios-physical-')))
            if native_kind:
                policy=registration.collection_policy
                check(policy['captureMode']=='test-data'
                      and project['evidencePolicy']['pixels'] is True,
                      'capture_suppressed',
                      'Native provider cannot honor this capture policy',403)
            selected=authority_grant if authority_grant is not None else self.parent_grant
            if device.get('_remoteAuthority') is True:
                check(selected is not None or self.project_grant_provider is not None,
                      'authority_unavailable','Originating host authority is unavailable',409)
            if '_authority' in device or device.get('_remoteAuthority') is True:
                check((selected is None and self.project_grant_provider is not None)
                      or selected is not None and getattr(selected,'project_id',None)==project['id'],
                      'recording_identity','Host grant project does not match registration',409)
            return registration,project,device
        except LiveError:raise
        except Exception:raise LiveError('recording_identity','Release recording identity is invalid',400) from None

    def _release_grant(self,device,registration,grant,reservation=None):
        if reservation is not None:
            check(type(reservation) is TrustedDeviceReservation
                  and reservation._issuer is self._device_reservation_issuer,
                  'stale_controller','Device reservation is stale')
            held=reservation._authority_grant
            check(grant is None or grant is held,'recording_identity',
                  'Reserved authority cannot be replaced',409)
            return held
        selected=grant if grant is not None else self.parent_grant
        if selected is None and self.project_grant_provider is not None \
                and ('_authority' in device or device.get('_remoteAuthority') is True):
            selected=self.project_grant_provider(registration)
            check(getattr(selected,'project_id',None)==registration.project['id'],
                  'recording_identity','Host grant project does not match registration',409)
            self.authority._require_parent_grant(selected)
        return selected

    def reserve_release_device(self,device_id,owner,reservation_id,registration,*,
                               application_id,build_id,authority_grant=None,candidate_binding=None):
        """Validate and reserve a device before any fixture mutation occurs."""
        registration,_,device=self._validate_release_selection(
            device_id,registration,application_id,build_id,authority_grant,candidate_binding=candidate_binding)
        self._check_device_admission(device)
        authority_grant=self._release_grant(device,registration,authority_grant)
        public_id(reservation_id)
        check(isinstance(owner,str) and bool(owner),'invalid_argument','Invalid owner',400)
        with self.lock:
            check(device['state']=='available','device_busy','Device is already allocated')
            check(reservation_id not in self._device_reservations,
                  'invalid_argument','Reservation identity is already in use',400)
            native=(device.get('kind') in {'android-live','android-adb','ios-simulator','ios-physical'}
                    or str(device.get('kind','')).startswith('ios-physical-'))
            check(not native or '_authority' in device,'authority_unavailable',
                  'Prepared native recording requires shared device authority')
            handle=None
            if device.get('_remoteAuthority') is True:
                selected=authority_grant if authority_grant is not None else self.parent_grant
                reserver=device.get('_remoteReservation')
                check(self.authority is not None and selected is not None
                      and callable(reserver),'authority_unavailable',
                      'Prepared recording requires a reservation on the physical worker')
                handle=reserver(
                    self,registration=registration,application_id=application_id,
                    build_id=build_id,reservation_id=reservation_id,
                    authority_grant=selected)
                try:handle.check_ownership()
                except Exception:
                    try:handle.close()
                    except Exception:pass
                    device.update(state='quarantined',sessionId=None);self._persist_devices()
                    raise
            elif '_authority' in device:
                selected=authority_grant if authority_grant is not None else self.parent_grant
                check(self.authority is not None and selected is not None,
                      'authority_unavailable','Host authority is unavailable')
                trusted=device['_authority']
                handle=self.authority.claim_device(
                    device_kind=trusted['deviceKind'],physical_id=trusted['physicalId'],
                    display_alias=device_id,helper_incarnation='helper_'+uuid.uuid4().hex,
                    parent_grant=selected)
                try:handle.check_ownership()
                except Exception:
                    handle.close()
                    device.update(state='quarantined',sessionId=None);self._persist_devices()
                    raise
            capability=TrustedDeviceReservation(
                reservation_id,device_id,owner,registration.project_digest,
                application_id,build_id,handle,self._device_reservation_issuer,authority_grant)
            self._device_reservations[reservation_id]=capability
            device.update(state='reserved',sessionId=reservation_id)
            try:self._persist_devices()
            except Exception:
                self._device_reservations.pop(reservation_id,None)
                released=handle is None or handle.close()
                device.update(state='available' if released else 'quarantined',sessionId=None)
                raise LiveError('recording_unavailable','Device reservation could not be recorded') from None
            return capability

    def retain_device_reservation(self, reservation):
        """Promote one trusted reservation to an enclosing repair scope.

        The reservation remains owned by this Lab until
        :meth:`release_retained_device_scope` succeeds.  The returned object is
        process-local and issuer-bound; it cannot be reconstructed from wire
        data or used by another Lab.
        """
        check(type(reservation) is TrustedDeviceReservation
              and reservation._issuer is self._device_reservation_issuer,
              'invalid_argument','Trusted device reservation is required',400)
        with self.lock:
            current=self._device_reservations.get(reservation.reservation_id)
            device=self.devices.get(reservation.device_id)
            check(current is reservation and device is not None
                  and device['state']=='reserved'
                  and device['sessionId']==reservation.reservation_id,
                  'stale_controller','Device reservation is stale')
            check(reservation.reservation_id not in self._retained_device_scopes,
                  'invalid_argument','Device reservation scope is already retained',400)
            scope=TrustedDeviceScope(
                'scope_'+uuid.uuid4().hex, reservation.reservation_id,
                reservation.device_id, reservation.owner, reservation.project_digest,
                reservation.application_id, reservation.build_id, reservation,
                self._retained_scope_issuer)
            self._retained_device_scopes[reservation.reservation_id]=scope
            self._retained_scope_sequences[reservation.reservation_id]=0
            return scope

    def begin_retained_device_scope(self, device_id, owner, reservation_id, registration, *,
                                    application_id, build_id, authority_grant=None,
                                    candidate_binding=None):
        """Reserve and retain a native device for an enclosing repair owner."""
        reservation=self.reserve_release_device(
            device_id,owner,reservation_id,registration,
            application_id=application_id,build_id=build_id,
            authority_grant=authority_grant,candidate_binding=candidate_binding)
        try:
            return self.retain_device_reservation(reservation)
        except Exception:
            try:self.release_device_reservation(reservation)
            except Exception:pass
            raise

    def _require_retained_scope(self, scope, *, owner=None, device_id=None,
                                require_reserved=False):
        check(type(scope) is TrustedDeviceScope
              and scope._issuer is self._retained_scope_issuer,
              'stale_controller','Trusted retained device scope is required')
        current=self._retained_device_scopes.get(scope.reservation_id)
        check(current is scope,'stale_controller','Trusted retained device scope is stale')
        reservation=scope._reservation
        check(self._device_reservations.get(scope.reservation_id) is reservation
              and reservation.device_id==scope.device_id
              and reservation.owner==scope.owner,
              'stale_controller','Trusted retained device scope is stale')
        if owner is not None:
            check(owner==scope.owner,'stale_controller','Device scope owner changed')
        if device_id is not None:
            check(device_id==scope.device_id,'stale_controller','Device scope device changed')
        device=self.devices.get(scope.device_id)
        check(device is not None and device['state'] in {'reserved','busy','quarantined'},
              'stale_controller','Trusted retained device scope is stale')
        if device['state']=='reserved':
            check(device['sessionId']==scope.reservation_id,
                  'stale_controller','Trusted retained device scope is stale')
        elif device['state']=='busy':
            session=self.sessions.get(device['sessionId'])
            check(session is not None and session.get('_retainedDeviceScope') is scope,
                  'stale_controller','Trusted retained device scope is stale')
        elif device['state']=='quarantined':
            check(device['sessionId']==scope.reservation_id,
                  'stale_controller','Trusted retained device scope is stale')
        if require_reserved:
            check(device['state']=='reserved' and
                  device['sessionId']==scope.reservation_id,
                  'device_busy','Retained device scope is not available for a new session')
            handle=reservation._authority_handle
            if handle is not None:
                try:handle.check_ownership()
                except Exception:
                    raise LiveError('authority_unavailable',
                                    'Retained device authority is unavailable') from None
        return reservation,device

    def validate_retained_device_scope(self, scope, *, owner, device_id,
                                       registration=None, application_id=None,
                                       build_id=None, candidate_binding=None,
                                       _candidate_identity=None,
                                       _candidate_profile=None):
        """Validate an enclosing scope before allocating fixtures or a session."""
        with self.lock:
            reservation,_=self._require_retained_scope(
                scope,owner=owner,device_id=device_id,require_reserved=True)
            if registration is not None:
                check(application_id is not None and build_id is not None,
                      'recording_identity','Retained session selection is incomplete',409)
                check(reservation.project_digest==registration.project_digest
                      and reservation.application_id==application_id,
                      'recording_identity','Retained session selection changed',409)
                if candidate_binding is None:
                    check(reservation.build_id==build_id,
                          'recording_identity','Retained session build changed',409)
                self._validate_release_selection(
                    device_id,registration,application_id,build_id,
                    candidate_binding=candidate_binding,
                    _identity_override=_candidate_identity,
                    _profile_override=_candidate_profile)

    @staticmethod
    def _validate_startup_logical_payload(value):
        check(type(value) is dict and set(value)=={'kind','applicationIdentity'},
              'invalid_argument','Invalid retained startup logical payload',400)
        check(type(value['kind']) is str and bool(value['kind']),
              'invalid_argument','Invalid retained startup device kind',400)
        check(type(value['applicationIdentity']) is dict,
              'invalid_argument','Invalid retained startup application identity',400)

    def _validate_retained_startup_payload(self, scope, device, *,
                                           logical_payload, native_payload,
                                           provider_incarnation, provider=None):
        self._validate_startup_logical_payload(logical_payload)
        check(type(native_payload) is dict,
              'invalid_argument','Invalid retained startup native payload',400)
        required={'kind','contextDigest','nativeBindingDigest','scopeDigest',
                  'applicationId','providerIncarnation'}
        check(required <= set(native_payload),
              'invalid_argument','Retained startup native payload is incomplete',400)
        check(native_payload['kind']=='ios-fixed-xctest-launch-v1',
              'invalid_argument','Unsupported retained startup native payload',400)
        for name in ('contextDigest','nativeBindingDigest','scopeDigest'):
            try:contracts.validate_digest(native_payload[name])
            except Exception:
                raise LiveError('invalid_argument',
                                'Invalid retained startup native payload digest',400) from None
        check(native_payload['applicationId']==scope.application_id,
              'recording_identity','Retained startup application changed',409)
        check(native_payload['providerIncarnation']==provider_incarnation,
              'invalid_argument','Retained startup provider incarnation changed',400)
        trusted=device.get('_authority')
        check(type(trusted) is dict and trusted.get('deviceKind')=='ios-physical'
              and type(trusted.get('physicalId')) is str and bool(trusted['physicalId']),
              'authority_unavailable','Retained iOS device authority is unavailable',409)
        try:expected_scope_digest=canonical_device_fingerprint(
            trusted['deviceKind'],trusted['physicalId'])
        except Exception:
            raise LiveError('authority_unavailable',
                            'Retained iOS device authority is unavailable',409) from None
        check(native_payload['scopeDigest']==expected_scope_digest,
              'recording_identity','Retained startup device scope changed',409)
        if 'projectDigest' in native_payload:
            try:contracts.validate_digest(native_payload['projectDigest'])
            except Exception:
                raise LiveError('invalid_argument',
                                'Invalid retained startup project digest',400) from None
            check(native_payload['projectDigest']==scope.project_digest,
                  'recording_identity','Retained startup project changed',409)
        try:
            binding=RetainedStartupBinding(
                issuer=self._retained_startup_issuer,scope=scope,owner=scope.owner,
                provider=provider,logical_payload=logical_payload,
                native_payload=native_payload,
                provider_incarnation=provider_incarnation)
        except (TypeError,ValueError):
            raise LiveError('invalid_argument',
                            'Retained startup payload is not JSON data',400) from None
        return binding

    def bind_retained_startup(self, scope, *, owner, provider, logical_payload,
                              native_payload, provider_incarnation):
        """Issue one opaque binding for a retained physical iOS startup.

        The provider and both payloads stay process-local.  The returned
        capability is accepted only by this Lab, this exact retained scope,
        and one subsequent session creation.
        """
        public_id(provider_incarnation)
        check(provider is not None,
              'invalid_argument','Retained startup provider is required',400)
        with self.lock:
            reservation,device=self._require_retained_scope(
                scope,owner=owner,require_reserved=True)
            check(device.get('kind')=='ios-physical'
                  and reservation._authority_handle is not None,
                  'invalid_argument','Retained startup requires an iOS physical device',400)
            check(scope.project_digest==reservation.project_digest
                  and scope.application_id==reservation.application_id,
                  'stale_controller','Retained device scope identity changed')
            binding=self._validate_retained_startup_payload(
                scope,device,logical_payload=logical_payload,
                native_payload=native_payload,
                provider_incarnation=provider_incarnation,provider=provider)
            # The exact provider object is retained in the opaque capability;
            # no caller-owned copy or serialized representation is accepted.
            self._retained_startup_bindings[id(binding)]=binding
            return binding

    def _consume_retained_startup(self, binding, scope, owner, device,
                                  *, candidate_identity=None):
        check(type(binding) is RetainedStartupBinding
              and binding._issuer is self._retained_startup_issuer,
              'stale_controller','Trusted retained startup binding is required')
        check(self._retained_startup_bindings.get(id(binding)) is binding,
              'stale_controller','Retained startup binding is stale or already used')
        check(binding.scope is scope and binding.owner==owner,
              'stale_controller','Retained startup binding scope changed')
        logical=binding.logical_payload
        selected=(copy.deepcopy(candidate_identity) if candidate_identity is not None
                  else copy.deepcopy(device.get('capabilities',{}).get('applicationIdentity')))
        expected={'kind':device.get('kind'),'applicationIdentity':selected}
        check(logical==expected,
              'recording_identity','Retained startup selection changed',409)
        native=binding.native_payload
        self._validate_retained_startup_payload(
            scope,device,logical_payload=logical,native_payload=native,
            provider_incarnation=binding.provider_incarnation)
        # Removal is the one-use boundary.  It happens before the factory or
        # authority can be reached, so a failed provider handoff cannot replay
        # the capability or create a native operation.
        self._retained_startup_bindings.pop(id(binding),None)
        return binding

    def prepare_retained_scope_effect(self, scope, *, owner, kind, payload,
                                      provider_incarnation, operation_id=None):
        """Admit a scope-level effect using the enclosing authority sequence."""
        check(type(kind) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',kind),
              'invalid_argument','Invalid retained effect kind',400)
        public_id(provider_incarnation)
        try:payload_digest=self._parameterized_digest(kind,payload)
        except Exception:
            raise LiveError('invalid_argument','Invalid retained effect payload',400) from None
        return self._prepare_retained_scope_digest_effect(
            scope,owner=owner,kind=kind,payload_digest=payload_digest,
            provider_incarnation=provider_incarnation,operation_id=operation_id)

    def prepare_retained_scope_exact_effect(self, scope, *, owner, kind,
                                            payload_digest,
                                            provider_incarnation,
                                            operation_id=None):
        """Admit a fixed native effect by its already-canonical payload digest."""
        check(type(kind) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',kind),
              'invalid_argument','Invalid retained effect kind',400)
        public_id(provider_incarnation)
        try:contracts.validate_digest(payload_digest)
        except Exception:
            raise LiveError('invalid_argument','Invalid retained effect payload digest',400) from None
        return self._prepare_retained_scope_digest_effect(
            scope,owner=owner,kind=kind,payload_digest=payload_digest,
            provider_incarnation=provider_incarnation,operation_id=operation_id)

    def _prepare_retained_scope_digest_effect(self, scope, *, owner, kind,
                                              payload_digest,
                                              provider_incarnation,
                                              operation_id=None):
        with self.lock:
            reservation,_=self._require_retained_scope(
                scope,owner=owner,require_reserved=True)
            handle=reservation._authority_handle
            check(handle is not None,'authority_unavailable',
                  'Retained scope has no native authority',409)
            sequence=self._retained_scope_sequences[scope.reservation_id]+1
            identifier=operation_id or self._operation_id(
                'scope_'+scope.scope_id,kind+'-'+str(sequence))
            public_id(identifier)
            try:
                admission=handle.admit_operation(
                    operation_id=identifier,payload_digest=payload_digest,
                    session_id='scope_'+scope.scope_id,sequence=sequence)
                self._retained_scope_sequences[scope.reservation_id]=sequence
                return handle.prepare_dispatch(
                    admission,provider_incarnation=provider_incarnation)
            except Exception:
                raise LiveError('authority_rejected',
                                'Retained scope authority rejected the operation') from None

    def confirm_retained_scope_effect(self, scope, permit, *, owner, status,
                                      result_digest):
        """Confirm a scope-level effect through the same durable authority journal."""
        with self.lock:
            reservation,_=self._require_retained_scope(
                scope,owner=owner)
            handle=reservation._authority_handle
            check(handle is not None,'authority_unavailable',
                  'Retained scope has no native authority',409)
            try:
                contracts.validate_digest(result_digest)
                receipt_id='receipt_'+hashlib.sha256(
                    (permit.operation_id+'\0'+status+'\0'+result_digest).encode()
                ).hexdigest()[:40]
                return handle.confirm_operation(
                    permit,ProviderResult(receipt_id,status,result_digest))
            except Exception:
                raise LiveError('authority_rejected',
                                'Retained scope effect could not be confirmed') from None

    def release_retained_device_scope(self, scope):
        """Release an enclosing scope only after all retained sessions ended."""
        with self.lock:
            reservation,device=self._require_retained_scope(scope, owner=scope.owner)
            active=[session for session in self.sessions.values()
                    if session.get('_retainedDeviceScope') is scope
                    and session.get('state') not in {'closed','failed'}]
            check(not active,'device_busy','Retained device scope still has an active session')
            check(device['state']=='reserved' and
                  device['sessionId']==reservation.reservation_id,
                  'cleanup_uncertain','Retained device scope is not sanitized')
            handle=reservation._authority_handle
            try:
                released=handle is None or handle.close()
            except Exception:
                released=False
            if not released:
                device.update(state='quarantined',sessionId=reservation.reservation_id)
                self._persist_devices()
                raise LiveError('cleanup_uncertain','Device scope release is unconfirmed')
            for key,binding in list(self._retained_startup_bindings.items()):
                if binding.scope is scope:
                    self._retained_startup_bindings.pop(key,None)
            self._retained_device_scopes.pop(scope.reservation_id,None)
            self._retained_scope_sequences.pop(scope.reservation_id,None)
            self._device_reservations.pop(reservation.reservation_id,None)
            device.update(state='available',sessionId=None)
            self._persist_devices()
            return {'deviceId':scope.device_id,'state':'available'}

    def release_device_reservation(self,reservation):
        check(type(reservation) is TrustedDeviceReservation
              and reservation._issuer is self._device_reservation_issuer,
              'invalid_argument','Trusted device reservation is required',400)
        with self.lock:
            check(reservation.reservation_id not in self._retained_device_scopes,
                  'device_busy','Device reservation is retained by an enclosing scope')
            current=self._device_reservations.get(reservation.reservation_id)
            if current is None and any(
                    session.get('_releasedDeviceReservation') is reservation
                    for session in self.sessions.values()):
                # A failed startup already released this exact issuing handle.
                # This receipt never changes a subsequent owner's device state.
                return {'deviceId':reservation.device_id,'state':'available'}
            check(current is reservation,'stale_controller','Device reservation is stale')
            device=self.devices[reservation.device_id]
            check(device['state']=='reserved'
                  and device['sessionId']==reservation.reservation_id,
                  'stale_controller','Device reservation is stale')
            handle=reservation._authority_handle
            released=handle is None or handle.close()
            self._device_reservations.pop(reservation.reservation_id,None)
            device.update(state='available' if released else 'quarantined',sessionId=None)
            self._persist_devices()
            check(released,'cleanup_uncertain','Device reservation release is unconfirmed')
        return {'deviceId':reservation.device_id,'state':'available'}

    def create_release_session(self,device_id,owner,client_id,registration,*,
                               application_id,build_id,preparation_receipts,
                               authority_grant=None,frame_sink=None,
                               device_reservation=None,session_id=None,
                               recording_id=None,effect_authorizer=None,candidate_binding=None,
                               preparation_unknown=False,device_scope=None,
                               _candidate_identity=None,_candidate_profile=None,
                               _provider_factory=None,_startup_binding=None):
        check(type(preparation_unknown) is bool, 'recording_invalid', 'Invalid initial-condition state', 400)
        if frame_sink is not None:
            check(callable(getattr(frame_sink,'accept_frame',None)),
                  'recording_unavailable','Recording frame sink is invalid',400)
        check(not (device_reservation is not None and device_scope is not None),
              'invalid_argument','Select one device reservation mode',400)
        if device_scope is not None:
            self._require_retained_scope(device_scope,owner=owner,device_id=device_id,
                                         require_reserved=True)
            check(_provider_factory is None or callable(_provider_factory),
                  'invalid_argument','Invalid retained provider factory',400)
            if _startup_binding is not None:
                check(type(_startup_binding) is RetainedStartupBinding
                      and _startup_binding.scope is device_scope,
                      'stale_controller','Retained startup binding scope changed')
        else:
            check(_candidate_identity is None and _candidate_profile is None
                  and _provider_factory is None and _startup_binding is None,
                  'invalid_argument','Candidate session binding requires a retained scope',400)
        registration,_,device=self._validate_release_selection(
            device_id,registration,application_id,build_id,authority_grant,
            candidate_binding=candidate_binding,
            _identity_override=_candidate_identity if device_scope is not None else None,
            _profile_override=_candidate_profile if device_scope is not None else None)
        authority_grant=self._release_grant(
            device,registration,authority_grant,
            device_reservation if device_scope is None else device_scope._reservation)
        if session_id is not None:public_id(session_id)
        if recording_id is not None:public_id(recording_id)
        release={'registration':registration,'applicationId':application_id,'buildId':build_id,
                 'preparation':copy.deepcopy(preparation_receipts),
                 'preparationUnknown':preparation_unknown,
                 'recordingId':recording_id or 'recording_'+uuid.uuid4().hex,
                 'frameSink':frame_sink,'candidateBinding':candidate_binding}
        return self.create_session(device_id,owner,client_id,authority_grant=authority_grant,
                                   _release=release,
                                   _device_reservation=device_reservation,
                                   _device_scope=device_scope,
                                   _candidate_identity=_candidate_identity,
                                   _candidate_profile=_candidate_profile,
                                   _provider_factory=_provider_factory,
                                   _startup_binding=_startup_binding,
                                   _session_id=session_id,
                                   _effect_authorizer=effect_authorizer)
    def _check_device_admission(self,device):
        check(self._connected_device_ids is None or device['id'] in self._connected_device_ids,
              'device_unavailable','Configured device is disconnected')
        binding=device.get('_inventoryBinding')
        if binding is not None:
            check(self.remote_inventory is not None,'inventory_unavailable',
                  'Enrolled device inventory is unavailable')
            self.remote_inventory.require_available(binding)
        check(not device.get('_recoveryOnly',False),'recovery_only',
              'This service registers the device for protected recovery only')

    def apply_device_presence(self,present_ids):
        with self.lock:
            check(type(present_ids) in (set,frozenset) and present_ids<=set(self.devices),
                  'invalid_argument','Invalid configured device presence',400)
            self._connected_device_ids=frozenset(present_ids)

    def inventory_devices(self):
        with self.lock:
            return [dict(device) for key,device in self.devices.items()
                    if self._connected_device_ids is None or key in self._connected_device_ids]

    def list_devices(self):
        with self.lock:
            devices=[(dict(d),{k:copy.deepcopy(v) for k,v in d.items()
                               if k!='factory' and not k.startswith('_')})
                     for d in self.devices.values()]
        for descriptor,public in devices:
            if self._connected_device_ids is not None and descriptor['id'] not in self._connected_device_ids:
                public['state']='missing' if public['state']=='available' else 'uncertain'
                continue
            if public['state']=='available':
                try:self._check_device_admission(descriptor)
                except LiveError as error:
                    if error.code!='recovery_only' or not descriptor.get('_recoveryOnly',False):public['state']='uncertain'
        return [public for _,public in devices]
    def _session(self,sid,owner=None):
        with self.lock:s=self.sessions.get(sid)
        check(s is not None,'not_found','Session not found',404)
        if owner is not None:check(s['owner']==owner,'forbidden','Session belongs to another owner',403)
        return s
    def _public(self,s):
        with s['frameLock']:
            frame=s['frame'];meta={k:v for k,v in frame.items() if k not in {'bytes','imageBase64','_received'}} if frame else None
        capabilities=copy.deepcopy(self.devices[s['deviceId']]['capabilities'])
        if 'automaticAppLogs' in s:capabilities['automaticAppLogs']=s['automaticAppLogs']
        result={'id':s['id'],'deviceId':s['deviceId'],'state':s['state'],'controllerId':s['controllerId'],
                'epoch':s['epoch'],'lastSequence':s['lastSequence'],'mode':s['mode'],'recordingId':s.get('recordingId'),
                'frame':meta,'capabilities':capabilities,
                'metrics':dict(s['metrics']),'replay':copy.deepcopy(s.get('replay')),'error':s.get('error'),
                'createdAt':s['createdAt'],'expiresAt':int(time.time()*1000+max(0,min(s['lastActivity']+self.idle_timeout,s['createdMonotonic']+self.max_session_seconds)-self.clock())*1000),
                'idleTimeoutSeconds':self.idle_timeout,'maxSessionSeconds':self.max_session_seconds,'closeReason':s.get('closeReason'),
                'activePointerIds':sorted(s.get('activePointers',{})),'pointerTimeoutSeconds':10}
        if s.get('releaseRecordingId') is not None:
            result['releaseRecordingId']=s['releaseRecordingId']
        return result
    def get_session(self,sid,owner=None):
        s=self._session(sid,owner)
        with s['lock']:
            if owner is not None:
                self._authorize_effect(s,'session')
                self._renew(s)
            return self._public(s)
    def peek_session(self,sid,owner=None):
        """Return current state without extending its operation lease."""
        s=self._session(sid,owner)
        with s['lock']:return self._public(s)
    def create_session(self,device_id,owner,client_id,*,authority_grant=None,_release=None,
                       _device_reservation=None,_device_scope=None,_session_id=None,
                       _effect_authorizer=None,_candidate_identity=None,
                       _candidate_profile=None,_provider_factory=None,
                       _startup_binding=None):
        public_id(client_id)
        if _session_id is not None:public_id(_session_id)
        check(_effect_authorizer is None or callable(_effect_authorizer),
              'invalid_argument','Invalid effect authorizer',400)
        check(_startup_binding is None or _device_scope is not None,
              'invalid_argument','Retained startup binding requires a retained scope',400)
        check(_startup_binding is None or callable(_provider_factory),
              'invalid_argument','Retained startup binding requires its provider factory',400)
        startup_binding=None;startup_native_payload=None
        startup_provider_incarnation=None
        with self.lock:
            device=self.devices.get(device_id);check(device is not None,'not_found','Device not found',404)
            if _device_scope is not None:
                reservation,_=self._require_retained_scope(
                    _device_scope,owner=owner,device_id=device_id,
                    require_reserved=True)
                check(_device_reservation is None and
                      (_provider_factory is None or callable(_provider_factory)),
                      'invalid_argument','Invalid retained session binding',400)
                check(_release is None or
                      (reservation.project_digest==_release['registration'].project_digest
                       and reservation.application_id==_release['applicationId']),
                      'recording_identity','Retained session selection changed',409)
                if _startup_binding is not None:
                    startup_binding=self._consume_retained_startup(
                        _startup_binding,_device_scope,owner,device,
                        candidate_identity=_candidate_identity)
                    startup_native_payload=startup_binding.native_payload
                    startup_provider_incarnation=startup_binding.provider_incarnation
            elif _device_reservation is None:
                check(_candidate_identity is None and _candidate_profile is None
                      and _provider_factory is None,
                      'invalid_argument','Candidate session binding requires a retained scope',400)
                self._check_device_admission(device)
                check(device['state']=='available','device_busy','Device is already allocated')
            else:
                check(type(_device_reservation) is TrustedDeviceReservation
                      and _device_reservation._issuer is self._device_reservation_issuer
                      and self._device_reservations.get(
                          _device_reservation.reservation_id) is _device_reservation
                      and _device_reservation.device_id==device_id
                      and _device_reservation.owner==owner
                      and device['state']=='reserved'
                      and device['sessionId']==_device_reservation.reservation_id,
                      'stale_controller','Device reservation is stale')
                if _release is not None:
                    check(_device_reservation.project_digest==_release['registration'].project_digest
                          and _device_reservation.application_id==_release['applicationId']
                          and _device_reservation.build_id==_release['buildId'],
                          'recording_identity','Device reservation selection changed',409)
                self._device_reservations.pop(_device_reservation.reservation_id,None)
            sid=_session_id or uuid.uuid4().hex
            check(sid not in self.sessions,'invalid_argument',
                  'Session identity is already in use',400)
            s={'id':sid,'deviceId':device_id,'owner':owner,'controllerId':client_id,'epoch':1,'lastSequence':0,'mode':'manual',
               'state':'connecting','lock':threading.RLock(),'frameLock':threading.RLock(),'frame':None,'frameSequence':0,
               'geometryVersion':0,'frameHistory':deque(maxlen=30),'frameTimes':deque(maxlen=30),'receipts':{},'recordingId':None,'replay':None,
               'createdAt':int(time.time()*1000),'createdMonotonic':self.clock(),'lastActivity':self.clock(),'events':[],
               'knownStart':False,'metrics':{'frames':0,'fps':0,'lastInputMs':0,'injected':0},'cancel':threading.Event(),
               'activePointers':{},'pointerLastActivity':{},'authoritySequence':0,
               'dispatchSequence':0,'inflightCommands':{},
               'providerIncarnation':startup_provider_incarnation or
               'provider_'+uuid.uuid4().hex,
               'releaseFrameSequence':0,'releaseObservationSequence':0,
               'releaseAcquisitionSequence':0,'inflightFrames':0,
               'inflightCollections':0,
               '_retainedDeviceScope':_device_scope,
               '_candidateIdentity':copy.deepcopy(_candidate_identity)
               if _device_scope is not None and _candidate_identity is not None else None,
               '_candidateProfile':copy.deepcopy(_candidate_profile)
               if _device_scope is not None and _candidate_profile is not None else None}
            if startup_binding is not None:s['_startupBinding']=startup_binding
            if _effect_authorizer is not None:s['effectAuthorizer']=_effect_authorizer
            s['frameCondition']=threading.Condition(s['frameLock'])
            s['releaseFrameCondition']=threading.Condition(s['lock'])
            if 'automaticAppLogs' in device['capabilities']:
                s['automaticAppLogs']=device['capabilities']['automaticAppLogs'] is True
            s['provider']=None;self.sessions[sid]=s;device.update(state='busy',sessionId=sid)
            self._persist_devices()
            self._event(s,'session_allocated')
        handle=(_device_reservation._authority_handle if _device_reservation is not None else
                _device_scope._reservation._authority_handle if _device_scope is not None else None)
        permit=None;factory_attempted=False
        try:
            self._authorize_effect(s,'startup')
            if _release is not None:
                try:
                    recorder=self._recording_store.begin_recording(
                        _release['registration'],recording_id=_release['recordingId'],
                        session_id='session_'+sid,application_id=_release['applicationId'],
                        build_id=_release['buildId'],
                        device_identity=(_candidate_identity if _device_scope is not None and
                                         _candidate_identity is not None else
                                         device.get('capabilities',{}).get('applicationIdentity')),
                        provider_incarnation=s['providerIncarnation'],
                        preparation_receipts=_release['preparation'],candidate_binding=_release.get('candidateBinding'))
                except Exception:
                    raise LiveError('recording_unavailable','Durable recording could not start') from None
                s.update(releaseRecorder=recorder,releaseRecordingId=_release['recordingId'],
                         releaseRegistration=_release['registration'],
                         releaseVideoSink=_release.get('frameSink'))
                if _release.get('preparationUnknown'):
                    recorder.declare_gap('preparation_unknown')
                if _release.get('frameSink') is not None:
                    try:recorder.attach_frame_sink(_release['frameSink'])
                    except Exception:
                        try:recorder.stop(reason='frame_sink_invalid')
                        except Exception:pass
                        try:_release['frameSink'].close()
                        except Exception:pass
                        raise LiveError('recording_unavailable','Recording frame sink is invalid') from None
                self.release_recording_owners[_release['recordingId']]=owner
            if device.get('_remoteAuthority') is True:
                selected_grant=authority_grant if authority_grant is not None else self.parent_grant
                check(self.authority is not None and (handle is not None or selected_grant is not None),
                      'authority_unavailable','Originating host authority is unavailable')
                s['_authorityGrant']=selected_grant
            if '_authority' in device:
                selected_grant=authority_grant if authority_grant is not None else self.parent_grant
                check(self.authority is not None
                      and (handle is not None or selected_grant is not None),
                      'authority_unavailable','Host authority is unavailable')
                trusted=device['_authority']
                provider_incarnation=s['providerIncarnation']
                if handle is None:
                    handle=self.authority.claim_device(
                        device_kind=trusted['deviceKind'],physical_id=trusted['physicalId'],
                        display_alias=device_id,helper_incarnation='helper_'+uuid.uuid4().hex,
                        parent_grant=selected_grant)
                handle.check_ownership()
                helper_incarnation=handle.helper_incarnation
            # Provider construction may probe a local transport. Shared mode
            # owns the canonical lock first; no Lab lock is held in either mode.
            factory_attempted=True
            created_provider=(_provider_factory() if _device_scope is not None and
                              _provider_factory is not None else device['factory']())
            if startup_binding is not None:
                check(created_provider is startup_binding.provider,
                      'native_protocol_mismatch','Retained startup provider binding changed',400)
            s['provider']=created_provider
            if device.get('_remoteAuthority') is True and handle is not None:
                binder=getattr(s['provider'],'bind_remote_reservation',None)
                check(callable(binder),'native_protocol_mismatch',
                      'Remote provider cannot transfer its physical reservation',400)
                binder(handle)
            if '_authority' in device:
                binder=getattr(s['provider'],'bind_authority',None)
                check(callable(binder),'native_protocol_mismatch',
                      'Provider does not support the authority protocol',400)
                binder(handle,provider_incarnation)
                s.update(deviceAuthority=handle,providerIncarnation=provider_incarnation,
                         helperIncarnation=helper_incarnation)
                if startup_binding is not None:
                    permit=self._prepare_effect(
                        s,'startup',startup_native_payload,
                        payload_digest=digest(startup_native_payload))
                else:
                    permit=self._prepare_effect(s,'startup',{
                        'kind':device['kind'],
                        'applicationIdentity':(_candidate_identity if _device_scope is not None and
                                               _candidate_identity is not None else
                                               device.get('capabilities',{}).get('applicationIdentity')),
                    })
                starter=getattr(s['provider'],'start_authorized',None)
                check(callable(starter),'native_protocol_mismatch',
                      'Provider does not support authorized startup',400)
                result=starter(s,self,permit)
                check(result is None or (isinstance(result,dict) and result.get('ok') is True),
                      'startup_unknown','Provider startup was not confirmed')
                self._confirm_effect(s,permit,'succeeded',result or {'ok':True},'startup')
            else:
                s['provider'].start(s,self)
        except Exception as error:
            release_recorder=s.get('releaseRecorder')
            if release_recorder is not None:
                try:release_recorder.stop(reason='provider_start_failed')
                except Exception:pass
            if s.get('provider') is None:
                with s['lock']:
                    clean=True
                    if handle is not None and _device_scope is None:
                        try:
                            clean=handle.status=='owned'
                            if clean:clean=handle.close()
                        except Exception:clean=False
                    message=('Host authority is unavailable'
                             if isinstance(error,LiveError) and error.code=='authority_unavailable'
                             else 'Provider construction was rejected' if factory_attempted
                             else 'Session setup was rejected before provider construction')
                    s.update(state='failed',error=message)
                    if clean and _device_scope is None:s['_startupReleaseConfirmed']=True
                    with self.lock:
                        if _device_scope is not None:
                            device.update(state='quarantined',
                                          sessionId=_device_scope.reservation_id)
                        else:
                            device.update(state='available' if clean else 'quarantined',sessionId=None)
                        self._persist_devices()
                        if clean and _device_reservation is not None:
                            s['_releasedDeviceReservation']=_device_reservation
                if factory_attempted:raise
                return self.get_session(sid,owner)
            clean_rejection=(handle is not None and
                (permit is None or (isinstance(error,LiveError) and error.code=='startup_rejected')))
            if clean_rejection:
                try:clean_rejection=handle.status=='owned'
                except Exception:clean_rejection=False
            if clean_rejection:
                if permit is not None:
                    try:self._confirm_effect(s,permit,'rejected',{'code':'native_protocol_mismatch'},'startup')
                    except Exception:clean_rejection=False
                if clean_rejection:
                    released=(handle.close() if _device_scope is None else False)
                    if _device_scope is None:s.pop('deviceAuthority',None)
                    s.update(state='failed',error='Provider version is incompatible')
                    with self.lock:
                        if _device_scope is not None:
                            device.update(state='quarantined',sessionId=_device_scope.reservation_id)
                        else:
                            device.update(state='available' if released else 'quarantined',sessionId=None)
                        self._persist_devices()
            if not clean_rejection:self.fail(sid,'Provider could not start')
        return self.get_session(sid,owner)

    @staticmethod
    def _operation_id(session_id, source):
        encoded=(session_id+'\0'+source).encode('utf-8')
        return 'operation_'+hashlib.sha256(encoded).hexdigest()[:40]

    @staticmethod
    def _parameterized_digest(kind,payload):
        value=copy.deepcopy(payload)
        if kind=='text' and isinstance(value,dict) and 'value' in value:
            value={'variable':'live-text'}
        return digest({'kind':kind,'payload':value})

    def _recording_input_for_dispatch(self,s,action,payload,frame,requested):
        try:requested=contracts.validate_input(requested)
        except ContractError:
            raise LiveError('recording_input_invalid',
                            'Release input is not a valid typed input',400) from None
        recorded_action={'long_press':'long-press'}.get(action,action)
        check(requested['action']==recorded_action,'recording_input_invalid',
              'Recorded input does not match dispatched input',400)
        if recorded_action in {'tap','long-press','swipe','pointer'}:
            result={'action':recorded_action,'parameters':{}}
            if recorded_action=='tap':
                result['parameters']={'x':payload['x'],'y':payload['y']}
            elif recorded_action=='long-press':
                result['parameters']={
                    'x':payload['x'],'y':payload['y'],
                    'durationMs':payload['durationMs'],
                }
            elif recorded_action=='swipe':
                result['parameters']={
                    'x':payload['fromX'],'y':payload['fromY'],
                    'x2':payload['toX'],'y2':payload['toY'],
                    'durationMs':payload['durationMs'],
                }
            else:
                result['parameters']={key:payload[key] for key in
                                      ('phase','pointerId','x','y')}
            if not (recorded_action=='pointer' and payload['phase']=='cancel'):
                check(isinstance(frame,dict),'recording_input_invalid',
                      'Coordinate input has no trusted frame geometry',400)
                geometry={
                    'width':frame['width'],'height':frame['height'],
                    'rotation':0 if frame['orientation']=='portrait' else 90,
                    'version':frame['geometryVersion'],
                }
                if isinstance(frame.get('objectDigest'),str):
                    geometry['frameDigest']=frame['objectDigest']
                result['geometry']=geometry
        elif recorded_action=='text':
            target=self.devices[s['deviceId']]['capabilities'].get(
                'recordingTextTarget')
            check(isinstance(target,dict) and requested.get('target')==target,
                  'recording_input_invalid',
                  'Provider has no trusted binding for this text target',400)
            result={
                'action':'text','target':copy.deepcopy(target),
                'parameters':{
                    'variableId':requested['parameters']['variableId'],
                },
            }
        elif recorded_action in {'home','back'}:
            result={'action':recorded_action,'parameters':{}}
        elif recorded_action=='rotate':
            result={'action':'rotate','parameters':{
                'orientation':payload['orientation'],
            }}
        elif recorded_action in {'launch','terminate'}:
            result={'action':recorded_action,'parameters':{
                'applicationId':payload['applicationId'],
            }}
        else:
            raise LiveError('recording_input_invalid',
                            'Live action has no original-evidence binding',400)
        try:return contracts.validate_input(result)
        except ContractError:
            raise LiveError('recording_input_invalid',
                            'Dispatched input cannot be recorded truthfully',400) from None

    def _prepare_effect(self,s,kind,payload,*,operation_id=None,payload_digest=None):
        handle=s.get('deviceAuthority')
        if handle is None:return None
        retained_scope=s.get('_retainedDeviceScope')
        if retained_scope is None:
            s['authoritySequence']+=1
            sequence=s['authoritySequence']
        else:
            with self.lock:
                self._require_retained_scope(retained_scope,owner=s['owner'],device_id=s['deviceId'])
                sequence=self._retained_scope_sequences[retained_scope.reservation_id]+1
                self._retained_scope_sequences[retained_scope.reservation_id]=sequence
            s['authoritySequence']=sequence
        identifier=operation_id or self._operation_id(
            s['id'],kind+'-'+str(sequence)
        )
        try:
            admission=handle.admit_operation(
                operation_id=identifier,
                payload_digest=(self._parameterized_digest(kind,payload)
                                if payload_digest is None else payload_digest),
                session_id='session_'+s['id'],sequence=sequence,
            )
            return handle.prepare_dispatch(
                admission,provider_incarnation=s['providerIncarnation']
            )
        except Exception:
            raise LiveError('authority_rejected','Device authority rejected the operation') from None

    def _confirm_effect(self,s,permit,status,result,kind):
        if permit is None:return
        bounded={'kind':kind,'status':status}
        if isinstance(result,dict):
            code=result.get('code')
            if isinstance(code,str) and re.fullmatch(r'[a-z_]{1,64}',code):bounded['code']=code
            timing=result.get('timing')
            if timing in {'best-effort','provider'}:bounded['timing']=timing
        result_digest=digest(bounded)
        receipt_id='receipt_'+hashlib.sha256(
            (permit.operation_id+'\0'+status+'\0'+result_digest).encode()
        ).hexdigest()[:40]
        try:
            s['deviceAuthority'].confirm_operation(
                permit,ProviderResult(receipt_id,status,result_digest)
            )
        except Exception:
            raise LiveError('injection_unknown','Authority receipt was not confirmed') from None

    def _provider_execute(self,s,action,payload,permit,frame=None,
                          operation_id=None):
        self._authorize_effect(s,action)
        if permit is not None:
            execute=getattr(s['provider'],'execute_authorized',None)
            check(callable(execute),'native_protocol_mismatch',
                  'Provider does not support authorized input',400)
            return execute(action,payload,permit,frame=frame)
        execute_operation=getattr(s['provider'],'execute_operation',None)
        if callable(execute_operation):
            if operation_id is None:
                with s['lock']:
                    s['dispatchSequence']+=1
                    operation_id=self._operation_id(
                        s['id'],'dispatch-'+str(s['dispatchSequence']))
            return execute_operation(
                action,payload,operation_id=operation_id,frame=frame)
        execute_with_frame=getattr(s['provider'],'execute_with_frame',None)
        if frame is not None and callable(execute_with_frame):return execute_with_frame(action,payload,frame)
        return s['provider'].execute(action,payload)
    @staticmethod
    def _authorize_effect(s,kind):
        authorizer=s.get('effectAuthorizer')
        if authorizer is None:return
        try:allowed=authorizer(kind)
        except LiveError:raise
        except Exception:
            raise LiveError('authorization_revoked',
                            'Operation authorization was revoked',403) from None
        check(allowed is True,'authorization_revoked',
              'Operation authorization was revoked',403)
    def _persist_devices(self):
        with self.lock:
            self.device_history.update({key:{'state':d['state'],'sessionId':d['sessionId']} for key,d in self.devices.items()})
            write_json(self.output/'device-state.json',self.device_history)
    def _event(self,s,kind,**details):
        entry={'id':len(s['events'])+1,'type':kind,'at':int(time.time()*1000),'epoch':s['epoch'],**details}
        s['events'].append(entry)
        write_json(self.output/'session-logs'/f"{s['id']}.json",{'owner':s['owner'],'sessionId':s['id'],'deviceId':s['deviceId'],'events':s['events']})
    def _expiry_reason(self,s):
        if self.clock()-s['createdMonotonic']>=self.max_session_seconds:return 'session_timeout'
        if self.clock()-s['lastActivity']>=self.idle_timeout:return 'idle_timeout'
        return None
    def _renew(self,s):
        if s['state'] in {'active','connecting'}:
            check(self._expiry_reason(s) is None,'session_expired','Session lease expired; close and reopen the device')
            s['lastActivity']=self.clock()
    def heartbeat(self,sid,owner,client_id):
        public_id(client_id);s=self._session(sid,owner)
        with s['lock']:
            check(s['state'] in {'active','connecting'},'session_inactive','Session is not active')
            self._authorize_effect(s,'session')
            self._renew(s);return self._public(s)
    def list_sessions(self,owner):
        with self.lock:values=[s for s in self.sessions.values() if s['owner']==owner]
        result=[]
        for s in values:
            with s['lock']:result.append(self._public(s))
        return sorted(result,key=lambda item:item['createdAt'],reverse=True)
    def observe(self,sid,owner,*,classification=None,acquisition_sequence=None):
        s=self._session(sid,owner)
        with s['lock']:
            self._authorize_effect(s,'observe')
            self._renew(s)
            recorder=s.get('releaseRecorder')
            if recorder is not None:
                policy=s['releaseRegistration'].project['evidencePolicy']
                check(policy.get('accessibility') is True and policy.get('text') is True,
                      'capture_suppressed','Semantic observation collection is disabled',403)
            observer=getattr(s['provider'],'observe',None)
            check(callable(observer),'unsupported_operation','This provider has no permitted semantic observation',400)
            if recorder is not None:s['inflightCollections']+=1
        failure_reason='observation_failed'
        try:
            self._authorize_effect(s,'observe')
            result=observer()
            self._authorize_effect(s,'observe')
            failure_reason='observation_invalid'
            with s['lock']:
                check(s['state']=='active' and not s.get('stopAdmission',False),
                      'session_inactive','Observation authority was revoked')
            check(isinstance(result,dict),'invalid_observation','Provider observation is invalid')
            if recorder is not None:
                with s['lock']:
                    if acquisition_sequence is None:
                        s['releaseObservationSequence']+=1
                        acquisition_sequence=s['releaseObservationSequence']
                    elif type(acquisition_sequence) is int:
                        s['releaseObservationSequence']=max(
                            s['releaseObservationSequence'],acquisition_sequence)
                failure_reason=None
                try:
                    stored=recorder.record_observation(
                        'accessibility',result,acquisition_sequence=acquisition_sequence,
                        classification=classification)
                except Exception:
                    raise LiveError('capture_suppressed',
                                    'Observation was not durably collected') from None
                check(stored is not None,'capture_suppressed',
                      'Observation sample was suppressed',403)
            return result
        except Exception:
            if recorder is not None and failure_reason is not None:
                try:recorder.declare_gap(failure_reason)
                except Exception:pass
            raise
        finally:
            if recorder is not None:
                with s['lock']:s['inflightCollections']-=1

    def resolve_locator(self,sid,owner,target):
        """Resolve an authored locator through the active trusted provider.

        The returned evidence describes this replay-time observation only; it
        never changes what the original recording observed.
        """
        try:
            checked=contracts.validate_input(
                {'action':'tap','target':copy.deepcopy(target),'parameters':{}})
        except Exception:
            raise LiveError('invalid_argument','Locator is invalid',400) from None
        target=checked['target'];s=self._session(sid,owner)
        with s['lock']:
            self._authorize_effect(s,'locator')
            self._renew(s)
            check(s['state']=='active' and not s.get('stopAdmission',False),
                  'session_inactive','Session is not active')
            capabilities=self.devices[s['deviceId']]['capabilities']
            kinds=capabilities.get('locatorKinds',[])
            check(isinstance(kinds,list) and target['kind'] in kinds,
                  'unsupported_operation','Provider does not support this locator',400)
            resolver=getattr(s['provider'],'resolve_locator',None)
            check(callable(resolver),'unsupported_operation',
                  'Provider does not support locator resolution',400)
            provider_incarnation=s['providerIncarnation']
            recorder=s.get('releaseRecorder')
            if recorder is not None:
                policy=s['releaseRegistration'].project['evidencePolicy']
                check(policy.get('accessibility') is True and policy.get('text') is True,
                      'capture_suppressed','Locator collection is disabled',403)
            handle=s.get('deviceAuthority')
            if handle is not None:
                try:handle.check_ownership()
                except Exception:
                    raise LiveError('authority_unavailable','Locator authority is unavailable') from None
            if recorder is not None:s['inflightCollections']+=1
        started_at_ms=int(time.time()*1000)
        try:
            self._authorize_effect(s,'locator')
            try:result=resolver(copy.deepcopy(target))
            except Exception:
                raise LiveError('observation_failed','Locator observation failed') from None
            self._authorize_effect(s,'locator')
            with s['lock']:
                check(s['state']=='active' and not s.get('stopAdmission',False)
                      and self._expiry_reason(s) is None,
                      'session_inactive','Locator authority was revoked')
                if handle is not None:
                    try:handle.check_ownership()
                    except Exception:
                        raise LiveError('authority_unavailable','Locator authority is unavailable') from None
                with s['frameLock']:
                    frame=s['frame']
                    check(isinstance(frame,dict),'frame_pending','No frame is available',503)
                    expected={'target','x','y','frameId','geometryVersion',
                              'providerIncarnation','observedAtMs'}
                    check(isinstance(result,dict) and set(result)==expected
                          and result['target']==target
                          and type(result['x']) in (int,float)
                          and not isinstance(result['x'],bool)
                          and type(result['y']) in (int,float)
                          and not isinstance(result['y'],bool)
                          and math.isfinite(result['x']) and math.isfinite(result['y'])
                          and 0<=result['x']<=1 and 0<=result['y']<=1
                          and result['frameId']==frame['id']
                          and result['geometryVersion']==frame['geometryVersion']
                          and result['providerIncarnation']==provider_incarnation
                          and type(result['observedAtMs']) is int
                          and started_at_ms<=result['observedAtMs']<=int(time.time()*1000),
                          'observation_failed','Locator observation is invalid')
                    return copy.deepcopy(result)
        finally:
            if recorder is not None:
                with s['lock']:s['inflightCollections']-=1

    def release_binding(self,sid,owner):
        """Return the immutable public selection for a release session."""
        s=self._session(sid,owner)
        with s['lock']:
            registration=s.get('releaseRegistration')
            check(registration is not None,'recording_unavailable',
                  'Session has no durable release recording')
            project=registration.project
            identity=copy.deepcopy(s.get('_candidateIdentity') or
                                   self.devices[s['deviceId']]['capabilities'].get('applicationIdentity'))
            recording=s['releaseRecorder']._row()
            check(isinstance(identity,dict) and digest(identity)==recording['device_identity_digest'],
                  'recording_identity','Release application identity changed',409)
            return {'projectId':project['id'],'projectRevision':project['revision'],
                    'projectDigest':registration.project_digest,
                    'applicationId':recording['application_id'],
                    'buildId':recording['build_id'],
                    'artifactDigest':identity['artifactDigest'],
                    'recordingId':s['releaseRecordingId'],
                    'providerIncarnation':s['providerIncarnation']}

    def record_registered_observation(self,sid,owner,value):
        """Persist a G4 registry-produced observation before the stop barrier."""
        s=self._session(sid,owner)
        with s['lock']:
            self._renew(s)
            recorder=s.get('releaseRecorder')
            check(recorder is not None and s['state']=='active'
                  and not s.get('stopAdmission',False),
                  'recording_unavailable','Release observation admission is closed')
            policy=s['releaseRegistration'].project['evidencePolicy']
            check(policy.get('accessibility') is True and policy.get('text') is True,
                  'capture_suppressed','Semantic observation collection is disabled',403)
            s['releaseObservationSequence']+=1
            sequence=s['releaseObservationSequence']
            s['inflightCollections']+=1
        try:
            stored=recorder.record_observation(
                'accessibility',copy.deepcopy(value),acquisition_sequence=sequence)
            check(stored is not None,'capture_suppressed',
                  'Observation sample was suppressed',403)
            return copy.deepcopy(stored)
        except LiveError:raise
        except Exception:
            raise LiveError('recording_unavailable',
                            'Observation was not durably collected') from None
        finally:
            with s['lock']:s['inflightCollections']-=1
    def session_events(self,sid,owner):
        s=self._session(sid,owner)
        with s['lock']:return copy.deepcopy(s['events'])
    def reap_expired(self):
        with self.lock:values=list(self.sessions.values())
        for s in values:
            pointer_timeout=False
            with s['lock']:
                if s.get('releaseRecorder') is not None and 'appLog' in s and not self._app_log_retained(s):
                    s.pop('appLog', None)
                if s['state'] in {'active','connecting'} and s.get('pointerLastActivity'):
                    now=self.clock();pointer_timeout=any(now-last>=10 for last in s['pointerLastActivity'].values())
                reason=self._expiry_reason(s) if s['state'] in {'active','connecting'} else None
                if pointer_timeout:reason='pointer_timeout'
                if reason:s['closeReason']=reason
            if pointer_timeout:
                try:self._cancel_active_pointers(s)
                except LiveError:continue
                with s['lock']:
                    self._invalidate_recording(s,'pointer_timeout');s['epoch']+=1;s['lastSequence']=0;s['mode']='manual'
                    self._event(s,'pointer_timeout')
                continue
            if reason:self.close_session(s['id'],s['owner'])
    def start_maintenance(self):
        if self.maintenance_thread is not None:return
        def run():
            while not self.maintenance_stop.wait(1):self.reap_expired()
        self.maintenance_thread=threading.Thread(target=run,daemon=True);self.maintenance_thread.start()
    def stop_maintenance(self):
        self.maintenance_stop.set()
        if self.maintenance_thread is not None:self.maintenance_thread.join(timeout=60)
    def recover_device(self,device_id):
        with self.lock:
            device=self.devices.get(device_id);check(device is not None,'not_found','Device not found',404)
            check(device['state']=='quarantined' and device['sessionId'] is None,'recovery_unavailable','Close the owning session before recovery')
            check(device['capabilities'].get('recovery') is True,'unsupported_operation','Provider has no verified recovery contract',400)
            device['state']='recovering';self._persist_devices()
        try:
            provider=device['factory']();check(provider.recover().get('ok') is True,'cleanup_uncertain','Device recovery was not confirmed')
        except Exception:
            with self.lock:device['state']='quarantined';self._persist_devices()
            raise LiveError('recovery_failed','Device recovery failed; quarantine retained') from None
        with self.lock:
            device.update(state='available',sessionId=None);device.pop('quarantineReason',None);self._persist_devices()
        return next(value for value in self.list_devices() if value['id']==device_id)
    def classify_recording_sample(self,sid,*,kind,body,native_incarnation,
                                  acquisition_sequence,sample_id,decision):
        s=self._session(sid)
        with s['lock']:
            registration=s.get('releaseRegistration')
            check(registration is not None,'recording_unavailable',
                  'Session has no trusted recording policy')
            try:return registration.classify_sample(
                kind=kind,sample_id=sample_id,
                sample_digest=hashlib.sha256(body).hexdigest(),
                provider_incarnation=s['providerIncarnation'],
                native_incarnation=native_incarnation,
                acquisition_sequence=acquisition_sequence,decision=decision)
            except Exception:raise LiveError('capture_suppressed','Sample classification is invalid',400) from None
    def bind_recording_provider_clock(self,sid,mapping,*,provider_boot_digest,
                                      native_incarnation):
        s=self._session(sid)
        recorder=s.get('releaseRecorder')
        check(recorder is not None,'recording_unavailable',
              'Session has no durable recording clock')
        try:return recorder.anchor.bind_provider(
            mapping,provider_boot_digest=provider_boot_digest,
            native_incarnation=native_incarnation)
        except Exception:raise LiveError('clock_mapping_invalid',
                                        'Provider clock mapping is invalid',400) from None
    def declare_recording_gap(self,sid,reason,*,invalidate_provider_mappings=False):
        s=self._session(sid)
        recorder=s.get('releaseRecorder')
        check(recorder is not None,'recording_unavailable',
              'Session has no durable recording')
        try:recorder.declare_gap(
            reason,invalidate_provider_mappings=invalidate_provider_mappings)
        except Exception:raise LiveError('recording_unavailable',
                                        'Recording gap could not be persisted') from None
    def publish_frame(self,sid,data,mime,width,height,orientation='portrait',captured_at=None,*,
                      acquisition_sequence=None,classification=None,
                      provider_clock_binding=None,provider_monotonic_ns=None,
                      provider_capture_start_ns=None,
                      native_incarnation=None,native_sequence_gap=None,timing_source='host-acquired'):
        s=self._session(sid)
        check(mime in {'image/jpeg','image/png','image/svg+xml'} and isinstance(data,bytes) and len(data)<=3*1024*1024,
              'invalid_frame','Invalid frame',400)
        check(type(width) is int and type(height) is int and 0<width<=8192 and 0<height<=8192,'invalid_frame','Invalid frame dimensions',400)
        recorder=s.get('releaseRecorder')
        publication=None
        if recorder is not None:
            if provider_clock_binding is not None:
                check(timing_source in {'host-acquired','provider-mapped'},
                      'invalid_frame','Invalid provider timing source',400)
                timing_source='provider-mapped'
            check(timing_source in {
                'host-acquired','provider-mapped','native-unmapped'
            },'invalid_frame','Invalid frame timing source',400)
            with s['lock']:
                if s.get('stopAdmission',False):return False
                if acquisition_sequence is None:
                    s['releaseAcquisitionSequence']+=1
                    acquisition_sequence=s['releaseAcquisitionSequence']
                elif type(acquisition_sequence) is int:
                    s['releaseAcquisitionSequence']=max(
                        s['releaseAcquisitionSequence'],acquisition_sequence)
                s['inflightFrames']+=1
            try:
                if timing_source=='native-unmapped':
                    recorder.declare_gap('native_timing_unknown')
                publication=recorder.record_frame(
                    data,mime,width,height,orientation,
                    acquisition_sequence=acquisition_sequence,
                    classification=classification,
                    provider_clock_binding=provider_clock_binding,
                    provider_monotonic_ns=provider_monotonic_ns,
                    provider_capture_start_ns=provider_capture_start_ns,
                    native_sequence_gap=native_sequence_gap,
                    native_incarnation=(native_incarnation or
                                        ('native_unmapped'
                                         if timing_source=='native-unmapped' else None)),
                    timing_source=timing_source)
            except Exception:
                with s['lock']:
                    s['inflightFrames']-=1
                    s['releaseFramePublicationFailed']=True
                    s['releaseFrameCondition'].notify_all()
                    stopping=s.get('stopAdmission',False) or s['state'] in {
                        'draining','closed'
                    }
                if stopping:return False
                self.fail(sid,'Durable frame publication failed')
                return False
            with s['lock']:
                s['inflightFrames']-=1
                s['releaseFrameCondition'].notify_all()
                if s.get('stopAdmission',False):return False
            if publication is None:return False
            captured_at=publication.stamp.display_ms
        with s['frameCondition']:
            if s['state'] in {'draining','closed'}:return
            previous=s['frame']
            changed=previous and (previous['width'],previous['height'],previous['orientation'])!=(width,height,orientation)
            if previous is None or changed:s['geometryVersion']+=1
            s['frameSequence']+=1;now=time.monotonic();s['frameTimes'].append(now)
            times=s['frameTimes'];fps=(len(times)-1)/(times[-1]-times[0]) if len(times)>1 and times[-1]>times[0] else 0
            s['frame']={'id':s['frameSequence'],'geometryVersion':s['geometryVersion'],'width':width,'height':height,
                        'orientation':orientation,
                        'capturedAt':(captured_at if captured_at is not None
                                      else int(time.time()*1000)),
                        'mime':mime,'bytes':data,'_received':now}
            if publication is not None:
                s['releaseFrameSequence']=acquisition_sequence
                s['frame'].update(objectDigest=publication.digest,
                                  acquisitionSequence=acquisition_sequence,
                                  presentationOffsetMs=publication.stamp.offset_ms,
                                  clockUncertaintyNs=publication.stamp.uncertainty_ns,
                                  captureTimingSource=publication.timing_source)
                if publication.stamp.provider_clock_id is not None:
                    s['frame'].update(providerClockId=publication.stamp.provider_clock_id,
                                      providerMonotonicNs=provider_monotonic_ns,
                                      nativeIncarnation=publication.stamp.native_incarnation)
            s['frameHistory'].append((s['frameSequence'],now,s['geometryVersion']))
            s['metrics'].update(frames=s['frameSequence'],fps=round(fps,2));s['frameCondition'].notify_all()
        # Do not hold the media lock while waiting for the input/control lock.
        if s['state']=='connecting':s['state']='active';s['knownStart']=False
        if changed:s['geometryChanged']=True
        return True
    def frame(self,sid,owner=None):
        s=self._session(sid,owner)
        with s['frameLock']:
            check(s['frame'] is not None,'frame_pending','Waiting for a provider frame',503)
            value={k:v for k,v in s['frame'].items() if k not in {'bytes','_received'}}
            value['imageBase64']=base64.b64encode(s['frame']['bytes']).decode();return value
    def _frame_meta(self,s):
        with s['frameLock']:
            check(s['frame'] is not None,'frame_pending','No frame is available',503)
            return {k:v for k,v in s['frame'].items() if k not in {'bytes','_received'}}
    def _control(self,s,controller,epoch,*,allow_recording_end=False):
        check(s['state']=='active','session_inactive','Session is not active')
        check(not s.get('stopAdmission',False),'session_inactive','Session is stopping')
        recorder=s.get('releaseRecorder')
        check(allow_recording_end or recorder is None or not recorder.duration_reached(),
              'recording_duration_reached','The recording duration has ended',409)
        self._authorize_effect(s,'control')
        self._renew(s)
        check(controller==s['controllerId'] and type(epoch) is int and epoch==s['epoch'],
              'stale_controller','Control has moved to another controller')
    def _pointer_capable(self,s):
        capabilities=self.devices[s['deviceId']]['capabilities']
        check('pointer' in capabilities.get('actions',[]) and capabilities.get('inputMode')=='continuous-pointer'
              and type(capabilities.get('maxPointers')) is int and capabilities.get('maxPointers')==5,
              'unsupported_operation','Provider does not support continuous pointers',400)
    def _pointer_transition(self,s,payload,geometry_version):
        phase=payload['phase'];pointer_id=payload['pointerId'];active=s['activePointers']
        if phase=='down':
            check(pointer_id not in active,'pointer_phase','Pointer is already down')
            check(len(active)<5,'pointer_limit','Maximum active pointers reached')
        elif phase in {'move','up'}:
            check(pointer_id in active,'pointer_phase','Pointer is not down')
            check(active[pointer_id]['geometryVersion']==geometry_version,
                  'stale_geometry','Screen geometry changed during a pointer gesture')
    def _apply_pointer_transition(self,s,payload,geometry_version):
        phase=payload['phase'];pointer_id=payload['pointerId']
        if phase=='down' or phase=='move':
            s['activePointers'][pointer_id]={'x':payload['x'],'y':payload['y'],'geometryVersion':geometry_version}
        elif phase=='up':
            s['activePointers'].pop(pointer_id,None);s['pointerLastActivity'].pop(pointer_id,None)
        else:  # A native cancel is a session-wide safety fence.
            s['activePointers'].clear();s['pointerLastActivity'].clear()
            return
        activity=self.clock()
        for active_id in s['activePointers']:
            s['pointerLastActivity'][active_id]=activity
    def _pointer_cancel_failure(self,s):
        s.update(state='failed',error='Pointer cancellation was not confirmed')
        s['epoch']+=1;s['cancel'].set();self._invalidate_recording(s,'pointer_cancel_unknown')
        with self.lock:
            self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
    def _cancel_active_pointers(self,s):
        with s['lock']:
            if not s.get('activePointers'):return
            payload={'phase':'cancel','pointerId':0,'x':0,'y':0}
            permit=self._prepare_effect(s,'pointer_cleanup',payload)
        try:
            result=self._provider_execute(s,'pointer',payload,permit)
            check(isinstance(result,dict) and result.get('ok') is True,
                  'pointer_cancel_unknown','Pointer cancellation was not confirmed')
            self._confirm_effect(s,permit,'succeeded',result,'pointer_cleanup')
        except Exception:
            with s['lock']:self._pointer_cancel_failure(s)
            raise LiveError('pointer_cancel_unknown','Pointer cancellation was not confirmed') from None
        with s['lock']:
            s['activePointers'].clear();s['pointerLastActivity'].clear();s['lastActivity']=self.clock()
    def claim(self,sid,owner,client_id,expected_epoch,mode='manual'):
        public_id(client_id);check(mode in {'manual','automation'},'invalid_argument','Invalid control mode',400)
        s=self._session(sid,owner)
        with s['lock']:
            self._authorize_effect(s,'control')
            check(s['state']=='active','session_inactive','Session is not active')
            check(not s.get('stopAdmission',False),'session_inactive','Session is stopping')
            check(not s['inflightCommands'],'injection_pending',
                  'An input outcome is still pending')
            self._renew(s)
            check(type(expected_epoch) is int and expected_epoch==s['epoch'],'stale_controller','Refresh the control epoch')
        self._cancel_active_pointers(s)
        with s['lock']:
            check(s['state']=='active' and expected_epoch==s['epoch'],
                  'stale_controller','Refresh the control epoch')
            check(not s.get('stopAdmission',False),'session_inactive','Session is stopping')
            check(not s['inflightCommands'],'injection_pending',
                  'An input outcome is still pending')
            s['cancel'].set();self._invalidate_recording(s,'controller_changed')
            s.update(controllerId=client_id,epoch=s['epoch']+1,lastSequence=0,mode=mode)
            self._event(s,'controller_changed',controllerId=client_id,mode=mode)
            return self._public(s)
    def input(self,sid,owner,command,*,recording_input=None):
        s=self._session(sid,owner)
        with s['lock']:
            check(isinstance(command,dict) and set(command)=={'controllerId','epoch','sequence','commandId','frameId','geometryVersion','action','payload'},
                  'invalid_argument','Invalid input envelope',400)
            self._control(s,command['controllerId'],command['epoch'])
            cid=public_id(command['commandId']);fingerprint=digest(command)
            if cid in s['receipts']:
                old_hash,receipt=s['receipts'][cid];check(old_hash==fingerprint,'idempotency_conflict','Command ID was reused for different input')
                if receipt.get('status')=='rejected':raise LiveError('input_rejected','Input was rejected: '+receipt['errorCode'],400)
                return copy.deepcopy(receipt)
            if cid in s['inflightCommands']:
                check(s['inflightCommands'][cid]==fingerprint,'idempotency_conflict',
                      'Command ID was reused for different input')
                raise LiveError('injection_pending','Input outcome is still pending')
            seq=command['sequence'];check(type(seq) is int and seq>s['lastSequence'],'stale_sequence','Input sequence is not newer')
            action=command['action'];validate_gesture(action,command['payload'])
            if action=='pointer':
                self._pointer_capable(s);self._pointer_transition(s,command['payload'],command['geometryVersion'])
            else:
                check(not s['activePointers'],'pointer_active','Finish or cancel active pointers before another input')
                check(action in self.devices[s['deviceId']]['capabilities']['actions'],'unsupported_operation','Provider does not support this action',400)
            cancel_pointer=action=='pointer' and command['payload']['phase']=='cancel'
            recording_frame=None
            if not cancel_pointer:
                with s['frameLock']:
                    frame=s['frame'];check(frame is not None,'frame_pending','No frame is available',503)
                    check(command['geometryVersion']==frame['geometryVersion'],'stale_geometry','Screen geometry changed; retry from a current frame')
                    check(type(command['frameId']) is int and 0<command['frameId']<=frame['id'] and frame['id']-command['frameId']<=20,
                          'stale_frame','Input is based on an obsolete frame')
                    captured=next((item for item in s['frameHistory'] if item[0]==command['frameId']),None)
                    check(captured is not None and time.monotonic()-captured[1]<=10 and captured[2]==command['geometryVersion'],'stale_frame','Displayed frame is stale')
                    recording_frame=copy.deepcopy(frame)
            started=time.monotonic()
            receipt={'id':cid,'sequence':seq,'action':action,'status':'unknown','epoch':s['epoch'],'frameId':command['frameId']}
            operation_id=(cid if re.fullmatch(r'operation_[0-9a-f]{40}',cid)
                          else self._operation_id(s['id'],'input-'+cid))
            recorder=s.get('releaseRecorder')
            durable_input=None
            if recorder is not None:
                check(isinstance(recording_input,dict),'recording_input_required',
                      'Release input requires a parameterized typed input',400)
                durable_input=self._recording_input_for_dispatch(
                    s,action,command['payload'],recording_frame,recording_input)
            s['lastSequence']=seq
            permit=self._prepare_effect(s,action,command['payload'],operation_id=operation_id)
            if recorder is not None:
                generation=(permit.ownership_generation if permit is not None else 1)
                provider_incarnation=(permit.provider_incarnation if permit is not None
                                      else s['providerIncarnation'])
                try:recorder.admit_input(operation_id,generation,provider_incarnation,
                                         durable_input)
                except Exception as error:
                    from .recording_session import RecordingDurationError
                    if isinstance(error,RecordingDurationError):
                        self._confirm_effect(s,permit,'rejected',
                            {'code':'recording_duration_reached'},action)
                        raise LiveError('recording_duration_reached',
                                        'The recording duration has ended',409) from None
                    handle=s.get('deviceAuthority')
                    if handle is not None:
                        try:handle.revoke_dispatches()
                        except Exception:pass
                    try:recorder.stop(reason='admission_journal_failed')
                    except Exception:pass
                    s.update(state='failed',error='Input admission could not be recorded',
                             epoch=s['epoch']+1);s['cancel'].set()
                    with self.lock:
                        self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
                    raise LiveError('recording_unavailable',s['error']) from None
                try:recorder.mark_dispatched(operation_id)
                except Exception:
                    handle=s.get('deviceAuthority')
                    if handle is not None:
                        try:handle.revoke_dispatches()
                        except Exception:pass
                    try:recorder.stop(reason='dispatch_journal_failed')
                    except Exception:pass
                    s.update(state='failed',error='Input dispatch could not be recorded',
                             epoch=s['epoch']+1);s['cancel'].set()
                    with self.lock:
                        self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
                    raise LiveError('recording_unavailable',s['error']) from None
            s['inflightCommands'][cid]=fingerprint
            payload=copy.deepcopy(command['payload'])
            frame_context={'frameId':command['frameId'],'geometryVersion':command['geometryVersion']}
        try:
            result=self._provider_execute(
                s,action,payload,permit,frame=frame_context,
                operation_id=operation_id)
            if isinstance(result,dict) and result.get('ok') is False and result.get('outcome')=='rejected':
                code=result.get('code','input_rejected')
                if not isinstance(code,str) or not re.fullmatch('[a-z_]{1,64}',code):code='input_rejected'
                self._confirm_effect(s,permit,'rejected',result,action)
                if recorder is not None:recorder.record_receipt(operation_id,'rejected',error_code=code)
                with s['lock']:
                    check(s['state']=='active' and not s.get('stopAdmission',False),
                          'session_inactive','Input authority was revoked')
                    receipt.update(status='rejected',errorCode=code,durationMs=round((time.monotonic()-started)*1000))
                    s['receipts'][cid]=(fingerprint,copy.deepcopy(receipt))
                    if len(s['receipts'])>2000:s['receipts'].pop(next(iter(s['receipts'])))
                raise _KnownInputRejection('input_rejected','Input was rejected: '+code,400)
            check(isinstance(result,dict) and result.get('ok') is True,'injection_failed','Provider did not confirm input injection')
            self._confirm_effect(s,permit,'succeeded',result,action)
            if recorder is not None:recorder.record_receipt(operation_id,'injected')
            with s['lock']:
                check(s['state']=='active' and not s.get('stopAdmission',False),
                      'session_inactive','Input authority was revoked')
                s['lastActivity']=self.clock()
                receipt.update(status='injected',durationMs=round((time.monotonic()-started)*1000),timing=result.get('timing','best-effort'))
                s['metrics'].update(lastInputMs=receipt['durationMs'],injected=s['metrics']['injected']+1)
                if s.pop('geometryChanged',False):self._invalidate_recording(s,'geometry_changed')
                if action=='pointer':self._apply_pointer_transition(s,command['payload'],command['geometryVersion'])
                self._record_input(s,command,receipt,started);s['knownStart']=action=='reset' and self.devices[s['deviceId']]['capabilities'].get('resetContract')!='app-relaunch-only'
                if action=='reset':s.pop('appLog',None);s.pop('appLogError',None)
                s['receipts'][cid]=(fingerprint,copy.deepcopy(receipt))
                if len(s['receipts'])>2000:s['receipts'].pop(next(iter(s['receipts'])))
                return receipt
        except _KnownInputRejection:
            raise
        except Exception:
            with s['lock']:
                self._invalidate_recording(s,'input_outcome_unknown');s['state']='failed';s['error']='Input result is uncertain; close the session before reuse'
                s['epoch']+=1;s['cancel'].set()
                with self.lock:self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
                s['receipts'][cid]=(fingerprint,copy.deepcopy(receipt))
            raise LiveError('injection_unknown',s['error']) from None
        finally:
            with s['lock']:s['inflightCommands'].pop(cid,None)
    def _record_input(self,s,command,receipt,started):
        rid=s.get('recordingId')
        if not rid:return
        r=self.recordings[rid]['data']
        if r['status']!='recording':return
        if len(r['events'])>=500 or time.monotonic()-r['_started']>600:
            self._invalidate_recording(s,'recording_limit');return
        payload=copy.deepcopy(command['payload'])
        if command['action']=='text':
            variable=f'text_{len(r["variables"])+1}';r['variables'].append(variable);payload={'variable':variable}
        r['events'].append({'id':f'e{len(r["events"])+1}','action':command['action'],'payload':payload,
                            'status':receipt['status'],'offsetMs':round((started-r['_started'])*1000),
                            'sourceCommandId':receipt['id'],'frameId':command['frameId'],'geometryVersion':command['geometryVersion'],
                            'controllerMode':s['mode'],'timing':receipt['timing']})
    def _freeze(self,entry,reason=None):
        r=entry['data'];r.pop('_started',None);r['status']='invalid' if reason else 'complete'
        if not r['events']:r['replayable']=False;r['reason']='empty_recording'
        if reason:r['replayable']=False;r['reason']=reason
        r['endedAt']=int(time.time()*1000);r['digest']=digest(r)
        write_json(self.output/'recordings'/f'{r["id"]}.json',{'owner':entry['owner'],'recording':r})
    def _invalidate_recording(self,s,reason):
        rid=s.get('recordingId')
        if rid and self.recordings[rid]['data']['status']=='recording':self._freeze(self.recordings[rid],reason)
    def _reset(self,s):
        with s['lock']:
            check(not s['activePointers'],'pointer_active','Finish or cancel active pointers before reset')
            s['knownStart']=False;before=s['frameSequence']
            permit=self._prepare_effect(s,'reset',{})
        try:
            result=self._provider_execute(s,'reset',{},permit)
            with s['lock']:
                check(s['state']=='active' and not s.get('stopAdmission',False),
                      'session_inactive','Reset authority was revoked')
            if isinstance(result,dict) and result.get('ok') is False and result.get('outcome')=='rejected':
                self._confirm_effect(s,permit,'rejected',result,'reset')
                raise _KnownInputRejection('reset_rejected','Provider rejected reset',409)
            check(isinstance(result,dict) and result.get('ok') is True,'reset_failed','Provider reset was not acknowledged')
            self._confirm_effect(s,permit,'succeeded',result,'reset')
            with s['frameCondition']:
                check(s['frameCondition'].wait_for(lambda:s['frameSequence']>before,timeout=5),'frame_pending','Reset frame missing')
        except _KnownInputRejection:
            raise
        except Exception:
            self.fail(s['id'],'Reset result is uncertain; close the session before reuse')
            raise LiveError('reset_unknown','Reset result is uncertain; close the session before reuse') from None
        with s['lock']:
            s['knownStart']=self.devices[s['deviceId']]['capabilities'].get('resetContract')!='app-relaunch-only'
            s.pop('appLog',None);s.pop('appLogError',None)
    def start_recording(self,sid,owner,controller,epoch,reset=False):
        s=self._session(sid,owner)
        with s['lock']:
            self._control(s,controller,epoch);check(s['mode']!='replay','replay_busy','Stop replay before recording')
            previous=s.get('recordingId')
            check(not previous or self.recordings[previous]['data']['status']!='recording','recording_busy','Recording already active')
            if reset:
                check('reset' in self.devices[s['deviceId']]['capabilities']['actions'],'unsupported_operation','Provider has no reset contract')
        if reset:self._reset(s)
        with s['lock']:
            self._control(s,controller,epoch)
            s.pop('geometryChanged',None)
            rid=uuid.uuid4().hex;frame=self._frame_meta(s)
            r={'schemaVersion':1,'kind':'live-gesture-recording','id':rid,'sessionId':sid,'deviceId':s['deviceId'],
               'applicationIdentity':copy.deepcopy(self.devices[s['deviceId']]['capabilities'].get('applicationIdentity')),
               'providerKind':self.devices[s['deviceId']]['kind'],'status':'recording','replayable':bool(s['knownStart']),
               'startingState':{'kind':'provider-reset' if s['knownStart'] else 'unknown'},
               'geometry':{k:frame[k] for k in ('width','height','orientation')},'events':[],'variables':[],
               'media':'frame-references-only','startedAt':int(time.time()*1000),'_started':time.monotonic()}
            self.recordings[rid]={'owner':owner,'data':r,'lock':s['lock']};s['recordingId']=rid;return self.recording(rid,owner)
    def stop_recording(self,sid,owner,controller,epoch):
        s=self._session(sid,owner)
        with s['lock']:
            self._control(s,controller,epoch);rid=s.get('recordingId')
            check(rid is not None and self.recordings[rid]['data']['status']=='recording','not_recording','No active recording')
            pointer_reason=bool(s['activePointers'])
        self._cancel_active_pointers(s)
        with s['lock']:
            self._control(s,controller,epoch)
            reason='pointer_cancelled' if pointer_reason else ('geometry_changed' if s.pop('geometryChanged',False) else None)
            self._freeze(self.recordings[rid],reason)
        self._save_app_logs_if_available(s)
        return self.recording(rid,owner)
    def begin_release_stop(self,sid,owner,controller,epoch,*,cleanup_only=False):
        s=self._session(sid,owner)
        with s['lock']:
            check(type(cleanup_only) is bool,'invalid_argument','Invalid stop mode',400)
            if cleanup_only:
                # A trusted issue handle may always reduce admission and
                # finalize already captured bytes after operation revocation.
                check(s['state'] in {'active','failed','closed'}
                      and controller==s['controllerId'] and type(epoch) is int and epoch==s['epoch'],
                      'stale_controller','Stop controller changed')
            else:self._control(s,controller,epoch,allow_recording_end=True)
            recorder=s.get('releaseRecorder')
            check(recorder is not None,'not_recording','No durable release recording exists')
            if s['state']=='closed':
                frozen=self.release_recording(recorder.recording_id,owner)
                check(s.get('stopAdmission') is True
                      and frozen['status'] in {'frozen-complete','frozen-incomplete'},
                      'recording_unavailable','Closed recording has no frozen stop barrier')
                return {'recordingId':recorder.recording_id,'status':'finalizing'}
            # Exclude new Lab admissions immediately. A publication admitted
            # before this flag may finish its durable source write before the
            # G2 barrier; a stalled publication remains an explicit gap.
            s['stopAdmission']=True
            s['releaseFrameCondition'].wait_for(lambda: s['inflightFrames']==0, timeout=1)
            reason=('pointer_active_stop' if s.get('activePointers') else
                    'frame_publication_failed' if s.get('releaseFramePublicationFailed') else
                    'frame_inflight_stop' if s.get('inflightFrames') else
                    'collection_inflight_stop' if s.get('inflightCollections') else None)
            # The stop response follows the durable barrier. The condition
            # wait above releases the lock only after admission was closed.
            recorder.stop_barrier(reason=reason)
        return {'recordingId':recorder.recording_id,'status':'finalizing'}
    def finalize_release_recording(self,sid,owner,controller,epoch):
        s=self._session(sid,owner)
        with s['lock']:
            check(s.get('stopAdmission') is True and controller==s['controllerId']
                  and type(epoch) is int and epoch==s['epoch'],
                  'stale_controller','Finalization requires the admitted stop controller')
            recorder=s.get('releaseRecorder')
            check(recorder is not None,'not_recording','No durable release recording exists')
            if s['state']=='closed':
                frozen=self.release_recording(recorder.recording_id,owner)
                check(frozen['status'] in {'frozen-complete','frozen-incomplete'},
                      'recording_unavailable','Closed recording has no frozen stop barrier')
                return frozen
        try:return recorder.freeze()
        except Exception:
            self.fail(sid,'Durable recording finalization failed')
            raise LiveError('recording_unavailable','Durable recording finalization failed') from None
    def stop_release_recording(self,sid,owner,controller,epoch):
        self.begin_release_stop(sid,owner,controller,epoch)
        return self.finalize_release_recording(sid,owner,controller,epoch)
    def release_recording(self,rid,owner):
        check(self._recording_store is not None,'recording_unavailable',
              'Durable recording storage is unavailable')
        check(self.release_recording_owners.get(rid)==owner,'forbidden',
              'Recording belongs to another owner',403)
        try:return self._recording_store.load(rid)
        except Exception:raise LiveError('recording_unavailable','Durable recording is unavailable') from None

    def bind_release_recording_owner(self,rid,owner,registration):
        """Rebind persisted evidence after trusted G5 project composition."""
        check(self._recording_store is not None,'recording_unavailable',
              'Durable recording storage is unavailable')
        try:
            registration=self._recording_store._require_registration(registration)
            value=self._recording_store.load(rid)
            original=value['original']
            project=registration.project
            check(original['projectId']==project['id']
                  and original['projectRevision']==project['revision']
                  and contracts.digest(project)==registration.project_digest,
                  'recording_identity','Release recording project changed',409)
        except LiveError:raise
        except Exception:
            raise LiveError('recording_identity',
                            'Release recording binding is invalid',409) from None
        with self.lock:
            existing=self.release_recording_owners.get(rid)
            check(existing in {None,owner},'recording_identity',
                  'Release recording owner changed',409)
            self.release_recording_owners[rid]=owner
        return copy.deepcopy(value)

    def append_release_lifecycle(self,rid,owner,*,operation_id,generation,
                                 kind,status,observed_at_ms=None):
        check(self._recording_store is not None,'recording_unavailable',
              'Durable recording storage is unavailable')
        check(self.release_recording_owners.get(rid)==owner,'forbidden',
              'Recording belongs to another owner',403)
        try:
            return self._recording_store.append_lifecycle(
                rid,operation_id=operation_id,generation=generation,kind=kind,
                status=status,observed_at_ms=observed_at_ms)
        except Exception:
            raise LiveError('recording_unavailable',
                            'Lifecycle evidence could not be appended') from None
    def _app_log_retained(self, session):
        try:
            reference = self._evidence_store.lookup(session.get('appLogDigest'))
            return reference is not None and reference.retain_until_ms > int(time.time() * 1000)
        except Exception:
            return False

    def app_logs(self,sid,owner,*,_allow_stopping=False,classification=None,
                 acquisition_sequence=None):
        s=self._session(sid,owner)
        with s['lock']:
            self._authorize_effect(s,'app_logs')
            check(s.get('automaticAppLogs') is True,'unsupported_operation','This session has no automatic app logs',400)
            recorder=s.get('releaseRecorder')
            if recorder is not None:
                check(s['releaseRegistration'].project['evidencePolicy'].get('logs') is True,
                      'capture_suppressed','Log collection is disabled',403)
            if s['state'] in {'closed','draining','failed'}:
                if recorder is not None:
                    if not self._app_log_retained(s):
                        s.pop('appLog', None)
                        raise LiveError('app_log_expired', 'The original app log is no longer available', 410)
                check('appLog' in s,'app_log_unavailable','No app log snapshot was saved for this session',409)
                return copy.deepcopy(s['appLog'])
            check(_allow_stopping or not s.get('stopAdmission',False),
                  'session_inactive','Session is stopping')
            permit=self._prepare_effect(s,'app_logs',{'scope':'bounded-provider-log'})
            collector=(getattr(s['provider'],'collect_app_logs_authorized',None) if permit is not None
                       else getattr(s['provider'],'collect_app_logs',None))
            check(callable(collector),'unsupported_operation','Automatic app logs are not configured',400)
            if recorder is not None:s['inflightCollections']+=1
        failure_reason='log_collection_failed'
        try:
            self._authorize_effect(s,'app_logs')
            value=collector(permit) if permit is not None else collector()
            with s['lock']:
                check((s['state']=='active' or _allow_stopping)
                      and (_allow_stopping or not s.get('stopAdmission',False)),
                      'session_inactive','Collection authority was revoked')
            self._confirm_effect(s,permit,'succeeded',{'ok':True},'app_logs')
            self._authorize_effect(s,'app_logs')
            from ..app_logs import APP_LOG_MIME, MAX_APP_LOG_BYTES
            failure_reason='log_collection_invalid'
            check(isinstance(value,dict) and isinstance(value.get('sessionId'),str)
                  and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value['sessionId'])
                  and len(json.dumps(value,allow_nan=False,separators=(',', ':')).encode())<=MAX_APP_LOG_BYTES,
                  'app_log_invalid','Automatic app log snapshot is invalid',400)
            if recorder is not None:
                with s['lock']:
                    if acquisition_sequence is None:
                        s['releaseObservationSequence']+=1
                        acquisition_sequence=s['releaseObservationSequence']
                    elif type(acquisition_sequence) is int:
                        s['releaseObservationSequence']=max(
                            s['releaseObservationSequence'],acquisition_sequence)
                failure_reason=None
                try:
                    stored=recorder.record_observation(
                        'logs',value,acquisition_sequence=acquisition_sequence,
                        classification=classification,mime_type=APP_LOG_MIME,
                        terminal_snapshot=_allow_stopping)
                except Exception:
                    raise LiveError('capture_suppressed',
                                    'Log sample was not durably collected') from None
                check(stored is not None,'capture_suppressed',
                      'Log sample was suppressed',403)
            else:
                write_json(self.output/'app-logs'/sid/(value['sessionId']+'.json'),value)
            with s['lock']:
                if recorder is not None:s['appLogDigest']=stored['digest']
                s['appLog']=copy.deepcopy(value);s.pop('appLogError',None)
            return copy.deepcopy(value)
        except Exception:
            if recorder is not None and failure_reason is not None:
                try:recorder.declare_gap(failure_reason)
                except Exception:pass
            if permit is not None:self.fail(sid,'App log collection result is uncertain; close the session')
            raise
        finally:
            if recorder is not None:
                with s['lock']:s['inflightCollections']-=1
    def collect_sdk_capture(self,sid,owner,controller,epoch):
        s=self._session(sid,owner)
        with s['lock']:
            self._control(s,controller,epoch)
            permit=self._prepare_effect(s,'sdk_capture',{'scope':'frozen-capture'})
            collector=(getattr(s['provider'],'collect_sdk_capture_authorized',None) if permit is not None
                       else getattr(s['provider'],'collect_sdk_capture',None))
            check(callable(collector),'unsupported_operation','SDK capture is not configured',400)
        try:
            self._authorize_effect(s,'sdk_capture')
            value=collector(permit) if permit is not None else collector()
            with s['lock']:
                check(s['state']=='active' and not s.get('stopAdmission',False),
                      'session_inactive','Capture authority was revoked')
            self._confirm_effect(s,permit,'succeeded',{'ok':True},'sdk_capture')
            self._authorize_effect(s,'sdk_capture')
            return value
        except Exception:
            if permit is not None:self.fail(sid,'SDK capture result is uncertain; close the session')
            raise
    def collect_sdk_diagnostics(self,sid,owner,controller,epoch,capture):
        s=self._session(sid,owner)
        with s['lock']:
            self._control(s,controller,epoch)
            permit=self._prepare_effect(s,'sdk_diagnostics',{'scope':'bounded-diagnostics'})
            collector=(getattr(s['provider'],'collect_sdk_diagnostics_authorized',None) if permit is not None
                       else getattr(s['provider'],'collect_sdk_diagnostics',None))
            check(callable(collector),'unsupported_operation','SDK diagnostics are not configured',400)
        try:
            self._authorize_effect(s,'sdk_diagnostics')
            value=collector(capture,permit) if permit is not None else collector(capture)
            with s['lock']:
                check(s['state']=='active' and not s.get('stopAdmission',False),
                      'session_inactive','Diagnostics authority was revoked')
            self._confirm_effect(s,permit,'succeeded',{'ok':True},'sdk_diagnostics')
            self._authorize_effect(s,'sdk_diagnostics')
            return value
        except Exception:
            if permit is not None:self.fail(sid,'SDK diagnostics result is uncertain; close the session')
            raise
    def _save_app_logs_if_available(self,s):
        if not s.get('automaticAppLogs'):return
        if s.get('releaseRecorder') is not None and s.get('stopAdmission',False):
            s['appLogError']='Automatic log collection was not admitted before the stop barrier'
            return
        try:self.app_logs(s['id'],s['owner'],_allow_stopping=True)
        except Exception:s['appLogError']='Latest app log snapshot could not be collected'
    def recording(self,rid,owner):
        entry=self.recordings.get(rid);check(entry is not None,'not_found','Recording not found',404)
        check(entry['owner']==owner,'forbidden','Recording belongs to another owner',403)
        with entry.get('lock',self.lock):
            r=entry['data']
            if r['status']!='recording':
                stored=read_json(self.output/'recordings'/f'{rid}.json')['recording'];check(stored==r,'recording_changed','Recording integrity mismatch')
            return copy.deepcopy({k:v for k,v in r.items() if not k.startswith('_')})
    def import_recording(self,document,owner):
        from .recordings import validate_recording
        try:r=validate_recording(document)
        except (ContractError,TypeError,ValueError):raise LiveError('invalid_recording','Recording schema, input privacy, or integrity validation failed',400) from None
        rid=r['id']
        with self.lock:
            existing=self.recordings.get(rid)
            if existing is not None:
                check(existing['owner']==owner,'forbidden','Recording belongs to another owner',403)
                check(existing['data'].get('digest')==r['digest'],'recording_conflict','Recording ID already has different content')
                return {'recording':copy.deepcopy(r),'imported':False}
            write_json(self.output/'recordings'/f'{rid}.json',{'owner':owner,'recording':r})
            self.recordings[rid]={'owner':owner,'data':r,'lock':threading.RLock()}
        return {'recording':copy.deepcopy(r),'imported':True}
    def derive_recording(self,rid,owner,*,event_ids=None,speed=1):
        from .recordings import derive_recording
        try:result=derive_recording(self.recording(rid,owner),event_ids=event_ids,speed=speed)
        except LiveError:raise
        except (ContractError,ValueError,TypeError):raise LiveError('invalid_derivation','Recording cannot be derived with these events or speed',400) from None
        return self.import_recording(result,owner)['recording']
    def list_recordings(self,owner):
        entries=list(self.recordings.items())
        return sorted([self.recording(rid,owner) for rid,entry in entries if entry['owner']==owner],key=lambda r:r.get('endedAt',r['startedAt']),reverse=True)
    def start_replay(self,sid,owner,controller,epoch,rid,variables=None):
        s=self._session(sid,owner);variables=variables or {}
        with s['lock']:
            self._control(s,controller,epoch)
            check(not s.get('replay') or s['replay']['state'] not in {'running','cancelling'},'replay_busy','Replay already active')
            r=self.recording(rid,owner);check(r['status']=='complete' and r['replayable'],'not_replayable','Recording has no supported starting state')
            check(r.get('applicationIdentity')==self.devices[s['deviceId']]['capabilities'].get('applicationIdentity'),'app_changed','Recorded application differs from the installed app')
            check(r['deviceId']==s['deviceId'] and r['providerKind']==self.devices[s['deviceId']]['kind'],'provider_mismatch','This version replays on the original device and provider only')
            check(all(e['action'] in self.devices[s['deviceId']]['capabilities']['actions'] for e in r['events']),'unsupported_operation','Recording uses an unsupported provider action',400)
            check(isinstance(variables,dict) and set(variables)==set(r['variables']),'missing_variables','Supply all recorded text variables',400)
            for value in variables.values():validate_gesture('text',{'value':value})
            original_mode=s['mode']
        self._cancel_active_pointers(s)
        with s['lock']:
            self._control(s,controller,epoch)
            self._invalidate_recording(s,'replay_started');s['epoch']+=1;replay_id=uuid.uuid4().hex
            replay_controller='replay-'+replay_id;s.update(controllerId=replay_controller,lastSequence=0,mode='replay')
            s['cancel']=threading.Event();cancel=s['cancel'];replay_epoch=s['epoch']
            s['replay']={'id':replay_id,'recordingId':rid,'state':'running','index':0,'total':len(r['events'])}
            thread=threading.Thread(target=self._replay,args=(s,owner,r,copy.deepcopy(variables),controller,replay_controller,replay_epoch,cancel,original_mode),daemon=True)
            s['replayThread']=thread;thread.start();return copy.deepcopy(s['replay'])
    def _replay(self,s,owner,r,variables,return_controller,replay_controller,epoch,cancel,return_mode='manual'):
        result={'id':s['replay']['id'],'recordingId':r['id'],'recordingDigest':r['digest'],'state':'running','receipts':[]}
        try:
            with s['lock']:
                self._control(s,replay_controller,epoch)
            self._reset(s)
            with s['lock']:
                self._control(s,replay_controller,epoch)
                frame=self._frame_meta(s);check({k:frame[k] for k in r['geometry']}==r['geometry'],'geometry_mismatch','Replay geometry differs')
            playback_started=time.monotonic()
            for index,event in enumerate(r['events'],1):
                delay=max(0,playback_started+event['offsetMs']/1000-time.monotonic())
                deadline=time.monotonic()+delay
                while time.monotonic()<deadline and not cancel.is_set():
                    cancel.wait(min(max(0,deadline-time.monotonic()),self.idle_timeout/3,10))
                    if not cancel.is_set():
                        with s['lock']:self._control(s,replay_controller,epoch)
                if cancel.is_set():result['state']='cancelled';break
                self.recording(r['id'],owner)
                with s['lock']:
                    self._control(s,replay_controller,epoch);frame=self._frame_meta(s)
                    check({k:frame[k] for k in r['geometry']}==r['geometry'],'geometry_mismatch','Replay geometry changed')
                    payload=copy.deepcopy(event['payload'])
                    if event['action']=='text':payload={'value':variables[event['payload']['variable']]}
                    cmd={'controllerId':replay_controller,'epoch':epoch,'sequence':index,'commandId':uuid.uuid4().hex,
                         'frameId':frame['id'],'geometryVersion':frame['geometryVersion'],'action':event['action'],'payload':payload}
                receipt=self.input(s['id'],owner,cmd)
                with s['lock']:
                    result['receipts'].append(receipt);s['replay']['index']=index
            else:
                self.recording(r['id'],owner);result['state']='actions_replayed'
        except Exception:
            result['state']='cancelled' if cancel.is_set() and s['state']=='active' else 'failed';result['error']='Replay stopped because an input, state or integrity contract failed'
        finally:
            variables.clear()
            with s['lock']:has_pointers=bool(s.get('activePointers'))
            pointer_cancel_failed=False
            if has_pointers:
                try:self._cancel_active_pointers(s)
                except LiveError:
                    pointer_cancel_failed=True;result.update(state='failed',error='Pointer cancellation was not confirmed')
            with s['lock']:
                try:write_json(self.output/'replays'/f'{result["id"]}.json',result)
                except (OSError,ContractError):result.update(state='failed',error='Replay receipt could not be persisted')
                s['replay'].update({k:v for k,v in result.items() if k!='receipts'})
                if not pointer_cancel_failed and s['controllerId']==replay_controller and s['epoch']==epoch:
                    s.update(controllerId=return_controller,epoch=epoch+1,lastSequence=0,mode=return_mode)
    def cancel_replay(self,sid,owner):
        s=self._session(sid,owner)
        with s['lock']:
            check(s.get('replay') is not None,'not_replaying','No replay exists')
            s['cancel'].set()
            if s['replay']['state']=='running':s['replay']['state']='cancelling'
            return copy.deepcopy(s['replay'])
    def fail(self,sid,message):
        s=self._session(sid)
        recorder=None
        with s['lock']:
            if s['state'] in {'closed','draining'}:return
            handle=s.get('deviceAuthority')
            if handle is not None:
                try:handle.revoke_dispatches()
                except Exception:pass
            recorder=s.get('releaseRecorder')
            if recorder is not None:
                try:recorder.stop_barrier(reason='provider_failed')
                except Exception:pass
            s.update(state='failed',error=message,epoch=s['epoch']+1);s['cancel'].set();self._invalidate_recording(s,'provider_failed')
        if recorder is not None:
            try:recorder.freeze()
            except Exception:pass
        with self.lock:self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
    def close_session(self,sid,owner,controller=None,epoch=None,*,reserve_for_repair=False,
                      require_finished_inputs=False):
        s=self._session(sid,owner)
        release_recorder=None
        with s['lock']:
            if s['state'] in {'closed','draining'}:return self._public(s)
            if (s['state']=='failed' and s.get('provider') is None
                    and s.get('_retainedDeviceScope') is not None):
                # A retained startup failure has no provider to close. Keep the
                # enclosing authority and quarantine the reservation for
                # explicit reconciliation; close_all must not release it.
                scope=s['_retainedDeviceScope']
                s.update(state='closed',stopAdmission=True,epoch=s['epoch']+1)
                s['cancel'].set();self._invalidate_recording(s,'session_closed')
                with self.lock:
                    self.devices[s['deviceId']].update(
                        state='quarantined',sessionId=scope.reservation_id)
                    self._persist_devices()
                self._event(s,'session_closed',reason='retained_startup_failed')
                return self._public(s)
            if (s['state']=='failed' and s.get('provider') is None
                    and s.get('_startupReleaseConfirmed') is True):
                # Startup already released this session's exact reservation.
                # Closing its record must not touch a later device owner.
                s.update(state='closed',stopAdmission=True,epoch=s['epoch']+1)
                s['cancel'].set();self._invalidate_recording(s,'session_closed')
                self._event(s,'session_closed',reason='startup_rejected')
                return self._public(s)
            if s['state']=='active' and controller is not None:
                check(controller==s['controllerId'] and type(epoch) is int and epoch==s['epoch'],'stale_controller','Control has moved to another controller')
            release_recorder=s.get('releaseRecorder')
            if release_recorder is not None:
                try:release_recorder.stop_barrier(reason='session_closed')
                except Exception:pass
            s['stopAdmission']=True
            authority_bound=s.get('deviceAuthority') is not None
        if release_recorder is not None:
            try:release_recorder.freeze()
            except Exception:pass
        if not authority_bound:
            try:self._cancel_active_pointers(s)
            except LiveError:pass
        self._save_app_logs_if_available(s)
        with s['lock']:
            s.update(state='draining',epoch=s['epoch']+1);s['cancel'].set();self._invalidate_recording(s,'session_closed')
        cleanup_permit=None
        cleanup_confirmed=False
        try:
            if s.get('deviceAuthority') is not None:
                cleanup_permit=self._prepare_effect(s,'cleanup',{'scope':'native-helper-and-pointers'})
                closer=getattr(s['provider'],'close_authorized',None)
                check(callable(closer),'native_protocol_mismatch',
                      'Provider does not support authorized cleanup',400)
                cleanup_result=closer(cleanup_permit)
                check(cleanup_result is None or (isinstance(cleanup_result,dict)
                      and cleanup_result.get('ok') is True),
                      'cleanup_uncertain','Provider cleanup was not confirmed')
                self._confirm_effect(s,cleanup_permit,'succeeded',
                                     cleanup_result or {'ok':True},'cleanup')
                cleanup_confirmed=True
            else:s['provider'].close()
        except Exception:
            abort=getattr(s['provider'],'abort',None)
            if callable(abort):
                try:abort()
                except Exception:pass
            s.update(state='failed',error='Provider cleanup failed')
            with self.lock:self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
            handle=s.get('deviceAuthority')
            if handle is not None and s.get('_retainedDeviceScope') is None:handle.close()
            return self._public(s)
        handle=s.get('deviceAuthority')
        retained_scope=s.get('_retainedDeviceScope')
        if handle is not None:
            check(cleanup_confirmed,'cleanup_uncertain','Authority cleanup was not confirmed')
            handle.confirm_native_cleanup(cleanup_permit)
            if retained_scope is None and not handle.close():
                s.update(state='failed',error='Device release was not durably confirmed')
                with self.lock:
                    self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
                return self._public(s)
        with s['lock']:
            if require_finished_inputs and s['inflightCommands']:
                s.update(state='failed',error='Input completion is still unconfirmed')
                with self.lock:
                    self.devices[s['deviceId']]['state']='quarantined';self._persist_devices()
                return self._public(s)
            s['state']='closed'
            s['activePointers'].clear();s['pointerLastActivity'].clear()
            with s['frameLock']:s['frame']=None
        with self.lock:
            if retained_scope is not None:
                self.devices[s['deviceId']].update(
                    state='reserved',sessionId=retained_scope.reservation_id)
            else:
                self.devices[s['deviceId']].update(
                    state='repairing' if reserve_for_repair else 'available',sessionId=None)
            self._persist_devices()
        with s['lock']:self._event(s,'session_closed',reason=s.get('closeReason','requested'))
        return self._public(s)
    def close_all(self):
        self.stop_maintenance()
        for s in list(self.sessions.values()):
            try:self.close_session(s['id'],s['owner'])
            except LiveError:continue
        with self.lock:
            for reservation in list(self._device_reservations.values()):
                if reservation.reservation_id in self._retained_device_scopes:
                    continue
                try:self.release_device_reservation(reservation)
                except LiveError:continue
            self._retained_startup_bindings.clear()
            retained=list(self._retained_device_scopes.values())
        if retained:
            # Lab shutdown is not proof of the enclosing adapter's final
            # sanitation. Fence retained handles and quarantine them instead
            # of letting HostAuthority.close publish a release.
            for scope in retained:
                handle=scope._reservation._authority_handle
                if handle is not None:
                    try:handle.revoke_dispatches()
                    except Exception:pass
                with self.lock:
                    device=self.devices.get(scope.device_id)
                    if device is not None:
                        device.update(state='quarantined',sessionId=scope.reservation_id)
                        self._persist_devices()
        elif self._owns_authority and self.authority is not None:
            self.authority.close()
        self.close_evidence_store()
    def close_evidence_store(self):
        if self._evidence_closed:return
        self._evidence_closed=True
        for sink in self._video_sinks:
            try:sink.close()
            except Exception:pass
        self._video_sinks.clear()
        for resource in (self._recording_store,self._evidence_store,self._recording_budget):
            if resource is not None:
                try:resource.close()
                except Exception:pass


__all__ = ['Lab','LiveError','TrustedDeviceReservation','TrustedDeviceScope','check','public_id',
           'validate_gesture']
