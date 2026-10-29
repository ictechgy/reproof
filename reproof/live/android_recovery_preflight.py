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
#define STAGE(name) do { \
    static const char message[] = "REPRO_PREFLIGHT_STAGE " name "\n"; \
    (void)write(STDERR_FILENO,message,sizeof(message)-1); \
} while (0)
int main(int argc,char **argv) {
    STAGE("main-entered");
    if(argc!=9) return 64;
    STAGE("allowed-read"); int ar=opened(argv[1],O_RDONLY);
    STAGE("allowed-write"); int aw=opened(argv[2],O_WRONLY|O_CREAT|O_EXCL);
    STAGE("outside-read"); int dr=opened(argv[3],O_RDONLY);
    STAGE("outside-write"); int dw=opened(argv[4],O_WRONLY|O_CREAT|O_EXCL);
    STAGE("allowed-network"); int an=connected(argv[5],argv[6]);
    STAGE("outside-network"); int dn=connected("tcp",argv[7]);
    STAGE("unlisted-exec");
    pid_t child=0; char *args[]={"/usr/bin/true",NULL};
    int spawned=posix_spawn(&child,args[0],NULL,NULL,args,environ)==0;
    if(spawned) waitpid(child,NULL,0);
    STAGE("result");
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



_PROBE_STAGES = ('main-entered', 'allowed-read', 'allowed-write', 'outside-read',
                 'outside-write', 'allowed-network', 'outside-network',
                 'unlisted-exec', 'result')


def _probe_stages(raw):
    prefix = b'REPRO_PREFLIGHT_STAGE '
    selected = [line[len(prefix):] for line in raw.splitlines() if line.startswith(prefix)]
    expected = [stage.encode('ascii') for stage in _PROBE_STAGES]
    valid = len(selected) <= len(expected) and selected == expected[:len(selected)]
    return {'stageMarkersValid': valid, 'mainEntryObserved': valid and bool(selected),
            'lastProbeStage': _PROBE_STAGES[len(selected)-1] if valid and selected else None}


def _diagnostic(child):
    streams = []
    raw_stderr = b''
    complete = True
    for index in range(2):
        collectors = getattr(child, 'collectors', ())
        collector = collectors[index] if index < len(collectors) else None
        thread = getattr(collector, 'thread', None)
        was_stopped = thread is not None and not thread.is_alive()
        raw = bytes(collector.data[:65537]) if collector is not None else b''
        complete = (complete and was_stopped and not thread.is_alive()
                    and not bool(getattr(collector, 'oversized', False)))
        streams.append({'bytesCaptured': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'truncated': bool(getattr(collector, 'oversized', False))})
        if index == 1:
            raw_stderr = raw
    process = getattr(child, 'process', None)
    code = getattr(process, 'returncode', None)
    code = code if type(code) is int and -4096 <= code <= 4096 else None
    role = getattr(child, '_preflight_role', 'unknown')
    stage = getattr(child, '_preflight_stage', 'start')
    failure = getattr(child, '_preflight_failure', None)
    stage = stage if stage in ('start', 'wait', 'collect', 'validate', 'complete') else 'unknown'
    failure = failure if failure in ('cancelled', 'timeout', 'output-limit', 'collection-incomplete',
        'process-exit', 'start-failure', 'interrupted', 'malformed-output', 'sentinel-mismatch') else None
    restricted = getattr(child, '_preflight_restricted', None)
    restricted = restricted if type(restricted) is bool else None
    return {'role': role if role in ('server', 'emulator', 'client') else 'unknown',
            'restricted': restricted,
            'hostStage': stage, 'failure': failure, 'returncode': code,
            'signal': -code if type(code) is int and code < 0 else None,
            'streamsComplete': bool(complete and code is not None),
            'stdout': streams[0], 'stderr': streams[1], **_probe_stages(raw_stderr)}

class PreflightIncomplete(ContractError):
    def __init__(self, owner):
        super().__init__('Private sandbox preflight incomplete; owned evidence retained')
        self.owner = owner
        self.report = {'schemaVersion': 1, 'kind': 'reproof-private-sandbox-preflight-failure',
                       'checks': [_diagnostic(child) for child in owner.children[:6]],
                       'cleanupIncomplete': bool(owner.cleanup_uncertain),
                       'privateDataRetained': True, 'causeConfirmed': False}


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
    child._preflight_role = getattr(owner, '_preflight_role', 'unknown')
    child._preflight_restricted = getattr(owner, '_preflight_restricted', None)
    child._preflight_stage, child._preflight_failure = 'start', None
    try:
        child.start(argv, environment, owner.root)
        child._preflight_stage = 'wait'
        until = min(owner.deadline, time.monotonic()+5)
        while child.process.poll() is None:
            if owner.cancellation.is_set():
                child._preflight_failure = 'cancelled'
            elif time.monotonic() >= until:
                child._preflight_failure = 'timeout'
            elif not child.bounded():
                child._preflight_failure = 'output-limit'
            _require(child._preflight_failure is None)
            owner.check()
            owner.cancellation.wait(.01)
        child._preflight_stage = 'collect'
        if not child.collect(min(owner.deadline, time.monotonic()+2)):
            child._preflight_failure = 'collection-incomplete'
            _require(False)
        owner.check()
        child._preflight_stage = 'validate'
        if child.process.returncode != 0:
            child._preflight_failure = 'process-exit'
            _require(False)
        if not child.bounded():
            child._preflight_failure = 'output-limit'
            _require(False)
        raw = bytes(child.collectors[0].data)
        _require(len(raw) <= 2048)
        try: value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ContractError('Invalid sandbox probe output') from None
        _require(type(value) is dict and set(value) == set(_EXPECTED)
                 and all(type(x) is int and x in (0, 1) for x in value.values()))
        child._preflight_stage = 'complete'
        return value
    except BaseException:
        if child._preflight_failure is None:
            if owner.cancellation.is_set():
                child._preflight_failure = 'cancelled'
            elif time.monotonic() >= owner.deadline:
                child._preflight_failure = 'timeout'
            else:
                child._preflight_failure = {'start': 'start-failure', 'wait': 'interrupted',
                    'collect': 'collection-incomplete', 'validate': 'malformed-output'}.get(
                        child._preflight_stage, 'interrupted')
        raise


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
                owner._preflight_role, owner._preflight_restricted = role, restricted
                result = _run(owner, args, environment)
                expected = _EXPECTED if restricted else dict.fromkeys(_EXPECTED, 1)
                if result != expected and owner.children:
                    owner.children[-1]._preflight_failure = 'sentinel-mismatch'
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
