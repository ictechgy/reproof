"""Owned executable protocol tests; no CoreDevice service or physical phone."""
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


IDENTIFIER = '11111111-2222-3333-4444-555555555555'
UDID = '00008020-0000000000000001'
BUNDLE = 'com.example.owned'


class PinnedDeviceCtlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='owned-device-tool-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tool = self.root/'devicectl'
        self.requests = self.root/'requests.jsonl'
        self.work = self.root/'work'; self.work.mkdir(mode=0o700)
        self.script = '''import json,os,pathlib,sys,time
args=sys.argv[1:]
with pathlib.Path(REQUESTS).open('a') as out:out.write(json.dumps(args)+'\\n')
destination=pathlib.Path(args[args.index('--json-output')+1])
result={'hardwareProperties':{'deviceType':'iPhone','platform':'iOS','udid':UDID},
        'deviceProperties':{'osVersionNumber':'owned','developerModeStatus':'enabled','ddiServicesAvailable':True},
        'connectionProperties':{'pairingState':'paired','transportType':'wired','tunnelState':'connected','tunnelIPAddress':'fd00::1'},
        'identifier':IDENTIFIER,'unexpectedEnvironment':os.environ.get('OWNED_UNRELATED_VALUE')}
if args[2]=='apps':result={'apps':[{'bundleIdentifier':BUNDLE}]}
elif args[2]=='processes':result={'runningProcesses':[]}
EXTRA
destination.write_text(json.dumps({'info':{'outcome':'success'},'result':result}))
'''
        self.write_tool()

    def write_tool(self, extra='pass'):
        constants = '\n'.join(name+'='+repr(value) for name,value in
            (('REQUESTS',str(self.requests)),('IDENTIFIER',IDENTIFIER),('UDID',UDID),('BUNDLE',BUNDLE)))
        body=constants+'\n'+self.script.replace('EXTRA',extra)
        # The owned Mach-O protocol double embeds its entire Python payload.
        source=self.root/'owned-tool.c'
        source.write_text('#include <stdlib.h>\n#include <unistd.h>\n'
            'int main(int argc, char **argv) {\n'
            'char **args=calloc((size_t)argc+3,sizeof(char*)); if(!args)return 70;\n'
            'args[0]='+json.dumps(sys.executable)+';args[1]="-c";args[2]='+json.dumps(body)+';\n'
            'for(int i=1;i<argc;i++)args[i+2]=argv[i];\n'
            'execv(args[0],args);return 71;\n}\n')
        result=subprocess.run(['/usr/bin/clang','-Wall','-Wextra','-Werror',str(source),'-o',str(self.tool)],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,'Owned protocol double did not compile')
        self.tool.chmod(0o700)

    def client(self):
        from reproloop.ios_device_tools import IOSDeviceTools, PinnedDeviceCtlClient
        result = PinnedDeviceCtlClient(IOSDeviceTools(self.tool,hashlib.sha256(self.tool.read_bytes()).hexdigest()),
            identifier=IDENTIFIER, udid=UDID, bundle=BUNDLE, work_root=self.work)
        self.addCleanup(result.close)
        return result

    def query(self, client, kind='details', **changes):
        return client.query(kind,cancellation=changes.get('cancellation',threading.Event()),
                            deadline_monotonic=changes.get('deadline',time.monotonic()+3))

    def test_selected_tool_and_device_are_used_without_ambient_discovery_or_environment(self):
        client=self.client()
        with patch.dict(os.environ,{'OWNED_UNRELATED_VALUE':'must-not-reach-tool'}):
            observation=self.query(client)
        calls=[json.loads(row) for row in self.requests.read_text().splitlines()]
        self.assertTrue(calls[0][:5]==['device','info','details','--device',IDENTIFIER])
        self.assertIsNone(observation.data['unexpectedEnvironment'])
        public=json.dumps(observation.public())+repr(observation)+repr(client)
        self.assertTrue(IDENTIFIER not in public and UDID not in public and str(self.root) not in public)
        self.assertEqual(observation.public()['executionAuthority'],'none')
        self.assertTrue(observation.public()['hostClientStopped'])
        self.assertFalse(observation.public()['deviceCleanupConfirmed'])
        self.assertEqual(list(self.work.iterdir()),[])

    def test_application_query_rechecks_device_and_fixes_the_bundle_filter(self):
        observation=self.query(self.client(),'apps')
        calls=[json.loads(row) for row in self.requests.read_text().splitlines()]
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[0][:3]==['device','info','details'])
        self.assertTrue(calls[1][:7]==['device','info','apps','--device',IDENTIFIER,'--bundle-id',BUNDLE])
        self.assertTrue(observation.data['apps'][0]['bundleIdentifier']==BUNDLE)

    def test_wrong_device_identity_stops_before_the_following_query(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        self.write_tool("result['hardwareProperties']['udid']='00008020-1111111111111111'")
        with self.assertRaises(IOSDeviceToolError):self.query(self.client(),'apps')
        self.assertEqual(len(self.requests.read_text().splitlines()),1)
        self.assertEqual(list(self.work.iterdir()),[])

    def test_changed_tool_closed_client_and_management_commands_do_not_dispatch(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        client=self.client()
        for kind in ('list','install','pair','manage',('details','--device','foreign')):
            with self.subTest(kind=kind),self.assertRaises(IOSDeviceToolError):self.query(client,kind)
        self.assertFalse(self.requests.exists())
        self.tool.write_bytes(self.tool.read_bytes()+b'changed')
        with self.assertRaises(IOSDeviceToolError):self.query(client)
        self.assertFalse(self.requests.exists())
        client.close()
        with self.assertRaises(IOSDeviceToolError):self.query(client)

    def test_cancellation_collects_owned_client_and_removes_only_its_work(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        self.write_tool('time.sleep(5)')
        client=self.client();cancel=threading.Event()
        timer=threading.Timer(.15,cancel.set);timer.start();self.addCleanup(timer.join)
        with self.assertRaises(IOSDeviceToolError):self.query(client,cancellation=cancel)
        self.assertEqual(client.active_processes,0)
        self.assertEqual(list(self.work.iterdir()),[])

    def test_failure_output_is_bounded_and_not_exposed(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        for extra in ("sys.stderr.write('owned-private-output');sys.exit(1)",
                      "destination.write_bytes(b'x'*300000);sys.exit(0)",
                      "destination.symlink_to(REQUESTS);sys.exit(0)"):
            with self.subTest(extra=extra):
                self.write_tool(extra)
                with self.assertRaises(IOSDeviceToolError) as raised:self.query(self.client())
                self.assertNotIn('owned-private-output',str(raised.exception))

    def test_tool_and_workspace_symlinks_are_rejected(self):
        from reproloop.ios_device_tools import IOSDeviceTools, IOSDeviceToolError, PinnedDeviceCtlClient
        digest=hashlib.sha256(self.tool.read_bytes()).hexdigest()
        alias=self.root/'alias';alias.symlink_to(self.tool)
        with self.assertRaises(IOSDeviceToolError):IOSDeviceTools(alias,digest)
        link=self.root/'work-link';link.symlink_to(self.work)
        with self.assertRaises(IOSDeviceToolError):
            PinnedDeviceCtlClient(IOSDeviceTools(self.tool,digest),identifier=IDENTIFIER,udid=UDID,
                                 bundle=BUNDLE,work_root=link)

    def test_shell_launchers_cannot_run_implicit_tool_installation(self):
        from reproloop.ios_device_tools import IOSDeviceTools, IOSDeviceToolError
        launcher=self.root/'launcher'
        launcher.write_text('#!/bin/sh\nexit 0\n');launcher.chmod(0o700)
        with self.assertRaises(IOSDeviceToolError):
            IOSDeviceTools(launcher,hashlib.sha256(launcher.read_bytes()).hexdigest())

    def test_query_definition_needs_no_workspace_creation_or_device_process(self):
        from reproloop.ios_device_tools import IOSDeviceTools,IOSDeviceQueryDefinition
        missing=self.root/'future-work'
        tools=IOSDeviceTools(self.tool,hashlib.sha256(self.tool.read_bytes()).hexdigest())
        with patch('subprocess.Popen',side_effect=AssertionError('Declaration started a process')):
            declared=IOSDeviceQueryDefinition(tools,IDENTIFIER,UDID,BUNDLE,missing)
        self.assertFalse(missing.exists());self.assertFalse(self.requests.exists())
        missing.mkdir(mode=0o700)
        client=declared.open_client();self.addCleanup(client.close)
        self.assertEqual(client.definition_digest,declared.definition_digest)
        self.assertTrue(UDID not in repr(declared) and IDENTIFIER not in repr(declared) and str(self.root) not in repr(declared))

    def test_expired_bounds_and_pre_cancelled_queries_never_dispatch(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        client=self.client();cancel=threading.Event();cancel.set()
        for arguments in ({'cancellation':cancel},{'deadline':time.monotonic()-1},{'deadline':float('nan')}):
            with self.subTest(arguments=arguments),self.assertRaises(IOSDeviceToolError):self.query(client,**arguments)
        self.assertFalse(self.requests.exists())

    def test_selected_iphone_entry_does_not_fall_back_to_global_discovery(self):
        from reproloop.live.iphone import select_iphone
        from reproloop.live.model import LiveError
        client=self.client()
        with patch('reproloop.live.iphone.discover_iphones',side_effect=AssertionError('Ambient discovery used')):
            device=select_iphone(query_client=client)
            self.assertTrue(device.identifier==IDENTIFIER and device.udid==UDID)
            with self.assertRaises(LiveError):select_iphone('unselected-public-device',query_client=client)

    def test_foreign_application_result_is_rejected(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        self.write_tool("\nif args[2]=='apps':result={'apps':[{'bundleIdentifier':'com.example.foreign'}]}")
        with self.assertRaises(IOSDeviceToolError):self.query(self.client(),'apps')
        self.assertEqual(list(self.work.iterdir()),[])

    def test_unconfirmed_host_collection_keeps_work_until_an_explicit_close_retry(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        from reproloop.repair_android_signing import _ProcessOwner
        client=self.client()
        with patch.object(_ProcessOwner,'_group_empty',return_value=False):
            with self.assertRaises(IOSDeviceToolError):self.query(client)
            self.assertTrue(list(self.work.iterdir()))
            self.assertFalse(client.close())
            self.assertTrue(list(self.work.iterdir()))
        self.assertTrue(client.close())
        self.assertEqual(list(self.work.iterdir()),[])

    def test_unexpected_work_symlink_is_preserved_and_prevents_reuse(self):
        from reproloop.ios_device_tools import IOSDeviceToolError
        self.write_tool('destination.symlink_to(REQUESTS);sys.exit(0)')
        client=self.client()
        with self.assertRaises(IOSDeviceToolError):self.query(client)
        count=len(self.requests.read_text().splitlines())
        with self.assertRaises(IOSDeviceToolError):self.query(client)
        self.assertEqual(len(self.requests.read_text().splitlines()),count)
        self.assertFalse(client.close())
        self.assertTrue(any(path.is_symlink() for path in self.work.glob('*/result.json')))
