"""Owned integration child: crash during native signing, or recover in a fresh process."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import sys
import threading
import time

from reproloop import contracts
from reproloop.execution.artifacts import BlobSet
from reproloop.execution.journal import RunStore
from reproloop.ios_provisioning_cms import IOSCmsTools, IOSCmsTrust
from reproloop.ios_provisioning_policy import decoded_profile_digest
from reproloop.ios_signing_inputs import (IOSSigningDefinition, IOSSigningIdentity, IOSSigningMaterialResolver,
    IOSSigningOwnerTools, IOSSigningProvisioning)
from reproloop.ios_signing_operation import IOSSigningOperationStore
from reproloop.repair_signing import SigningContext


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    root = Path(sys.argv[1]); operation_id = sys.argv[2]; request = sys.argv[3]; mode = sys.argv[4]
    profile = plistlib.loads((root/'owned-profile.plist').read_bytes())
    chain = tuple((root/('chain-'+str(index)+'.der')).read_bytes() for index in range(2))
    identity = IOSSigningIdentity('owned-key','ios_app','OWNEDTEAM1',chain)
    definition = IOSSigningDefinition(identity,'owned-profiles',
        {'.':{'bundleId':'com.example.reproinventory','entitlements':profile['Entitlements']}},
        {'.':{'cms':(root/'owned-profile.cms').read_bytes(),'profileDigest':decoded_profile_digest(profile)}})
    tools = IOSSigningOwnerTools(root/'signer',sha(root/'signer'),root/'verifier',sha(root/'verifier'),
        sha('/usr/bin/sandbox-exec'),root/'guardian',sha(root/'guardian'))
    provisioning = IOSSigningProvisioning(IOSCmsTools(Path('/usr/bin/openssl'),sha('/usr/bin/openssl'),
        sha('/usr/bin/sandbox-exec')),IOSCmsTrust('owned-issuer',chain[0],(chain[1],)),'OWNEDTEAM1','OWNED-DEVICE')
    store = RunStore(root/'run-store',environment_digest='a'*64,disk_limit=4*1024**3,create=False)
    operations = IOSSigningOperationStore(store,tools,definition,root/'operations',create=False)
    if mode == 'recover':
        deadline = time.monotonic()+5
        while True:
            try:
                with operations.recovery(operation_id,request) as capability:
                    result = store.finish_signing_recovery(capability,authority=operations)
                print(json.dumps({'state':result['state'],'reservedBytes':result['reservedBytes']}))
                return 0
            except Exception:
                if time.monotonic() >= deadline: raise
                time.sleep(.01)
    if mode != 'crash-sign': return 64
    password_fd = int(sys.argv[5]); password = os.read(password_fd,513); os.close(password_fd)
    resolver = IOSSigningMaterialResolver(); resolver.register(identity,pkcs12=root/'owned.p12',password=password)
    policy = {'schemaVersion':1,'id':'owned-signing','platform':'ios','applicationId':'ios_app',
        'identityReferenceId':'owned-key','entitlementsDigest':definition.entitlements_digest,
        'tool':'host-codesign-fixed','candidateHooks':'forbidden','artifactRelation':'pre-post-digests',
        'provisioningReferenceId':'owned-profiles'}
    context = SigningContext(operation_id,'a'*64,'b'*64,'ios_app','c'*64,sha(root/'unsigned.ipa'),
        contracts.digest(policy),secrets.token_hex(24))
    original = subprocess.Popen
    def crash_after_start(arguments, **kwargs):
        process = original(arguments, **kwargs)
        if str(root/'signer') in arguments:
            deadline = time.monotonic()+5
            marker = operations.operation_root(operation_id)/'start.json'
            while time.monotonic() < deadline and process.poll() is None:
                if marker.stat().st_size: os._exit(73)
                time.sleep(.001)
        return process
    subprocess.Popen = crash_after_start
    with operations.admit(context,request) as operation:
        operations.sign(operation,BlobSet((('candidate.ipa',(root/'unsigned.ipa').read_bytes()),)),
            material_resolver=resolver,provisioning=provisioning,policy_document=policy,
            cancellation=threading.Event(),deadline_monotonic=time.monotonic()+30)
    return 74


if __name__ == '__main__': raise SystemExit(main())
