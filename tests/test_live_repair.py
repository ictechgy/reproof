from pathlib import Path
from contextlib import nullcontext
import json
import plistlib
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from reproloop.core import digest
from reproloop.ios_storage import tree_manifest
from reproloop.ios_cases import case_spec
from reproloop.live.model import Lab, LiveError
from reproloop.live.providers import DemoProvider, demo_device
from reproloop.live.repair_jobs import LiveRepairJobs
from reproloop.storage import write_json


class CaptureFailureProvider(DemoProvider):
    record_sdk = True
    fixture = 'counter'
    capture_started_at = 0

    def collect_sdk_capture(self):
        raise LiveError('capture_invalid', 'Synthetic capture failure')


class LiveRepairBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'Fixture.swift').write_text('let fixture = true')
        self.build = self.root / 'build'
        products = self.build / 'DerivedData/Build/Products'
        app = products / 'Debug-iphoneos/ReproSample.app'
        app.mkdir(parents=True)
        (app / 'fixture').write_text('synthetic fixture')
        (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'io.reproloop.sample.ios','ReproBuildID':'fixture-build'}))
        self.identity = {'bundle': 'io.reproloop.sample.ios', 'artifactDigest': digest(tree_manifest(app))}
        write_json(self.build / 'receipt.json', {'executionEnvironment': 'physical-iphone', 'signed': True,
            'buildCompleted':True, 'buildId':'fixture-build',
            'sourceDigest': digest(tree_manifest(self.source, True)), 'productsDigest': digest(tree_manifest(products)),
            'appRelative': 'Debug-iphoneos/ReproSample.app'})
        def factory():
            provider = CaptureFailureProvider()
            provider.identity = self.identity
            return provider
        device = demo_device()
        device['factory'] = factory
        device['capabilities']['applicationIdentity'] = self.identity
        self.lab = Lab([device], self.root / 'live')
        self.s = self.lab.create_session('demo', 'owner', 'controller')
        self.s = self.lab.get_session(self.s['id'], 'owner')
        self.runner = Mock(side_effect=AssertionError('No repair process may run before valid capture'))
        self.repairs = LiveRepairJobs(self.lab, self.source, self.build, runner_factory=self.runner)
        self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'], True)
        self.send('original')
        self.record = self.lab.stop_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])

    def tearDown(self):
        self.repairs.close()
        self.lab.close_all()
        self.temp.cleanup()

    def send(self, identifier):
        s = self.lab.get_session(self.s['id'])
        frame = self.lab.frame(s['id'])
        self.lab.input(s['id'], 'owner', {'controllerId': s['controllerId'], 'epoch': s['epoch'],
            'sequence': s['lastSequence'] + 1, 'commandId': identifier, 'frameId': frame['id'],
            'geometryVersion': frame['geometryVersion'], 'action': 'tap', 'payload': {'x': .5, 'y': .5}})

    def submit(self, request='request', epoch=None):
        return self.repairs.submit(self.s['id'], 'owner', 'controller', self.s['epoch'] if epoch is None else epoch,
                                   self.record['id'], request)

    def test_capture_failure_restores_manual_control_without_releasing_live_session(self):
        job = self.submit()
        self.repairs.workers[job['id']].join(timeout=5)
        result = self.repairs.get(job['id'], 'owner')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['errorCode'], 'capture_invalid')
        self.assertEqual(self.lab.get_session(self.s['id'])['controllerId'], 'controller')
        self.assertEqual(self.lab.get_session(self.s['id'])['state'], 'active')
        self.runner.assert_not_called()
        self.assertEqual(self.submit()['id'], job['id'])
        with self.assertRaises(LiveError):self.repairs.get(job['id'], 'other-owner')
        with self.assertRaises(LiveError):self.repairs.resume(job['id'], 'owner', 'controller')

    def test_rejects_input_after_recording_and_stale_control(self):
        with self.assertRaises(LiveError):self.submit(epoch=99)
        self.send('later-input')
        with self.assertRaises(LiveError) as caught:self.submit()
        self.assertEqual(caught.exception.code, 'recording_changed')
        self.runner.assert_not_called()

    def test_source_change_is_rejected_before_controller_handoff(self):
        (self.source / 'Fixture.swift').write_text('let changed = true')
        with self.assertRaises(LiveError):self.submit()
        self.assertEqual(self.lab.get_session(self.s['id'])['controllerId'], 'controller')

    def test_controller_handoff_does_not_hold_session_lock_during_pointer_cleanup(self):
        provider = self.lab._session(self.s['id'])['provider']
        entered = threading.Event()
        release = threading.Event()
        original_execute = provider.execute

        def execute(action, payload, frame=None):
            if action == 'pointer' and payload.get('phase') == 'cancel':
                entered.set()
                release.wait(2)
            return original_execute(action, payload)

        provider.execute = execute
        state = self.lab._session(self.s['id'])
        with state['lock']:
            state['activePointers'][1] = {
                'x': .5, 'y': .5, 'geometryVersion': state['geometryVersion'],
            }
            state['pointerLastActivity'][1] = time.monotonic()
        result = []
        submitter = threading.Thread(target=lambda: result.append(self.submit()), daemon=True)
        submitter.start()
        self.assertTrue(entered.wait(1))
        inspected = []
        observer = threading.Thread(
            target=lambda: inspected.append(self.lab.get_session(self.s['id'])), daemon=True)
        observer.start()
        observer.join(1)
        self.assertFalse(observer.is_alive())
        self.assertEqual(inspected[0]['state'], 'active')
        release.set()
        submitter.join(2)
        self.assertFalse(submitter.is_alive())
        worker = self.repairs.workers[result[0]['id']]
        worker.join(5)

    def test_cancel_after_handoff_interrupts_child_and_releases_reservation(self):
        provider=self.lab._session(self.s['id'])['provider']
        provider.collect_sdk_capture=lambda:case_spec('counter').capture()
        original_close = provider.close
        bridge_completed = threading.Event()
        def close_with_native_bridge():
            # Simulator's final native request is handled by another HTTP thread.
            def final_request():
                self.lab._session(self.s['id'])
                bridge_completed.set()
            threading.Thread(target=final_request, daemon=True).start()
            if not bridge_completed.wait(1):
                raise LiveError('cleanup_uncertain', 'Native bridge was blocked during close')
            with self.assertRaises(LiveError):
                self.lab.create_session('demo', 'owner', 'racing-controller')
            original_close()
        provider.close = close_with_native_bridge
        class Process:
            pid=424242
            returncode=None
            def poll(self):return self.returncode
            def wait(self,timeout=None):return self.returncode
        process=Process()
        def start(*args,**kwargs):
            self.assertEqual(self.lab.list_devices()[0]['state'],'repairing')
            with self.assertRaises(LiveError):self.lab.create_session('demo','owner','other-controller')
            identifier=next(iter(self.repairs.jobs))
            self.repairs.cancel_events[identifier].set()
            return process
        self.repairs.runner_factory=start
        def interrupt(pid,signum):
            self.assertEqual(pid,process.pid)
            self.assertEqual(signum,signal.SIGINT)
            process.returncode=130
        phone=Mock();phone.lease.return_value=nullcontext()
        observation=Mock(returncode=0,stdout=json.dumps([{'counter':True,'observedCount':'2'}]).encode())
        with patch('reproloop.live.repair_jobs.subprocess.run',return_value=observation),\
             patch('reproloop.live.repair_jobs.os.killpg',side_effect=interrupt) as kill,\
             patch('reproloop.ios_device.IosPhysicalDevice',return_value=phone):
            job=self.submit()
            self.repairs.workers[job['id']].join(timeout=5)
        self.assertEqual(self.repairs.get(job['id'],'owner')['state'],'cancelled')
        self.assertEqual(self.lab.list_devices()[0]['state'],'available')
        self.assertEqual(self.lab.get_session(self.s['id'])['state'],'closed')
        self.assertTrue(bridge_completed.is_set())
        kill.assert_called_once();phone.stop.assert_called_once()

    def test_verified_history_cannot_resume_an_unconfigured_candidate(self):
        identifier='a'*32
        self.repairs.jobs[identifier]={'id':identifier,'owner':'owner','state':'verified','deviceId':'demo',
                                     'candidateIdentity':{'artifactDigest':'different'}}
        with self.assertRaises(LiveError) as caught:self.repairs.resume(identifier,'owner','controller')
        self.assertEqual(caught.exception.code,'app_changed')


if __name__ == '__main__':
    unittest.main()
