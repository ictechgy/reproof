"""Actual native lock retention and parent-loss behavior for iOS signing."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from reproof.resources import read_resource


SDK = Path(os.environ.get('MACOSX_SDK_PATH') or subprocess.run(['xcrun','--sdk','macosx','--show-sdk-path'],capture_output=True,text=True,check=True).stdout.strip())


class IOSNativeSigningOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-native-signing-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / 'operation'; self.work.mkdir(mode=0o700)
        self.descriptors = []
        self.process = None
        self.writer = None
        self.addCleanup(self.close_owned)

    def close_owned(self):
        if self.writer is not None:
            os.close(self.writer); self.writer = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGCONT)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=3)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None: stream.close()
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()

    def build(self):
        for name in ('main.c', 'ownership.h'):
            (self.root / name).write_bytes(read_resource('native/ios-signing-owner/' + name))
        self.binary = self.root / 'signing-owner'
        result = subprocess.run(['/usr/bin/clang', '-std=c11', '-fblocks', '-Wall', '-Wextra', '-Werror',
            '-mmacosx-version-min=15.0', '-isysroot', str(SDK), str(self.root/'main.c'),
            '-framework', 'Security', '-framework', 'CoreFoundation', '-o', str(self.binary)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def prepare(self, *, changes=None):
        config = {'schemaVersion': '1', 'mode': 'liveness-probe', 'operationId': 'owned_signing',
            'requestDigest': 'a'*64, 'contextDigest': 'b'*64, 'scopeDigest': 'c'*64,
            'definitionDigest': 'd'*64, 'workPath': str(self.work), 'appRelativePath': 'App.app',
            'certificateSha256': 'e'*64, 'teamId': 'OWNEDTEAM1', 'certificateChain': [], 'codeObjects': []}
        config.update(changes or {})
        configuration = self.work/'request.plist'
        configuration.write_bytes(plistlib.dumps(config, fmt=plistlib.FMT_BINARY)); configuration.chmod(0o600)
        def descriptor(path, flags):
            result = os.open(path, flags | os.O_NOFOLLOW)
            self.descriptors.append(result)
            return result
        request = descriptor(configuration, os.O_RDONLY)
        directory = descriptor(self.work, os.O_RDONLY | os.O_DIRECTORY)
        files = {}
        for name in ('producer.lock', 'owner.lock', 'start.json', 'termination.json'):
            path = self.work/name
            path.touch(mode=0o600)
            files[name] = descriptor(path, os.O_RDWR)
        for name in ('producer.lock', 'owner.lock'):
            fcntl.flock(files[name], fcntl.LOCK_EX | fcntl.LOCK_NB)
        reader, self.writer = os.pipe(); self.descriptors.append(reader)
        self.arguments = [request, directory, files['producer.lock'], files['owner.lock'], reader,
                          0, 0, files['start.json'], files['termination.json']]
        return files

    def start(self):
        directories = ('/System/Library', '/usr/lib', '/dev/fd', '/System/Volumes/Preboot/Cryptexes/OS',
                       '/System/Cryptexes/OS', str(self.work))
        files = ('/', '/System', '/System/Volumes', '/System/Volumes/Preboot',
            '/System/Volumes/Preboot/Cryptexes', '/System/Cryptexes', '/dev/null',
            '/dev/random', '/dev/urandom', str(self.binary))
        readable = (' '.join('(subpath '+json.dumps(path)+')' for path in directories)
            + ' ' + ' '.join('(literal '+json.dumps(path)+')' for path in files))
        profile = ('(version 1) (allow default) (deny network*) (deny mach-lookup) '
            '(deny process-fork process-exec) (allow process-exec (literal '+json.dumps(str(self.binary))+')) '
            '(deny file-read* file-write*) (allow file-read* '+readable+') '
            '(allow file-read-metadata) (deny file-read-data file-read-xattr '
            '(subpath "/System/Library/Keychains") '
            '(subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/Keychains")) '
            '(allow file-write* (subpath '+json.dumps(str(self.work/'App.app'))+') '
            '(literal '+json.dumps(str(self.work))+') '
            '(literal '+json.dumps(str(self.work/'start.json'))+') '
            '(literal '+json.dumps(str(self.work/'termination.json'))+'))')
        self.process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile,
            str(self.binary), *map(str, self.arguments)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=tuple(item for item in self.arguments if item >= 3),
            cwd=self.work, env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()

    def wait_started(self):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            body = (self.work/'start.json').read_bytes()
            if body:
                try:
                    record = json.loads(body)
                except ValueError:
                    pass
                else:
                    self.assertEqual(record['contextDigest'], 'b'*64)
                    self.assertEqual(record['state'], 'started')
                    return record
            if self.process.poll() is not None:
                self.fail('native owner exited before start')
            time.sleep(.01)
        self.fail('native owner did not start')

    def lock_available(self, name):
        descriptor = os.open(self.work/name, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        finally:
            os.close(descriptor)

    def test_parent_eof_terminates_owner_and_releases_both_native_locks(self):
        self.build(); self.prepare(); self.start(); self.wait_started()
        self.assertFalse(self.lock_available('producer.lock'))
        self.assertFalse(self.lock_available('owner.lock'))
        os.close(self.writer); self.writer = None
        self.assertEqual(self.process.wait(timeout=3), 75)
        self.assertTrue(self.lock_available('producer.lock'))
        self.assertTrue(self.lock_available('owner.lock'))
        self.assertEqual(self.process.stdout.read(), b'')

    def test_parent_eof_before_configuration_does_not_leave_a_native_owner(self):
        self.build(); self.prepare(); self.start()
        os.close(self.writer); self.writer = None
        self.assertEqual(self.process.wait(timeout=3), 75)
        self.assertTrue(self.lock_available('producer.lock'))
        self.assertTrue(self.lock_available('owner.lock'))
        self.assertEqual(self.process.stdout.read(), b'')

    def test_stop_keeps_ownership_and_early_ack_cannot_complete_a_probe(self):
        self.build(); self.prepare(); self.start(); self.wait_started()
        self.process.send_signal(signal.SIGSTOP)
        self.assertFalse(self.lock_available('owner.lock'))
        os.write(self.writer, b'\x01')
        self.process.send_signal(signal.SIGCONT)
        self.assertEqual(self.process.wait(timeout=3), 76)
        self.assertEqual((self.work/'termination.json').read_bytes(), b'')
        self.assertTrue(self.lock_available('owner.lock'))

    def test_replaced_lock_and_unknown_request_fields_fail_before_start(self):
        self.build(); self.prepare(changes={'command': 'unapproved'})
        self.start(); self.assertEqual(self.process.wait(timeout=3), 64)
        self.assertEqual((self.work/'start.json').read_bytes(), b'')
        self.close_owned(); self.process = None
        for path in self.work.iterdir(): path.unlink()
        self.prepare()
        (self.work/'owner.lock').rename(self.work/'original-owner.lock')
        (self.work/'owner.lock').touch(mode=0o600)
        self.start(); self.assertEqual(self.process.wait(timeout=3), 64)
        self.assertEqual((self.work/'start.json').read_bytes(), b'')

    def test_nonstring_version_and_embedded_null_identifiers_cannot_start(self):
        self.build()
        for number, changes in enumerate(({'schemaVersion': 1.0}, {'operationId': 'owned\0suffix'})):
            with self.subTest(changes=changes):
                self.work = self.root / ('case-' + str(number)); self.work.mkdir(mode=0o700)
                self.prepare(changes=changes); self.start()
                try:
                    exit_code = self.process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    exit_code = None
                self.close_owned(); self.process = None
                self.assertEqual(exit_code, 64)
                self.assertEqual((self.work/'start.json').read_bytes(), b'')
                for path in self.work.iterdir(): path.unlink()

    def test_failed_signing_keeps_its_locks_until_the_parent_collects_exit(self):
        self.build(); self.prepare(changes={'mode': 'sign'})
        path = self.root/'owned-invalid-pkcs12'; path.write_bytes(b'owned invalid input'); path.chmod(0o600)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        reader, writer = os.pipe(); os.write(writer, b'owned-password'); os.close(writer)
        self.descriptors += [descriptor, reader]; self.arguments[5:7] = [descriptor, reader]
        self.start(); self.wait_started()
        self.assertTrue(select.select([self.process.stdout], [], [], 5)[0])
        report = json.loads(self.process.stdout.readline(4096))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['signedCodeObjects'], 0)
        self.assertIsNone(self.process.poll())
        self.assertFalse(self.lock_available('producer.lock'))
        self.assertFalse(self.lock_available('owner.lock'))
        os.write(self.writer, b'\x01'); os.close(self.writer); self.writer = None
        self.assertEqual(self.process.wait(timeout=3), 1)
        self.assertTrue(self.lock_available('producer.lock'))
        self.assertTrue(self.lock_available('owner.lock'))

    def test_real_parent_exit_leaves_no_native_owner_holding_the_operation(self):
        self.build()
        script = r'''
from pathlib import Path
import fcntl,json,os,plistlib,subprocess,sys,time
work=Path(sys.argv[1]); binary=sys.argv[2]
config={'schemaVersion':'1','mode':'liveness-probe','operationId':'owned_signing',
 'requestDigest':'a'*64,'contextDigest':'b'*64,'scopeDigest':'c'*64,'definitionDigest':'d'*64,
 'workPath':str(work),'appRelativePath':'App.app','certificateSha256':'e'*64,'teamId':'OWNEDTEAM1',
 'certificateChain':[],'codeObjects':[]}
path=work/'request.plist';path.write_bytes(plistlib.dumps(config));path.chmod(0o600)
request=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
directory=os.open(work,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
fds=[]
for name in ('producer.lock','owner.lock','start.json','termination.json'):
 fd=os.open(work/name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);fds.append(fd)
for fd in fds[:2]:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
reader,writer=os.pipe()
args=[request,directory,fds[0],fds[1],reader,0,0,fds[2],fds[3]]
process=subprocess.Popen([binary,*map(str,args)],pass_fds=tuple(fd for fd in args if fd>=3),
 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,cwd=work)
for fd in (request,directory,*fds,reader):os.close(fd)
deadline=time.monotonic()+5
while time.monotonic()<deadline and process.poll() is None:
 if (work/'start.json').stat().st_size:os._exit(73)
 time.sleep(.01)
os.close(writer)
try:process.wait(timeout=3)
except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
raise SystemExit(74)
'''
        result = subprocess.run([sys.executable, '-I', '-c', script, str(self.work), str(self.binary)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 73)
        deadline = time.monotonic()+6
        while time.monotonic() < deadline:
            if self.lock_available('producer.lock') and self.lock_available('owner.lock'):
                break
            time.sleep(.01)
        self.assertTrue(self.lock_available('producer.lock'))
        self.assertTrue(self.lock_available('owner.lock'))
        self.assertEqual((self.work/'termination.json').read_bytes(), b'')

    def test_actual_app_signature_retains_locks_until_acknowledged_exit(self):
        self.build()
        from reproof.ios_artifact_staging import stage_ios_artifact
        from reproof.ios_artifact_transfer import parse_ios_artifact
        source = Path(__file__).resolve().parents[1] / ('artifacts/product-delivery/d1-uikit-r1/'
            'original-release/DerivedData/Build/Products/Release-iphonesimulator/Inventory.app')
        selected = parse_ios_artifact(source)
        configuration = self.root/'openssl.cnf'
        configuration.write_text('[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=code\n'
            '[dn]\nCN=Owned Native Signing Test\nOU=OWNEDTEAM1\n'
            '[code]\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\n'
            'extendedKeyUsage=critical,codeSigning\n')
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'OPENSSL_CONF': str(configuration)}
        def crypto(*arguments, pass_fds=()):
            result = subprocess.run(['/usr/bin/openssl', *map(str, arguments)], env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=pass_fds, timeout=15)
            self.assertEqual(result.returncode, 0, 'owned certificate setup failed')
            return result.stdout
        crypto('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '1',
            '-config', configuration, '-keyout', self.root/'owned.key', '-out', self.root/'owned.pem')
        (self.root/'owned.key').chmod(0o600)
        certificate = crypto('x509', '-in', self.root/'owned.pem', '-outform', 'DER')
        password = secrets.token_hex(24).encode()
        reader, writer = os.pipe()
        try:
            os.write(writer, password); os.close(writer); writer = None
            crypto('pkcs12', '-export', '-inkey', self.root/'owned.key', '-in', self.root/'owned.pem',
                '-out', self.root/'owned.p12', '-passout', 'fd:'+str(reader), pass_fds=(reader,))
        finally:
            os.close(reader)
            if writer is not None: os.close(writer)
        (self.root/'owned.p12').chmod(0o600)
        entitlements = {'application-identifier': 'OWNEDTEAM1.com.example.reproinventory', 'get-task-allow': False}
        self.prepare(changes={'mode': 'sign', 'certificateSha256': hashlib.sha256(certificate).hexdigest(),
            'certificateChain': [certificate], 'codeObjects': [{'bundlePath': '.',
            'bundleId': 'com.example.reproinventory', 'entitlements': plistlib.dumps(entitlements)}]})
        stage_ios_artifact(selected, self.work/'App.app')
        key_fd = os.open(self.root/'owned.p12', os.O_RDONLY | os.O_NOFOLLOW)
        password_fd, password_writer = os.pipe()
        os.write(password_writer, password); os.close(password_writer)
        self.descriptors += [key_fd, password_fd]
        self.arguments[5:7] = [key_fd, password_fd]
        self.start(); self.wait_started()
        self.assertTrue(select.select([self.process.stdout], [], [], 10)[0])
        report = json.loads(self.process.stdout.readline(4096))
        self.assertEqual(report['status'], 'succeeded')
        self.assertEqual(report['signedCodeObjects'], 1)
        self.assertIsNone(self.process.poll())
        self.assertFalse(self.lock_available('owner.lock'))
        termination = json.loads((self.work/'termination.json').read_bytes())
        self.assertEqual(termination['recordMeaning'], 'exit-intent')
        self.assertEqual(termination['state'], 'succeeded')
        os.write(self.writer, b'\x01'); os.close(self.writer); self.writer = None
        self.assertEqual(self.process.wait(timeout=3), 0)
        self.assertTrue(self.lock_available('owner.lock'))
        self.assertTrue(self.lock_available('producer.lock'))
        self.assertNotEqual(parse_ios_artifact(self.work/'App.app').app_digest, selected.app_digest)
        self.assertEqual(parse_ios_artifact(source).app_digest, selected.app_digest)
        verifier_source = self.root/'verify.c'; verifier_source.write_bytes(read_resource('native/ios-code-verifier/main.c'))
        verifier = self.root/'code-verifier'
        compiled = subprocess.run(['/usr/bin/clang', '-isysroot', str(SDK), str(verifier_source),
            '-framework', 'Security', '-framework', 'CoreFoundation', '-o', str(verifier)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(compiled.returncode, 0)
        from reproof.ios_code_signature import IOSCodeSignatureInspector, IOSCodeSignatureTools
        checksum = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        tools = IOSCodeSignatureTools(Path('/usr/bin/codesign'), checksum('/usr/bin/codesign'),
            checksum('/usr/bin/sandbox-exec'), verifier, checksum(verifier))
        inspector = IOSCodeSignatureInspector(tools, self.root/'inspection', signature_kind='identity',
            expected_certificate_sha256=hashlib.sha256(certificate).hexdigest(), expected_team_id='OWNEDTEAM1',
            bundle_policies={'.': {'bundleId': 'com.example.reproinventory', 'entitlements': entitlements}})
        self.addCleanup(lambda: inspector.close(deadline_monotonic=time.monotonic()+5))
        import threading
        artifact = parse_ios_artifact(self.work/'App.app')
        proof = inspector.inspect(artifact, context_digest='b'*64, cancellation=threading.Event(),
            deadline_monotonic=time.monotonic()+15)
        inspected = inspector.require_verified(proof, artifact, context_digest='b'*64)
        self.assertEqual(len(inspected['codeObjects'][0]['architectures']), 2)
        self.close_owned(); self.process = None
        self.work = self.root/'partial-operation'; self.work.mkdir(mode=0o700)
        self.prepare(changes={'mode': 'sign', 'certificateSha256': hashlib.sha256(certificate).hexdigest(),
            'certificateChain': [certificate], 'codeObjects': [
                {'bundlePath': '.', 'bundleId': 'com.example.reproinventory', 'entitlements': plistlib.dumps(entitlements)},
                {'bundlePath': '.', 'bundleId': 'com.example.wrong', 'entitlements': plistlib.dumps(entitlements)}]})
        stage_ios_artifact(selected, self.work/'App.app')
        key_fd = os.open(self.root/'owned.p12', os.O_RDONLY | os.O_NOFOLLOW)
        password_fd, password_writer = os.pipe()
        os.write(password_writer, password); os.close(password_writer)
        self.descriptors += [key_fd, password_fd]; self.arguments[5:7] = [key_fd, password_fd]
        self.start(); self.wait_started()
        self.assertTrue(select.select([self.process.stdout], [], [], 10)[0])
        partial = json.loads(self.process.stdout.readline(4096))
        self.assertEqual(partial['status'], 'failed')
        self.assertEqual(partial['signedCodeObjects'], 1)
        self.assertFalse(self.lock_available('owner.lock'))
        os.write(self.writer, b'\x01'); os.close(self.writer); self.writer = None
        self.assertEqual(self.process.wait(timeout=3), 1)
        self.assertNotEqual(parse_ios_artifact(self.work/'App.app').app_digest, selected.app_digest)
        self.assertEqual(parse_ios_artifact(source).app_digest, selected.app_digest)
