import copy
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
import uuid
from reproloop.device import AdbDevice
from reproloop.live.android_live import AndroidLiveProvider
from reproloop.live.providers import IosProvider
from reproloop.live.model import LiveError
from reproloop.ios_instrumentation import sample_ios_auto_profile
from tests.test_app_logs import marker,snapshot,RUN,SESSION


class AppLogProviderTests(unittest.TestCase):
    def test_android_reader_checks_run_and_original_marker_around_snapshot(self):
        device=AdbDevice.__new__(AdbDevice);device.package='io.reproloop.plain'
        device.app_profile=SimpleNamespace(digest='a'*64,data={'appLogs':1,'targets':{'tap':['add']},'screenTargets':{'panel':'main'}})
        paths=[]
        def read(path,limit):
            paths.append(path)
            return marker() if path.endswith('app-log-session.json') else snapshot()
        device._sdk_json=read
        self.assertEqual(device.collect_app_logs(RUN),snapshot())
        self.assertEqual(paths,['files/repro/app-log-session.json','files/repro/app-logs/'+SESSION+'/app-log.json','files/repro/app-log-session.json'])
        from reproloop.core import ContractError
        with self.assertRaises(ContractError):device.collect_app_logs(str(uuid.uuid4()))

    def test_ios_observation_only_readiness_is_independent_of_replay_marker(self):
        provider=IosProvider('sim',Path('products'),'io.reproloop.sample.ios',record_sdk=True,app_logs_only=True)
        profile=sample_ios_auto_profile();provider.auto_profile=profile;provider.auto_run_id=RUN;provider.automatic_app_logs=True
        value=snapshot();value.update(platform='ios',applicationId=provider.bundle,profileDigest=profile.digest)
        for event in value['events']:
            if event['type']=='click':event['target']='counter.add'
        selected={key:value[key] for key in marker()}
        provider._read_app_log_json=lambda path:copy.deepcopy(selected if path=='app-log-session.json' else value)
        provider._wait_for_auto_marker=lambda:(_ for _ in ()).throw(AssertionError('strict replay must not gate observation logs'))
        self.assertEqual(provider.bridge('ready',{}),{'ok':True})
        self.assertEqual(provider.collect_app_logs(),value)
        with self.assertRaises(LiveError):provider.collect_sdk_capture()
        selected['sessionId']=str(uuid.uuid4())
        with self.assertRaises(LiveError):provider.collect_app_logs()

    def test_android_reset_rotates_host_log_run_before_native_request(self):
        provider=AndroidLiveProvider.__new__(AndroidLiveProvider)
        provider.general_profile=False
        provider.record_sdk=True;provider.automatic_app_logs=True;provider.app_log_run_id=RUN
        provider.app_log_marker=marker();provider.stop=threading.Event();provider._receive_frame=lambda:None
        provider._wait_for_sdk_session=lambda:'new-sdk-session'
        captured=[]
        def call(path,body=None):
            if path=='/command':captured.append(body);return {'accepted':True}
            return {'id':captured[-1]['id'],'ok':True}
        provider.transport=SimpleNamespace(call=call)
        self.assertTrue(provider._execute('reset',{})['ok'])
        run=provider.app_log_run_id
        self.assertNotEqual(run,RUN);self.assertEqual(str(uuid.UUID(run)),run)
        self.assertEqual(captured[0]['payload'],{'appLogRunId':run});self.assertIsNone(provider.app_log_marker)

class AndroidAppLogCleanupTests(unittest.TestCase):
    def test_logging_app_stops_before_its_device_lease_is_released(self):
        provider=AndroidLiveProvider.__new__(AndroidLiveProvider)
        provider.stop=threading.Event();provider.process=None;provider.port=None;provider.transport=None;provider.thread=None
        provider.automatic_app_logs=True;provider.target_package='io.reproloop.plain';provider.token='test-only';provider.lease_held=True
        running=[True];released=[]
        def shell(*args,**kwargs):
            if args==('am','force-stop',provider.target_package):running[0]=False;return ''
            if args==('pidof',provider.target_package):return '123' if running[0] else ''
            raise AssertionError('Unexpected command')
        class Lease:
            def __exit__(self,*args):released.append(not running[0])
        provider.device=SimpleNamespace(shell=shell);provider.lease=Lease()
        provider.close()
        self.assertFalse(running[0]);self.assertEqual(released,[True])

    def test_logging_app_force_stop_failure_keeps_lease_reserved(self):
        provider=AndroidLiveProvider.__new__(AndroidLiveProvider)
        provider.stop=threading.Event();provider.process=None;provider.port=None;provider.transport=None;provider.thread=None
        provider.automatic_app_logs=True;provider.target_package='io.reproloop.plain';provider.token='test-only';provider.lease_held=True
        released=[]
        def shell(*args,**kwargs):
            if args==('am','force-stop',provider.target_package):raise RuntimeError('stop failed')
            raise AssertionError('Unexpected command')
        class Lease:
            def __exit__(self,*args):released.append(True)
        provider.device=SimpleNamespace(shell=shell);provider.lease=Lease()
        with self.assertRaises(LiveError) as error:
            provider.close()
        self.assertEqual(error.exception.code,'cleanup_uncertain')
        self.assertEqual(released,[])

    def test_logging_app_without_acquired_lease_issues_no_app_command(self):
        provider=AndroidLiveProvider.__new__(AndroidLiveProvider)
        provider.stop=threading.Event();provider.process=None;provider.port=None;provider.transport=None;provider.thread=None
        provider.automatic_app_logs=True;provider.target_package='io.reproloop.plain';provider.token='test-only';provider.lease_held=False
        commands=[]
        class Lease:
            def __exit__(self,*args):commands.append('release')
        def shell(*args,**kwargs):
            commands.append(args)
            raise AssertionError('A failed acquire must not stop the target')
        provider.device=SimpleNamespace(shell=shell);provider.lease=Lease()
        provider.close()
        self.assertEqual(commands,[])


if __name__=='__main__':unittest.main()
