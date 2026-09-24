"""Authenticated local observer tests; native Android transport is explicitly doubled."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import threading
import time
import unittest

from reproof import contracts
from reproof.validation import ValidationBinding,ValidationContext,TrustedValidationAuthority,ValidationError
from tests import test_repair_android as support


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def mac(key,kind,message):return hmac.new(key,b'reproof-validation-v1/'+kind.encode()+b'\0'+canonical(message),hashlib.sha256).hexdigest()


class OwnedObserverServer:
    def __init__(self,path,key,state):
        self.path=path;self.key=key;self.state=state;self.mode='normal';self.requests=[]
        self.received=threading.Event();self.release=threading.Event();self.stopped=threading.Event()
        self.server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.server.bind(str(path));path.chmod(0o600)
        self.server.listen();self.server.settimeout(.05)
        self.thread=threading.Thread(target=self.serve);self.thread.start()

    @staticmethod
    def read(connection,size):
        result=b''
        while len(result)<size:
            chunk=connection.recv(size-len(result))
            if not chunk:raise EOFError()
            result+=chunk
        return result

    def serve(self):
        while not self.stopped.is_set():
            try:connection,_=self.server.accept()
            except socket.timeout:continue
            except OSError:break
            with connection:
                try:
                    connection.settimeout(2)
                    size=struct.unpack('!I',self.read(connection,4))[0]
                    if not 0<size<=65536:continue
                    envelope=json.loads(self.read(connection,size));request=envelope['message']
                    if not hmac.compare_digest(envelope['mac'],mac(self.key,'request',request)):continue
                    self.requests.append(request);self.received.set()
                    if self.mode=='delay':self.release.wait(2)
                    observed=json.loads(self.state.read_bytes())['installed']
                    context_digest=contracts.digest(request['context'])
                    response={'schemaVersion':1,'providerId':request['providerId'],'sourceId':request['sourceId'],
                        'contextDigest':context_digest,'exchangeNonce':request['exchangeNonce'],'target':request['target'],
                        'outcome':'pass' if observed==request['context']['artifactDigest'] else 'fail',
                        'evidenceDigest':contracts.digest({'observedInstalledDigest':observed,'contextDigest':context_digest}),
                        'terminationConfirmed':True,'cleanupConfirmed':self.mode!='unclean'}
                    if self.mode=='wrong-context':response['contextDigest']='0'*64
                    if self.mode=='extra-field':response['execute']='OwnedUntrustedCanary'
                    signature=mac(b'x'*32 if self.mode=='wrong-mac' else self.key,'response',response)
                    body=canonical({'message':response,'mac':signature});frame=struct.pack('!I',len(body))+body
                    if self.mode=='drip':
                        for byte in frame:
                            connection.sendall(bytes((byte,)));time.sleep(.03)
                    else:connection.sendall(frame)
                except (OSError,EOFError,ValueError,KeyError):pass

    def close(self):
        self.release.set();self.stopped.set();self.server.close();self.thread.join(3)
        if self.path.exists():self.path.unlink()
        if self.thread.is_alive():raise RuntimeError('owned observer did not stop')


class ProtectedUnixValidationTests(unittest.TestCase):
    def setUp(self):
        from reproof.protected_validation import ValidationSecretRegistry,UnixAndroidValidationObserver
        self.fixture=support.AndroidAdapterTests(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown);self.fixture.install()
        self.key=secrets.token_bytes(32);self.registry=ValidationSecretRegistry();self.addCleanup(self.registry.close)
        self.registry.register('owned-auth',project_digest=self.fixture.registration.project_digest,
            provider_id='owned-observer',secret=self.key)
        self.server=OwnedObserverServer(self.fixture.root/'observer.sock',self.key,self.fixture.state_path)
        self.addCleanup(self.server.close)
        self.plan={'schemaVersion':1,'id':'validation','projectDigest':self.fixture.registration.project_digest,
            'checks':[{'id':'regression','recipeId':'regression_ui','kind':'external-observation','evidenceSourceId':'backend-source'}],
            'candidateReports':'supplemental-only'}
        self.observer=UnixAndroidValidationObserver(self.fixture.adapter,self.plan,source_id='backend-source',
            provider_id='owned-observer',socket_path=self.server.path,authentication_reference_id='owned-auth',
            secret_registry=self.registry)
        mobile=self.fixture.context
        binding=ValidationBinding(mobile.operation_id,mobile.repair_plan_digest,mobile.project_digest,
                                  mobile.source_digest,mobile.artifact_digest)
        self.context=ValidationContext(binding,contracts.digest(self.plan),'regression','regression_ui','backend-source',secrets.token_hex(24))

    def observe(self,*,timeout=1,cancellation=None):
        return self.observer(self.context,cancellation=cancellation or threading.Event(),deadline_monotonic=time.monotonic()+timeout)

    def test_authenticated_observation_reads_installed_state_and_binds_fresh_exchange(self):
        first=self.observe();second=self.observe()
        self.assertEqual((first.outcome,first.context_digest),('pass',self.context.digest))
        self.assertTrue(first.termination_confirmed and first.cleanup_confirmed)
        self.assertNotEqual(self.server.requests[0]['exchangeNonce'],self.server.requests[1]['exchangeNonce'])
        state=json.loads(self.fixture.state_path.read_bytes());state['installed']=self.fixture.original_sha
        self.fixture.state_path.write_text(json.dumps(state))
        self.assertEqual(self.observe().outcome,'fail')
        self.assertNotIn(self.key.hex(),repr(self.registry))

    def test_wrong_mac_context_and_extra_fields_cannot_issue_validation_evidence(self):
        for mode in ('wrong-mac','wrong-context','extra-field'):
            self.server.mode=mode
            with self.subTest(mode=mode),self.assertRaises(ValidationError) as error:self.observe()
            self.assertNotIn('OwnedUntrustedCanary',str(error.exception))

    def test_protocol_failure_and_unconfirmed_cleanup_quarantine_the_validation_authority(self):
        for mode in ('wrong-mac','unclean'):
            authority=TrustedValidationAuthority(self.plan)
            authority.register('backend-source',self.observer,kind='external-observation')
            self.server.mode=mode
            receipt=authority.run(self.context.binding,cancellation=threading.Event(),timeout_seconds=1)
            self.assertEqual(receipt.public()['status'],'quarantined')

    def test_socket_permissions_and_missing_live_installation_are_rejected_before_requests(self):
        self.server.path.chmod(0o666)
        with self.assertRaises(ValidationError):self.observe()
        self.assertEqual(self.server.requests,[]);self.server.path.chmod(0o600)
        self.fixture.adapter.cleanup(self.fixture.context,**self.fixture.bounds())
        with self.assertRaises(ValidationError):self.observe()
        self.assertEqual(self.server.requests,[])

    def test_absolute_deadline_stops_a_drip_fed_response(self):
        self.server.mode='drip';started=time.monotonic()
        with self.assertRaises(ValidationError):self.observe(timeout=.12)
        self.assertLess(time.monotonic()-started,.7)
        self.assertEqual(self.registry.active_requests,0)

    def test_registry_revocation_interrupts_active_io_and_erases_registered_secrets(self):
        self.server.mode='delay';outcomes=[]
        def observe():
            try:outcomes.append(self.observe(timeout=2))
            except ValidationError:outcomes.append('rejected')
        worker=threading.Thread(target=observe);worker.start()
        try:
            self.assertTrue(self.server.received.wait(1))
            self.registry.close(timeout_seconds=1);worker.join(1)
            self.assertEqual(outcomes,['rejected']);self.assertEqual(self.registry.active_requests,0)
            with self.assertRaises(ValidationError):self.observe()
        finally:self.server.release.set();worker.join(3)

    def test_installation_context_cannot_change_while_observation_is_in_flight(self):
        from dataclasses import replace
        self.server.mode='delay';outcomes=[];original=self.fixture.adapter._context
        def observe():
            try:outcomes.append(self.observe(timeout=2))
            except ValidationError:outcomes.append('rejected')
        worker=threading.Thread(target=observe);worker.start()
        try:
            self.assertTrue(self.server.received.wait(1))
            self.fixture.adapter._context=replace(original,nonce='f'*48)
            self.server.release.set();worker.join(1)
            self.assertEqual(outcomes,['rejected'])
        finally:
            self.fixture.adapter._context=original;self.server.release.set();worker.join(3)

    def test_foreign_peer_identity_receives_no_observation_request(self):
        from unittest.mock import patch
        with patch('reproof.protected_validation._peer_uid',return_value=os.getuid()+1):
            with self.assertRaises(ValidationError):self.observe()
        self.assertEqual(self.server.requests,[])


class ProtectedObserverCompositionTests(unittest.TestCase):
    def test_fixed_observer_is_bound_inside_mobile_factory_and_closed_with_its_owner(self):
        from reproof.execution.artifacts import BlobSet
        from reproof.protected_mobile_inputs import LoadedAndroidMobileInputs,_snapshot
        from reproof.protected_validation import ValidationSecretRegistry
        from reproof.protected_validation_inputs import load_android_validation_inputs
        from tests import test_android_mobile_operation_integration as integration
        fixture=integration.PersistentAndroidAdapterTests(methodName='runTest');fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner,args,runtime,approved=fixture.protected();f=fixture.fixture
        mobile=LoadedAndroidMobileInputs('owned-mobile',f.config,'f'*64,_snapshot(f.config,'f'*64))
        key=secrets.token_bytes(32);registry=ValidationSecretRegistry();self.addCleanup(registry.close)
        registry.register('owned-auth',project_digest=f.registration.project_digest,provider_id='owned-observer',secret=key)
        server=OwnedObserverServer(f.root/'observer.sock',key,f.state_path);self.addCleanup(server.close)
        source_id=runtime.validators.plan['checks'][0]['evidenceSourceId']
        document={'schemaVersion':1,'kind':'unix-validation-observers-v1','observers':[{
            'sourceId':source_id,'providerId':'owned-observer','socketPath':str(server.path),'authenticationReferenceId':'owned-auth'}]}
        path=f.root/'validation-inputs.json';path.write_text(json.dumps(document));path.chmod(0o600)
        inputs=load_android_validation_inputs({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
            plan=runtime.validators.plan,mobile_inputs=mobile)
        args.pop('validators')
        from reproof.repair_execution import RepairExecutionError
        foreign=ValidationSecretRegistry();self.addCleanup(foreign.close);foreign.claim(object())
        with self.assertRaises(RepairExecutionError):
            owner.configure_android_mobile(**args,validation_inputs=inputs,validation_secrets=foreign)
        supervisor=owner.configure_android_mobile(**args,validation_inputs=inputs,validation_secrets=registry)
        fixture.adapter=f.adapter=supervisor.adapter.install.__self__
        source=BlobSet((('src/owned.py',b'public bounded fixture'),))
        build=runtime.builder.build(source,operation_id='observer-build',repair_plan_digest='b'*64,cancellation=threading.Event())
        signed=args['signer'].sign(build,operation_id='observer-sign',cancellation=threading.Event())
        proof=supervisor.verify(signed,approved,operation_id='observer-mobile',cancellation=threading.Event(),
            boundary=lambda:None,progress=lambda *_:None)
        supervisor.require_verified(proof,signed,approved)
        self.assertEqual(len(server.requests),1)
        self.assertEqual(len(proof.public()['attempts']),3)
        self.assertEqual(fixture.run_store.status('observer-mobile')['reservedBytes'],0)
        owner.close();self.assertTrue(registry.closed);self.assertEqual(registry.active_requests,0)
