"""Authenticated host-local observations bound to an active Android installation."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import hmac
import math
import os
import re
import secrets
import select
import socket
import stat
import struct
import sys
import threading
import time

from . import contracts
from .contracts.versions import exact
from .execution.artifacts import open_directory
from .execution.protocol import validate_external_validation_plan
from .execution.wire import canonical,decode_json
from .repair_android import AndroidTrustedMobileAdapter
from .repair_signing_configuration import _path
from .validation import ValidationContext,ValidationError,ValidationObservation


def _require(value,code='validation_observer_unavailable'):
    if not value:raise ValidationError(code)


class ValidationSecretRegistry:
    """Explicit scoped bytes only; no environment or credential-file discovery."""
    def __init__(self):
        self._changed=threading.Condition(threading.RLock())
        self._entries={};self._closed=False;self._active=0;self._owner=None

    def __repr__(self):
        with self._changed:return f'<ValidationSecretRegistry entries={len(self._entries)} closed={self._closed}>'

    @property
    def active_requests(self):
        with self._changed:return self._active

    @property
    def closed(self):
        with self._changed:return self._closed

    def register(self,reference_id,*,project_digest,provider_id,secret):
        contracts.validate_id(reference_id);contracts.validate_id(provider_id);contracts.validate_digest(project_digest)
        _require(type(secret) in (bytes,bytearray) and 32<=len(secret)<=64,'validation_authentication')
        key=(reference_id,project_digest,provider_id)
        with self._changed:
            _require(not self._closed and key not in self._entries and len(self._entries)<128,'validation_authentication')
            self._entries[key]=bytearray(secret)

    def require_reference(self,reference_id,project_digest,provider_id):
        with self._changed:
            _require(not self._closed and (reference_id,project_digest,provider_id) in self._entries,'validation_authentication')

    def require_owner(self,owner):
        with self._changed:_require(not self._closed and (self._owner is None or self._owner is owner),'validation_authentication')

    def claim(self,owner):
        with self._changed:
            self.require_owner(owner);self._owner=owner

    @contextmanager
    def material(self,reference_id,project_digest,provider_id):
        with self._changed:
            self.require_reference(reference_id,project_digest,provider_id)
            key=bytearray(self._entries[(reference_id,project_digest,provider_id)]);self._active+=1
        try:yield key
        finally:
            key[:]=b'\0'*len(key)
            with self._changed:self._active-=1;self._changed.notify_all()

    def revoke(self):
        with self._changed:
            self._closed=True
            for key in self._entries.values():key[:]=b'\0'*len(key)
            self._entries.clear();self._changed.notify_all()

    def close(self,*,timeout_seconds=10):
        _require(type(timeout_seconds) in (int,float) and math.isfinite(timeout_seconds) and 0<timeout_seconds<=30)
        self.revoke();deadline=time.monotonic()+timeout_seconds
        with self._changed:
            while self._active and time.monotonic()<deadline:self._changed.wait(deadline-time.monotonic())
            _require(self._active==0,'validation_cleanup_unknown')
        return True


def _mac(key,kind,message):
    return hmac.new(key,b'reproloop-validation-v1/'+kind.encode('ascii')+b'\0'+canonical(message),hashlib.sha256).hexdigest()


def _peer_uid(connection):
    # Darwin's declared libc getpeereid(int, uid_t *, gid_t *) interface.
    uid=ctypes.c_uint();gid=ctypes.c_uint()
    function=ctypes.CDLL(None,use_errno=True).getpeereid
    function.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint)]
    function.restype=ctypes.c_int
    _require(function(connection.fileno(),ctypes.byref(uid),ctypes.byref(gid))==0)
    return uid.value


def _socket_namespace(path):
    parent=open_directory(path.parent)
    try:
        directory=os.fstat(parent);node=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
        _require(directory.st_uid==os.getuid() and stat.S_IMODE(directory.st_mode)==0o700
            and stat.S_ISSOCK(node.st_mode) and node.st_uid==os.getuid()
            and node.st_nlink==1 and stat.S_IMODE(node.st_mode)==0o600)
        identity=(directory.st_dev,directory.st_ino,node.st_dev,node.st_ino,node.st_uid,node.st_mode)
        return parent,identity
    except BaseException:
        os.close(parent);raise


class UnixAndroidValidationObserver:
    """Read one authenticated response from a registered independent service.

