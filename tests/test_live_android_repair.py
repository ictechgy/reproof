from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reproloop.core import digest
from reproloop.live.model import Lab, LiveError
from reproloop.live.providers import DemoProvider, demo_device
from reproloop.live.repair_jobs import LiveRepairJobs
from reproloop.orchestrator import PRODUCT_FILE
from reproloop.repair import snapshot_source
from reproloop.storage import load_bundle, read_json, sha_file, write_json
from tests.test_core import capture, run


class AndroidCaptureProvider(DemoProvider):
    record_sdk = True
    fixture = 'counter'
    capture_started_at = 0

    def collect_sdk_capture(self):
        return capture()

    def observe(self):
        return {'ready': True, 'nodes': [{'id': 'count', 'text': '2', 'visible': True}]}


class AndroidLiveRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.product = self.source / PRODUCT_FILE
        self.product.parent.mkdir(parents=True)
        self.product.write_text('fun increment() = 2')
        self.build = self.root / 'build'
        self.build.mkdir()
        (self.build / 'original.apk').write_bytes(b'original')
        (self.build / 'driver.apk').write_bytes(b'protected driver')
        self.manifest = snapshot_source(self.source)
        self.receipt = {'schemaVersion': 1, 'platform': 'android', 'variant': 'buggy',
                        'buildCompleted': True, 'sourceDigest': digest(self.manifest),
                        'sourceFiles': self.manifest, 'buildTask': ':sample:assembleBuggyDebug',
                        'apkSha256': sha_file(self.build / 'original.apk'),
                        'driverSha256': sha_file(self.build / 'driver.apk')}
        self.receipt['driverProof'] = dict(self.receipt, apkSha256=self.receipt['driverSha256'],
                                          buildTask=':driver:assembleDebug')
        write_json(self.build / 'receipt.json', self.receipt)
        self.identity = {'bundle': 'io.reproloop.sample', 'artifactDigest': self.receipt['apkSha256']}
        self.phone = Mock()
        self.phone.serial = 'synthetic'
        self.phone.identity = 'synthetic-hash'
        self.phone.lease.return_value = nullcontext()
        self.phone.installation_proof.return_value = {'installedVerified': True}
        self.provider = AndroidCaptureProvider()
        self.provider.identity = self.identity
        self.provider.device = self.phone
        self.provider.sample_apk = self.build / 'original.apk'
        self.provider.helper_apk = self.build / 'helper.apk'
        self.provider.helper_apk.write_bytes(b'helper')
        device = demo_device()
        device['factory'] = lambda: self.provider
        device['capabilities']['applicationIdentity'] = self.identity
        self.lab = Lab([device], self.root / 'live')
        self.s = self.lab.create_session('demo', 'owner', 'controller')
        self.runner = Mock(side_effect=AssertionError('Unexpected child process'))
        self.repairs = LiveRepairJobs(self.lab, self.source, self.build,
                                      platform='android', runner_factory=self.runner)
        self.lab.start_recording(self.s['id'], 'owner', 'controller', self.s['epoch'], True)
        self.send('original')
        self.record = self.lab.stop_recording(self.s['id'], 'owner', 'controller', self.s['epoch'])

    def tearDown(self):
        if hasattr(self, 'repairs'):
            self.repairs.close()
        self.lab.close_all()
        self.temp.cleanup()

    def send(self, identifier):
        current = self.lab.get_session(self.s['id'])
        frame = self.lab.frame(current['id'])
        self.lab.input(current['id'], 'owner', {'controllerId': current['controllerId'],
            'epoch': current['epoch'], 'sequence': current['lastSequence'] + 1,
            'commandId': identifier, 'frameId': frame['id'], 'geometryVersion': frame['geometryVersion'],
            'action': 'tap', 'payload': {'x': .5, 'y': .5}})

    def submit(self, epoch=None):
        return self.repairs.submit(self.s['id'], 'owner', 'controller',
            self.s['epoch'] if epoch is None else epoch, self.record['id'], 'request')

    def finished(self):
        job = self.submit()
        self.repairs.workers[job['id']].join(timeout=5)
        self.assertFalse(self.repairs.workers[job['id']].is_alive())
        return self.repairs.get(job['id'], 'owner')

    def test_capture_failure_restores_live_control(self):
        self.provider.collect_sdk_capture = Mock(side_effect=LiveError('capture_invalid', 'Incomplete'))
        job = self.finished()
        self.assertEqual((job['state'], job['errorCode']), ('failed', 'capture_invalid'))
        current = self.lab.get_session(self.s['id'])
        self.assertEqual((current['state'], current['controllerId']), ('active', 'controller'))
        self.runner.assert_not_called()
        self.assertEqual(self.submit()['id'], job['id'])
        with self.assertRaises(LiveError):
            self.repairs.get(job['id'], 'someone-else')

    def test_source_apk_and_driver_changes_rejected_before_claim(self):
        for path in [self.product, self.build / 'original.apk', self.build / 'driver.apk']:
            original = path.read_bytes()
            path.write_bytes(b'changed')
            with self.subTest(path=path.name), self.assertRaises(LiveError):
                self.submit()
            path.write_bytes(original)
            self.assertEqual(self.lab.get_session(self.s['id'])['controllerId'], 'controller')
        self.runner.assert_not_called()

    def test_stale_controller_and_additional_input_rejected(self):
        with self.assertRaises(LiveError):
            self.submit(epoch=999)
        self.send('later')
        with self.assertRaises(LiveError) as error:
            self.submit()
        self.assertEqual(error.exception.code, 'recording_changed')

    def test_reset_after_recording_rejected(self):
        self.provider.capture_started_at = self.record['startedAt'] + 1
        with self.assertRaises(LiveError) as error:
            self.submit()
        self.assertEqual(error.exception.code, 'recording_changed')

    def test_inconclusive_screen_does_not_launch_repair(self):
        self.provider.observe = lambda: {'ready': True, 'nodes': [{'id': 'count', 'text': '1', 'visible': True}]}
        job = self.finished()
        self.assertEqual(job['errorCode'], 'analysis_inconclusive')
        self.assertEqual(self.lab.get_session(self.s['id'])['state'], 'active')
        self.runner.assert_not_called()

    def test_cancel_after_handoff_releases_device(self):
        class Process:
            pid = 424242
            returncode = None
            def poll(self): return self.returncode
            def wait(self, timeout=None): return self.returncode
        process = Process()
        def start(command, **kwargs):
            self.assertEqual(self.lab.list_devices()[0]['state'], 'repairing')
            with self.assertRaises(LiveError):
                self.lab.create_session('demo', 'owner', 'other')
            self.assertIn('--driver-sha256', command)
            self.assertEqual(command[3], 'repair')
            self.repairs.cancel_events[next(iter(self.repairs.jobs))].set()
            return process
        def interrupt(*args): process.returncode = 130
        self.repairs.runner_factory = start
        with patch('reproloop.live.repair_jobs.os.killpg', side_effect=interrupt):
            job = self.finished()
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(self.lab.list_devices()[0]['state'], 'available')
        self.assertEqual(self.lab.get_session(self.s['id'])['state'], 'closed')
        self.phone.stop.assert_called_once()

    def test_unverified_result_never_configures_candidate(self):
        def start(command, **kwargs):
            output = Path(command[command.index('--output') + 1])
            write_json(output / 'job.json', {'status': 'verified', 'attempts': [], 'runs': []})
            return Mock(poll=Mock(return_value=0), returncode=0)
        self.repairs.runner_factory = start
        job = self.finished()
        self.assertEqual(job['state'], 'failed')
        self.assertNotIn('repairVerified', self.lab.list_devices()[0]['capabilities'])
        with self.assertRaises(LiveError):
            self.repairs.resume(job['id'], 'owner', 'controller')

    def verified_process(self, command, **kwargs):
        output = Path(command[command.index('--output') + 1])
        candidate_source = output / 'attempt-1/source'
        candidate_source.mkdir(parents=True)
        product = candidate_source / PRODUCT_FILE
        product.parent.mkdir(parents=True)
        product.write_text('fun increment() = 1')
        from reproloop.orchestrator import APK_RELATIVE
        self.candidate_apk = candidate_source / APK_RELATIVE
        self.candidate_apk.parent.mkdir(parents=True)
        self.candidate_apk.write_bytes(b'patched')
        proof = {'buildCompleted': True, 'apkSha256': sha_file(self.candidate_apk),
                 'sourceDigest': digest(snapshot_source(candidate_source)), 'protectedDigest': digest({})}
        write_json(output / 'attempt-1/build-receipt.json', proof)
        baseline, verified = [], []
        bundle = load_bundle(output.parent / 'bundle')
        for kind, destination in [('bug', baseline), ('expected', verified)]:
            for _ in range(3):
                evidence = run(kind)
                runner = {'apkSha256': self.receipt['driverSha256'], 'installedVerified': True,
                          'deviceId': self.phone.identity}
                evidence['runner'] = dict(runner, before=runner, after=runner)
                evidence.update(bundleDigest=bundle['manifestDigest'], scenarioDigest=bundle['scenario']['scenarioDigest'],
                                installation={'deviceId': self.phone.identity, 'installedVerified': True,
                                              'fixtureVerified': True,
                                              'apkSha256': self.receipt['apkSha256'] if kind == 'bug' else proof['apkSha256']})
                destination.append(evidence)
        write_json(output / 'baseline/result.json', {'status': 'reproduced', 'repeats': 3, 'runs': baseline,
            'apkSha256': self.receipt['apkSha256'], 'bundleDigest': bundle['manifestDigest']})
        write_json(output / 'attempt-1/verification/result.json',
                   {'status': 'verified', 'repeats': 3, 'runs': verified, 'apkSha256': proof['apkSha256'],
                    'bundleDigest': bundle['manifestDigest']})
        write_json(output / 'job.json', {'status': 'verified', 'agent': 'claude',
            'attempts': [{'attemptId': 1, 'status': 'verified',
                          'agentReceipt': {'provider': 'claude', 'status': 'completed'},
                          'regressionTests': {'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0}}],
            'runs': baseline + verified})
        return Mock(poll=Mock(return_value=0), returncode=0)

    def test_verified_candidate_opens_as_a_new_session_and_rejects_changed_apk(self):
        self.repairs.runner_factory = self.verified_process
        candidate_provider = DemoProvider()
        with patch('reproloop.live.android_live.AndroidLiveProvider', return_value=candidate_provider):
            job = self.finished()
            self.assertEqual(job['state'], 'verified')
            self.assertEqual((job['baselineRuns'], job['verifiedRuns']), (3, 3))
            self.assertEqual(job['analysis']['method'], 'native-sample-accessibility')
            self.assertEqual(self.lab.get_session(self.s['id'])['state'], 'closed')
            resumed = self.repairs.resume(job['id'], 'owner', 'controller')
            self.assertNotEqual(resumed['id'], self.s['id'])
            self.assertEqual(resumed['state'], 'active')
            self.assertFalse(resumed['capabilities']['sdkCapture'])
            self.assertEqual(self.repairs.resume(job['id'], 'owner', 'controller')['id'], resumed['id'])
            self.lab.close_session(resumed['id'], 'owner', 'controller', resumed['epoch'])
            self.candidate_apk.write_bytes(b'changed after verification')
            with self.assertRaises(LiveError) as error:
                self.repairs.resume(job['id'], 'owner', 'controller')
            self.assertEqual(error.exception.code, 'app_changed')

    def test_cleanup_failure_quarantines_reserved_device(self):
        self.repairs.runner_factory = self.verified_process
        self.phone.stop.side_effect = RuntimeError('Synthetic disconnect')
        job = self.finished()
        self.assertEqual((job['state'], job['errorCode']), ('failed', 'cleanup_failed'))
        self.assertEqual(self.lab.list_devices()[0]['state'], 'quarantined')

    def test_results_from_another_bundle_are_rejected(self):
        def start(command, **kwargs):
            process = self.verified_process(command, **kwargs)
            output = Path(command[command.index('--output') + 1])
            path = output / 'attempt-1/verification/result.json'
            result = read_json(path)
            result['bundleDigest'] = 'another-bundle'
            write_json(path, result)
            return process
        self.repairs.runner_factory = start
        self.assertEqual(self.finished()['state'], 'failed')

    def test_missing_final_runner_proof_is_rejected(self):
        def start(command, **kwargs):
            process = self.verified_process(command, **kwargs)
            output = Path(command[command.index('--output') + 1])
            path = output / 'attempt-1/verification/result.json'
            result = read_json(path)
            del result['runs'][-1]['runner']['after']
            write_json(path, result)
            return process
        self.repairs.runner_factory = start
        self.assertEqual(self.finished()['state'], 'failed')

    def test_short_verification_is_rejected_despite_verified_status(self):
        def start(command, **kwargs):
            process = self.verified_process(command, **kwargs)
            output = Path(command[command.index('--output') + 1])
            path = output / 'attempt-1/verification/result.json'
            result = read_json(path)
            result['runs'].pop()
            write_json(path, result)
            return process
        self.repairs.runner_factory = start
        self.assertEqual(self.finished()['state'], 'failed')

    def assert_cancel_during_finalization(self, stage):
        self.repairs.runner_factory = self.verified_process
        original_factory = self.lab.devices['demo']['factory']
        def cancel():
            self.repairs.cancel(next(iter(self.repairs.jobs)), 'owner')
        if stage == 'candidate':
            validate = self.repairs.android.candidate
            def candidate(*args):
                result = validate(*args)
                cancel()
                return result
            self.repairs.android.candidate = candidate
        else:
            self.phone.stop.side_effect = cancel
        job = self.finished()
        self.assertEqual(job['state'], 'cancelled')
        self.assertIs(self.lab.devices['demo']['factory'], original_factory)
        self.assertEqual(self.lab.devices['demo']['capabilities']['applicationIdentity'], self.identity)
        self.assertEqual(self.lab.devices['demo']['state'], 'available')
        with self.assertRaises(LiveError):
            self.repairs.resume(job['id'], 'owner', 'controller')

    def test_cancel_after_child_exit_does_not_publish_candidate(self):
        self.assert_cancel_during_finalization('candidate')

    def test_cancel_during_cleanup_does_not_publish_candidate(self):
        self.assert_cancel_during_finalization('cleanup')

    def test_wrong_installed_candidate_apk_is_rejected(self):
        def start(command, **kwargs):
            process = self.verified_process(command, **kwargs)
            output = Path(command[command.index('--output') + 1])
            path = output / 'attempt-1/verification/result.json'
            result = read_json(path)
            result['runs'][0]['installation']['apkSha256'] = self.receipt['apkSha256']
            write_json(path, result)
            return process
        self.repairs.runner_factory = start
        self.assertEqual(self.finished()['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
