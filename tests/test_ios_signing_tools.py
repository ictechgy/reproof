import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


SDK = Path('/Applications/Xcode-27.0.0-beta.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk')


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class IOSSigningToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='owned-ios-tool-build-'); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(); self.output = self.root/'tools'

    def tools(self):
        from reproloop.ios_signing_tools import IOSSigningBuildTools
        return IOSSigningBuildTools(Path('/usr/bin/clang'),sha('/usr/bin/clang'),SDK,sha(SDK/'SDKSettings.json'))

    def build(self, **kwargs):
        from reproloop.ios_signing_tools import build_ios_signing_owner
        return build_ios_signing_owner(self.output,self.tools(),cancellation=kwargs.get('cancellation',threading.Event()),
            deadline_monotonic=kwargs.get('deadline',time.monotonic()+45))

    def command(self, *extra):
        from reproloop.cli import main
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['ios-signing','build-tools','--output-new',str(self.output),
                '--clang','/usr/bin/clang','--clang-sha256',sha('/usr/bin/clang'),
                '--sdk-root',str(SDK),'--sdk-settings-sha256',sha(SDK/'SDKSettings.json'),*extra])
        return code,json.loads(output.getvalue())

    def test_public_cli_builds_loadable_tools_without_opening_recovery_configuration(self):
        from reproloop.ios_signing_tools import load_ios_signing_owner
        previous = {number:signal.getsignal(number) for number in (signal.SIGINT,signal.SIGTERM)}
        with patch('reproloop.ios_signing_cli.load_ios_signing_configuration',
                   side_effect=AssertionError('build must not open a signing journal')):
            code,report = self.command()
        self.assertEqual(code,0)
        self.assertEqual((report['kind'],report['status']),('ios-signing-tools-v1','built'))
        tools = load_ios_signing_owner(self.output,report['manifestDigest'])
        self.assertEqual(tools.definition_digest,report['ownerDefinitionDigest'])
        self.assertEqual(previous,{number:signal.getsignal(number) for number in previous})
        self.assertEqual(list(self.root.glob('.tools.*')),[])

    def test_cli_rejects_invalid_deadline_and_existing_output_with_static_error(self):
        for value in ('0','nan','inf','121'):
            with self.subTest(timeout=value), patch('reproloop.ios_signing_tools._run_fixed') as run:
                code,report = self.command('--timeout-seconds',value)
                self.assertEqual(code,2); self.assertEqual(report['status'],'rejected')
                self.assertTrue(report['cleanupConfirmed']); run.assert_not_called()
                self.assertFalse(self.output.exists())
        self.output.mkdir(); marker=self.output/'preserve'; marker.write_bytes(b'owned')
        code,report = self.command()
        self.assertEqual(code,2); self.assertEqual(report['error']['code'],'ios_signing_tools_output')
        self.assertEqual(marker.read_bytes(),b'owned')

    def test_missing_output_parent_uses_public_build_error(self):
        from reproloop.ios_signing_tools import IOSSigningToolsError
        self.output = self.root/'missing'/'tools'
        with self.assertRaises(IOSSigningToolsError) as caught: self.build()
        self.assertEqual(caught.exception.code,'ios_signing_tools_output')
        self.assertFalse(self.output.parent.exists())

    def sleeping_compiler(self):
        fake = self.root/'clang'; pids = self.root/'owned-processes'
        fake.write_text('#!/usr/bin/python3\nimport os,subprocess,time\n'
            "child=subprocess.Popen(['/bin/sleep','30'])\n"
            f"open({str(pids)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
            'time.sleep(30)\n')
        fake.chmod(0o700)
        return fake,pids

    def assert_processes_removed(self, pids):
        for pid in map(int,pids.read_text().split()):
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.tools.*')),[])

    def test_timeout_collects_real_tool_process_group_and_private_workspace(self):
        from reproloop.ios_signing_tools import IOSSigningBuildTools, IOSSigningToolsError, build_ios_signing_owner
        fake,pids = self.sleeping_compiler()
        tools = IOSSigningBuildTools(fake,sha(fake),SDK,sha(SDK/'SDKSettings.json'))
        started = time.monotonic()
        with self.assertRaises(IOSSigningToolsError) as caught:
            build_ios_signing_owner(self.output,tools,cancellation=threading.Event(),
                deadline_monotonic=time.monotonic()+1)
        self.assertEqual(caught.exception.code,'ios_signing_tools_timeout')
        self.assertTrue(caught.exception.cleanup_confirmed)
        self.assertLess(time.monotonic()-started,4)
        self.assert_processes_removed(pids)

    def test_cli_sigterm_collects_real_tool_process_group_and_private_workspace(self):
        fake,pids = self.sleeping_compiler()
        process = subprocess.Popen([sys.executable,'-m','reproloop','ios-signing','build-tools',
            '--output-new',str(self.output),'--clang',str(fake),'--clang-sha256',sha(fake),
            '--sdk-root',str(SDK),'--sdk-settings-sha256',sha(SDK/'SDKSettings.json')],
            cwd=Path(__file__).resolve().parents[1],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+5
            while not pids.exists() and process.poll() is None and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue(pids.exists())
            process.send_signal(signal.SIGTERM)
            stdout,stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=3)
            process.stdout.close(); process.stderr.close()
        self.assertEqual(process.returncode,2); self.assertEqual(stderr,b'')
        report = json.loads(stdout)
        self.assertEqual(report['error']['code'],'ios_signing_tools_cancelled')
        self.assertTrue(report['cleanupConfirmed'])
        self.assert_processes_removed(pids)

    def test_real_offline_build_loads_exact_native_outputs_and_public_sources(self):
        from reproloop.ios_signing_tools import load_ios_signing_owner
        from reproloop.resources import read_resource
        result = self.build()
        loaded = load_ios_signing_owner(self.output,result.manifest_digest)
        self.assertEqual(loaded.definition_digest,result.tools.definition_digest)
        manifest = json.loads((self.output/'tools-manifest.json').read_bytes())
        for name,digest in manifest['sources'].items(): self.assertEqual(digest,hashlib.sha256(read_resource(name)).hexdigest())
        self.assertEqual(len(manifest['sources']),4)
        self.assertEqual(set(p.name for p in self.output.iterdir()),
            {'tools-manifest.json','ios-signing-owner','ios-process-guardian','ios-code-verifier'})
        for binary in (loaded.signer,loaded.guardian,loaded.verifier):
            ran = subprocess.run([str(binary)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
            self.assertEqual(ran.returncode,64)
        self.assertEqual(list(self.root.glob('.tools.*')),[])

    def test_existing_output_changed_compiler_and_cancelled_build_do_not_start(self):
        from reproloop.ios_signing_tools import IOSSigningBuildTools, IOSSigningToolsError
        with self.assertRaises(IOSSigningToolsError):
            IOSSigningBuildTools(Path('/usr/bin/clang'),'0'*64,SDK,sha(SDK/'SDKSettings.json'))
        self.output.mkdir()
        marker = self.output/'preserve'; marker.write_bytes(b'owned')
        with patch('reproloop.ios_signing_tools._run_fixed') as run:
            with self.assertRaises(IOSSigningToolsError): self.build()
            run.assert_not_called()
        self.assertEqual(marker.read_bytes(),b'owned')
        self.output = self.root/'cancelled'
        stop=threading.Event(); stop.set()
        with patch('reproloop.ios_signing_tools._run_fixed') as run:
            with self.assertRaises(IOSSigningToolsError): self.build(cancellation=stop)
            run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_modified_manifest_or_binary_is_rejected_without_rebuilding(self):
        from reproloop.ios_signing_tools import load_ios_signing_owner, IOSSigningToolsError
        result = self.build()
        with self.assertRaises(IOSSigningToolsError): load_ios_signing_owner(self.output,'0'*64)
        result.tools.guardian.write_bytes(b'changed native output')
        with self.assertRaises(IOSSigningToolsError): load_ios_signing_owner(self.output,result.manifest_digest)