The service must independently measure the bound check and finish its work
before reporting cleanup. Transport failure or unauthenticated data cannot
produce a ValidationObservation and therefore quarantine the calling authority.
"""
    def __init__(self,adapter,plan,*,source_id,provider_id,socket_path,authentication_reference_id,secret_registry):
        _require(sys.platform=='darwin' and type(adapter) is AndroidTrustedMobileAdapter
            and type(secret_registry) is ValidationSecretRegistry)
        checked=validate_external_validation_plan(plan)
        contracts.validate_id(source_id);contracts.validate_id(provider_id);contracts.validate_id(authentication_reference_id)
        path=_path(str(socket_path));_require(len(os.fsencode(path))<104)
        checks=[row for row in checked['checks'] if row['evidenceSourceId']==source_id]
        _require(checks and all(row['kind']=='external-observation' for row in checks)
            and checked['projectDigest']==adapter.config.registration.project_digest)
        secret_registry.require_reference(authentication_reference_id,checked['projectDigest'],provider_id)
        self.adapter=adapter;self.plan_digest=contracts.digest(checked);self.project_digest=checked['projectDigest']
        self.source_id=source_id;self.provider_id=provider_id;self.socket_path=path
        self.authentication_reference_id=authentication_reference_id;self.secrets=secret_registry
        self._checks={row['id']:row['recipeId'] for row in checks}

    def _guard(self,context):
        _require(type(context) is ValidationContext and context.validation_plan_digest==self.plan_digest
            and context.evidence_source_id==self.source_id and self._checks.get(context.check_id)==context.recipe_id
            and type(context.nonce) is str and re.fullmatch(r'[0-9a-f]{48}',context.nonce) is not None)
        adapter=self.adapter;mobile=adapter._context;config=adapter.config
        _require(mobile is not None and adapter._installed and adapter._scope is not None and not adapter._stopping.is_set())
        for name in ('operation_id','repair_plan_digest','project_digest','source_digest','artifact_digest'):
            _require(getattr(context.binding,name)==getattr(mobile,name))
        _require(mobile.project_digest==self.project_digest and mobile.application_id==config.application_id
            and mobile.scope_digest==config.scope_digest and mobile.runtime_policy_digest==config.runtime_policy_digest)
        config.lab.validate_retained_device_scope(adapter._scope,owner=config.owner,device_id=config.device_id)
        return {'applicationId':config.application_id,'deviceId':config.device_id,'scopeDigest':config.scope_digest}

    def __call__(self,context,*,cancellation,deadline_monotonic):
        connection=None;parent=None
        def active():
            _require(callable(getattr(cancellation,'is_set',None))
                and type(deadline_monotonic) in (int,float) and math.isfinite(deadline_monotonic)
                and not cancellation.is_set() and not self.secrets.closed and not self.adapter._stopping.is_set()
                and time.monotonic()<deadline_monotonic,
                'validation_interrupted')
        def wait(writable):
            while True:
                active();timeout=min(.05,deadline_monotonic-time.monotonic())
                readable,writing,_=select.select([] if writable else [connection],[connection] if writable else [],[],timeout)
                if readable or writing:return
        def send(body):
            offset=0
            while offset<len(body):
                active()
                try:count=connection.send(body[offset:])
                except BlockingIOError:wait(True);continue
                _require(count>0);offset+=count
        def receive(size):
            result=bytearray()
            while len(result)<size:
                active()
                try:chunk=connection.recv(size-len(result))
                except BlockingIOError:wait(False);continue
                _require(bool(chunk));result.extend(chunk)
            return bytes(result)
        try:
            active();target=self._guard(context)
            adapter=self.adapter;scope=adapter._scope;installation_digest=adapter._context.digest
            with self.secrets.material(self.authentication_reference_id,self.project_digest,self.provider_id) as key:
                parent,namespace=_socket_namespace(self.socket_path)
                connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.setblocking(False)
                result=connection.connect_ex(str(self.socket_path))
                _require(result in (0,errno.EINPROGRESS,errno.EALREADY,errno.EWOULDBLOCK))
                if result:
                    wait(True);_require(connection.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)==0)
                _require(_peer_uid(connection)==os.getuid())
                request={'schemaVersion':1,'providerId':self.provider_id,'sourceId':self.source_id,
                    'context':context.public(),'exchangeNonce':secrets.token_hex(32),'target':target}
                body=canonical({'message':request,'mac':_mac(key,'request',request)});_require(len(body)<=65536)
                send(struct.pack('!I',len(body))+body)
                length=struct.unpack('!I',receive(4))[0];_require(0<length<=65536)
                envelope=decode_json(receive(length));exact(envelope,('message','mac'))
                response=envelope['message'];signature=envelope['mac']
                _require(type(signature) is str and re.fullmatch(r'[0-9a-f]{64}',signature) is not None
                    and hmac.compare_digest(signature,_mac(key,'response',response)),'validation_authentication')
                exact(response,('schemaVersion','providerId','sourceId','contextDigest','exchangeNonce','target',
                    'outcome','evidenceDigest','terminationConfirmed','cleanupConfirmed'))
                _require(type(response['schemaVersion']) is int and response['schemaVersion']==1
                    and response['providerId']==self.provider_id and response['sourceId']==self.source_id
                    and response['contextDigest']==context.digest and response['exchangeNonce']==request['exchangeNonce']
                    and response['target']==target and response['outcome'] in ('pass','fail','unknown')
                    and type(response['terminationConfirmed']) is bool and type(response['cleanupConfirmed']) is bool)
                contracts.validate_digest(response['evidenceDigest'])
                checked,current_namespace=_socket_namespace(self.socket_path);os.close(checked)
                _require(current_namespace==namespace and self._guard(context)==target
                    and self.adapter is adapter and adapter._scope is scope
                    and adapter._context.digest==installation_digest);active()
                evidence=contracts.digest({'contextDigest':context.digest,'providerId':self.provider_id,
                    'sourceId':self.source_id,'target':target,'responseMac':signature,'evidenceDigest':response['evidenceDigest']})
                return ValidationObservation(context.digest,response['outcome'],evidence,
                    response['terminationConfirmed'],response['cleanupConfirmed'])
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
            raise ValidationError('validation_observer_unavailable') from None
        finally:
            if connection is not None:connection.close()
            if parent is not None:os.close(parent)


class UnixIOSValidationObserver:
    """Authenticated host-local observation bound to a live iOS installation.

    The wire contract deliberately matches the Android observer.  The adapter
    type is exact, so a serialized or foreign mobile implementation cannot be
    substituted for the trusted iOS owner.
    """
    def __init__(self,adapter,plan,*,source_id,provider_id,socket_path,authentication_reference_id,secret_registry):
        from .repair_ios import IOSTrustedMobileAdapter
        _require(sys.platform=='darwin' and type(adapter) is IOSTrustedMobileAdapter
            and type(secret_registry) is ValidationSecretRegistry)
        checked=validate_external_validation_plan(plan)
        contracts.validate_id(source_id);contracts.validate_id(provider_id);contracts.validate_id(authentication_reference_id)
        path=_path(str(socket_path));_require(len(os.fsencode(path))<104)
        checks=[row for row in checked['checks'] if row['evidenceSourceId']==source_id]
        _require(checks and all(row['kind']=='external-observation' for row in checks)
            and checked['projectDigest']==adapter.config.registration.project_digest)
        secret_registry.require_reference(authentication_reference_id,checked['projectDigest'],provider_id)
        self.adapter=adapter;self.plan_digest=contracts.digest(checked);self.project_digest=checked['projectDigest']
        self.source_id=source_id;self.provider_id=provider_id;self.socket_path=path
        self.authentication_reference_id=authentication_reference_id;self.secrets=secret_registry
        self._checks={row['id']:row['recipeId'] for row in checks}

    def _guard(self,context):
        _require(type(context) is ValidationContext and context.validation_plan_digest==self.plan_digest
            and context.evidence_source_id==self.source_id and self._checks.get(context.check_id)==context.recipe_id
            and type(context.nonce) is str and re.fullmatch(r'[0-9a-f]{48}',context.nonce) is not None)
        adapter=self.adapter;mobile=adapter._context;config=adapter.config
        _require(mobile is not None and adapter._installed and adapter._scope is not None and not adapter._stopping.is_set())
        for name in ('operation_id','repair_plan_digest','project_digest','source_digest','artifact_digest'):
            _require(getattr(context.binding,name)==getattr(mobile,name))
        _require(mobile.project_digest==self.project_digest and mobile.application_id==config.application_id
            and mobile.scope_digest==config.scope_digest and mobile.runtime_policy_digest==config.runtime_policy_digest)
        config.lab.validate_retained_device_scope(adapter._scope,owner=config.owner,device_id=config.device_id)
        return {'applicationId':config.application_id,'deviceId':config.device_id,'scopeDigest':config.scope_digest}

    def __call__(self,context,*,cancellation,deadline_monotonic):
        connection=None;parent=None
        def active():
            _require(callable(getattr(cancellation,'is_set',None))
                and type(deadline_monotonic) in (int,float) and math.isfinite(deadline_monotonic)
                and not cancellation.is_set() and not self.secrets.closed and not self.adapter._stopping.is_set()
                and time.monotonic()<deadline_monotonic,'validation_interrupted')
        def wait(writable):
            while True:
                active();timeout=min(.05,deadline_monotonic-time.monotonic())
                readable,writing,_=select.select([] if writable else [connection],[connection] if writable else [],[],timeout)
                if readable or writing:return
        def send(body):
            offset=0
            while offset<len(body):
                active()
                try:count=connection.send(body[offset:])
                except BlockingIOError:wait(True);continue
                _require(count>0);offset+=count
        def receive(size):
            result=bytearray()
            while len(result)<size:
                active()
                try:chunk=connection.recv(size-len(result))
                except BlockingIOError:wait(False);continue
                _require(bool(chunk));result.extend(chunk)
            return bytes(result)
        try:
            active();target=self._guard(context)
            adapter=self.adapter;scope=adapter._scope;installation_digest=adapter._context.digest
            with self.secrets.material(self.authentication_reference_id,self.project_digest,self.provider_id) as key:
                parent,namespace=_socket_namespace(self.socket_path)
                connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.setblocking(False)
                result=connection.connect_ex(str(self.socket_path))
                _require(result in (0,errno.EINPROGRESS,errno.EALREADY,errno.EWOULDBLOCK))
                if result:
                    wait(True);_require(connection.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)==0)
                _require(_peer_uid(connection)==os.getuid())
                request={'schemaVersion':1,'providerId':self.provider_id,'sourceId':self.source_id,
                    'context':context.public(),'exchangeNonce':secrets.token_hex(32),'target':target}
                body=canonical({'message':request,'mac':_mac(key,'request',request)});_require(len(body)<=65536)
                send(struct.pack('!I',len(body))+body)
                length=struct.unpack('!I',receive(4))[0];_require(0<length<=65536)
                envelope=decode_json(receive(length));exact(envelope,('message','mac'))
                response=envelope['message'];signature=envelope['mac']
                _require(type(signature) is str and re.fullmatch(r'[0-9a-f]{64}',signature) is not None
                    and hmac.compare_digest(signature,_mac(key,'response',response)),'validation_authentication')
                exact(response,('schemaVersion','providerId','sourceId','contextDigest','exchangeNonce','target',
                    'outcome','evidenceDigest','terminationConfirmed','cleanupConfirmed'))
                _require(type(response['schemaVersion']) is int and response['schemaVersion']==1
                    and response['providerId']==self.provider_id and response['sourceId']==self.source_id
                    and response['contextDigest']==context.digest and response['exchangeNonce']==request['exchangeNonce']
                    and response['target']==target and response['outcome'] in ('pass','fail','unknown')
                    and type(response['terminationConfirmed']) is bool and type(response['cleanupConfirmed']) is bool)
                contracts.validate_digest(response['evidenceDigest'])
                checked,current_namespace=_socket_namespace(self.socket_path);os.close(checked)
                _require(current_namespace==namespace and self._guard(context)==target
                    and self.adapter is adapter and adapter._scope is scope
                    and adapter._context.digest==installation_digest);active()
                evidence=contracts.digest({'contextDigest':context.digest,'providerId':self.provider_id,
                    'sourceId':self.source_id,'target':target,'responseMac':signature,'evidenceDigest':response['evidenceDigest']})
                return ValidationObservation(context.digest,response['outcome'],evidence,
                    response['terminationConfirmed'],response['cleanupConfirmed'])
        except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError):
            raise ValidationError('validation_observer_unavailable') from None
        finally:
            if connection is not None:connection.close()
            if parent is not None:os.close(parent)


__all__=['ValidationSecretRegistry','UnixAndroidValidationObserver','UnixIOSValidationObserver']
