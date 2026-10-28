"""Explicit Darwin sandbox probe, not ADB discovery/runtime qualification.

The caller supplies a pinned, trusted build of PROBE_SOURCE. A file hash binds
that selected build, not compiler provenance. Only fresh owned sentinel endpoints
are contacted. Policies use the production generators with a fresh sibling root;
only the probe's exact executable/read admission is added. No ADB/AVD is started.
Runtime permission is required separately; importing this module has no effects.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
import uuid

from reproof.core import ContractError
from reproof.adb_endpoint import adb_client_sandbox
from .android_recovery_birth import BirthPlan, PinnedFile, _Child, _sandbox
from .android_recovery_authority import _directory

PROBE_SOURCE = r'''
#include <arpa/inet.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
static int opened(const char *p, int flags) {
    int fd=open(p,flags,0600); if(fd<0) return 0; close(fd); return 1;
}
static int connected(const char *kind,const char *value) {
    int fd=socket(!strcmp(kind,"unix")?AF_UNIX:AF_INET,SOCK_STREAM,0),ok=0;
    if(fd<0) return 0;
    if(!strcmp(kind,"unix")) {
        struct sockaddr_un a={0}; a.sun_family=AF_UNIX;
        if(strlen(value)<sizeof(a.sun_path)) {
            strcpy(a.sun_path,value); ok=connect(fd,(struct sockaddr*)&a,sizeof(a))==0;
        }
    } else {
        struct sockaddr_in a={0}; a.sin_family=AF_INET;
        a.sin_port=htons((unsigned short)atoi(value));
        inet_pton(AF_INET,"127.0.0.1",&a.sin_addr);
        ok=connect(fd,(struct sockaddr*)&a,sizeof(a))==0;
    }
    close(fd); return ok;
}
int main(int argc,char **argv) {
    if(argc!=9) return 64;
    int ar=opened(argv[1],O_RDONLY),aw=opened(argv[2],O_WRONLY|O_CREAT|O_EXCL);
    int dr=opened(argv[3],O_RDONLY),dw=opened(argv[4],O_WRONLY|O_CREAT|O_EXCL);
    int an=connected(argv[5],argv[6]),dn=connected("tcp",argv[7]);
    pid_t child=0; char *args[]={"/usr/bin/true",NULL};
    int spawned=posix_spawn(&child,args[0],NULL,NULL,args,environ)==0;
    if(spawned) waitpid(child,NULL,0);
    printf("{\"allowedRead\":%d,\"allowedWrite\":%d,\"allowedNetwork\":%d,"
           "\"outsideRead\":%d,\"outsideWrite\":%d,\"outsideNetwork\":%d,\"unlistedExec\":%d}\n",
           ar,aw,an,dr,dw,dn,spawned);
    return strcmp(argv[8],"reproof-sandbox-probe-v1")?64:0;
}
'''
PROBE_SOURCE_DIGEST = hashlib.sha256(PROBE_SOURCE.encode()).hexdigest()
_EXPECTED = dict(allowedRead=1, allowedWrite=1, allowedNetwork=1,
                 outsideRead=0, outsideWrite=0, outsideNetwork=0, unlistedExec=0)


def _require(value, message='Private sandbox preflight failed'):
    if not value:
        raise ContractError(message)


def _plan_digest(plan):
    digest = hashlib.sha256()
    def add(value):
        digest.update(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()+b'\n')
    add([plan.output, plan.console_port, plan.lifetime_seconds])
    for selected in (plan.adb, plan.aapt, plan.sandbox, plan.sample_apk, plan.helper_apk):
        add([selected.path, selected.sha256, selected.size])
    for tree in (plan.emulator, plan.image):
        add([tree.root, tree.executables])
        for selected in tree.files:
            add([selected.path, selected.sha256, selected.size])
    return digest.hexdigest()


def _probe_policy(policy, probe):
    # Network, write, and process-fork clauses are unchanged. Server/emulator
    # permit their own forks but deny an unlisted executable; client denies fork.
    literal = json.dumps(probe.path)
    return policy + '(allow file-read* (literal '+literal+'))(allow process-exec (literal '+literal+'))'


class PreflightIncomplete(ContractError):
    def __init__(self, owner):
        super().__init__('Private sandbox preflight incomplete; owned evidence retained')
        self.owner = owner


class _Owner:
    def __init__(self, root, fd, cancellation, deadline):
        self.root, self.fd = root, fd
        st = os.fstat(fd);self.identity = (st.st_dev, st.st_ino)
        self.cancellation, self.deadline = cancellation, deadline
        self.children, self.listeners = [], []
        self.cleanup_uncertain = False

    def check(self):
        _require(not self.cancellation.is_set() and time.monotonic() < self.deadline)
        st = os.stat(self.root, follow_symlinks=False)
        _require(stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == self.identity)

    def collect(self):
        clean = True
        until = time.monotonic()+5
        for child in self.children:
            try: clean = child.collect(until) and clean
            except (OSError, RuntimeError): clean = False
        if clean:
            remaining = []
            for listener in self.listeners:
                try: listener.close()
                except OSError:
                    clean = False
                    remaining.append(listener)
            self.listeners = remaining
        if clean and self.fd is not None:
            fd, self.fd = self.fd, None
            try: os.close(fd)
            except OSError: clean = False
        self.cleanup_uncertain = self.cleanup_uncertain or not clean
        return clean and not self.cleanup_uncertain


def _listener(owner, family, endpoint):
    value = socket.socket(family, socket.SOCK_STREAM)
    owner.listeners.append(value)
    value.bind(endpoint);value.listen(16)
    return value


def _run(owner, argv, environment):
    owner.check()
    child = _Child();owner.children.append(child)  # retained before Popen
    child.start(argv, environment, owner.root)
    until = min(owner.deadline, time.monotonic()+5)
    while child.process.poll() is None:
        owner.check()
        _require(child.bounded() and time.monotonic() < until)
        owner.cancellation.wait(.01)
    _require(child.collect(min(owner.deadline, time.monotonic()+2)))
    owner.check()
    _require(child.process.returncode == 0 and child.bounded())
    raw = bytes(child.collectors[0].data)
    _require(len(raw) <= 2048)
    try: value = json.loads(raw)
    except (ValueError, UnicodeError): raise ContractError('Invalid sandbox probe output') from None
    _require(type(value) is dict and set(value) == set(_EXPECTED)
             and all(type(x) is int and x in (0, 1) for x in value.values()))
    return value


def run_sandbox_preflight(plan, sandbox_probe, *, cancellation):
    """Effectful preflight; must finish before birth, never accepts receipt JSON."""
    _require(sys.platform == 'darwin' and type(plan) is BirthPlan
             and type(sandbox_probe) is PinnedFile and sandbox_probe.size <= 2*1024*1024
             and type(cancellation) is threading.Event and not cancellation.is_set())
    deadline = time.monotonic()+60
    for selected in (sandbox_probe, plan.sandbox):
        selected.verify(cancellation=cancellation, deadline=deadline)
    parent = Path(plan.output).parent
    parent_fd = _directory(parent)
    root = parent/('reproof-preflight-'+uuid.uuid4().hex[:12])
    owner = None
    try:
        _require(len(os.fsencode(root/'target'/'gateway.sock')) < 104)
        os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        fd = os.open(root.name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            owner = _Owner(root, fd, cancellation, deadline)
        except BaseException:
            os.close(fd)
            raise
        owner.check()
        target = root/'target';target.mkdir(mode=0o700)
        for name in ('home', 'android', 'avd', 'client', 'server-work', 'gateways'):
            (target/name).mkdir(mode=0o700)
        allowed_read = target/'canary';allowed_read.write_bytes(b'public owned sentinel')
        outside_read = root/'outside';outside_read.write_bytes(b'public owned sentinel')
        server = _listener(owner, socket.AF_INET, ('127.0.0.1', plan.console_port+1))
        emulator = _listener(owner, socket.AF_INET, ('127.0.0.1', 0))
        denied = _listener(owner, socket.AF_INET, ('127.0.0.1', 0))
        gateway = target/'gateway.sock'
        _listener(owner, socket.AF_UNIX, str(gateway))
        ports = [x.getsockname()[1] for x in (server, emulator, denied)]
        _require(len(set(ports)) == 3 and 5037 not in ports)
        policies = {
            'server': _sandbox(plan, target, target/'server.sock', target/'report.sock', ports[1], emulator=False),
            'emulator': _sandbox(plan, target, target/'server.sock', target/'report.sock', ports[1], emulator=True),
            'client': adb_client_sandbox(plan.adb.path, target/'client', gateway),
        }
        # The client can read only its work directory, unlike both producer roles.
        (target/'client'/'canary').write_bytes(b'public owned sentinel')
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                       'HOME': str(target/'home'), 'TMPDIR': str(target/'client')}
        digests = {}
        for role, original in policies.items():
            owner.check()
            work = target/('server-work' if role == 'server' else 'client')
            read = work/'canary' if role == 'client' else allowed_read
            kind, address = ('unix', str(gateway)) if role == 'client' else ('tcp', str(ports[1 if role == 'emulator' else 0]))
            policy = _probe_policy(original, sandbox_probe)
            digests[role] = hashlib.sha256(policy.encode()).hexdigest()
            for restricted in (False, True):
                tag = role+('-restricted' if restricted else '-control')
                args = [sandbox_probe.path, str(read), str(work/(tag+'-write')),
                        str(outside_read), str(root/(tag+'-outside-write')), kind, address,
                        str(ports[2]), 'reproof-sandbox-probe-v1']
                sandbox_probe.verify(cancellation=cancellation, deadline=deadline)
                plan.sandbox.verify(cancellation=cancellation, deadline=deadline)
                if restricted: args = [plan.sandbox.path, '-p', policy, *args]
                result = _run(owner, args, environment)
                expected = _EXPECTED if restricted else dict.fromkeys(_EXPECTED, 1)
                _require(result == expected, 'Sandbox sentinel result did not match policy')
        owner.check()
        report = {'schemaVersion': 1, 'kind': 'reproof-private-sandbox-preflight', 'passed': True,
                  'probeDigest': sandbox_probe.sha256, 'probeSourceDigest': PROBE_SOURCE_DIGEST,
                  'sandboxDigest': plan.sandbox.sha256, 'adbDigest': plan.adb.sha256,
                  'sampleDigest': plan.sample_apk.sha256, 'helperDigest': plan.helper_apk.sha256,
                  'birthPlanDigest': _plan_digest(plan),
                  'policyDigests': digests, 'derivativeProbe': True,
                  'actualAdbDiscoveryVerified': False, 'privateDataRetained': True}
        _require(owner.collect(), 'Sandbox probe process collection incomplete')
        return report
    except BaseException:
        if owner is not None:
            owner.collect()
            raise PreflightIncomplete(owner) from None
        raise
    finally:
        os.close(parent_fd)
