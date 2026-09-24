import copy
import unittest
from reproof.core import ContractError
from reproof.app_logs import collect_app_logs, validate_app_log, validate_app_log_marker

RUN = '11111111-2222-4333-8444-555555555555'
SESSION = '22222222-3333-4444-8555-666666666666'


def marker():
    return dict(schemaVersion=1, platform='android', applicationId='io.reproof.plain',
                runId=RUN, sessionId=SESSION, profileDigest='a'*64, startedAtMs=100)


def snapshot():
    events=[dict(seq=1, elapsedMs=0, type='lifecycle', name='created', component='activity', componentId='c'+'1'*16, target=None),
            dict(seq=2, elapsedMs=1, type='screen', name='appeared', component='view', componentId='c'+'2'*16, target='main'),
            dict(seq=3, elapsedMs=2, type='click', name='began', component='view', componentId='c'+'3'*16, target='add'),
            dict(seq=4, elapsedMs=3, type='click', name='returned', component='view', componentId='c'+'3'*16, target='add'),
            dict(seq=5, elapsedMs=4, type='lifecycle', name='background', component='application', componentId='app', target=None)]
    return dict(marker(), endSequence=len(events), truncated=False, lostEvents=False, events=events)


class AppLogContractTests(unittest.TestCase):
    def validate(self, value):
        return validate_app_log(value, marker(), click_targets={'add'}, screen_targets={'main'})

    def test_mixed_observations_roundtrip_and_flagged_prefix_remains_downloadable(self):
        value=snapshot();self.assertEqual(self.validate(value), value)
        value.update(truncated=True, lostEvents=True)
        self.assertEqual(self.validate(value), value)
        result=self.validate(value);result['events'].clear()
        self.assertEqual(len(value['events']), 5)

    def test_unapproved_values_and_event_corruption_are_rejected(self):
        for mutate in [lambda d:d['events'][0].update(message='private'),
                       lambda d:d['events'][2].update(target='unconfigured'),
                       lambda d:d['events'][1].update(target='private screen title'),
                       lambda d:d['events'][0].update(componentId='MyController'),
                       lambda d:d['events'][0].update(target='private'),
                       lambda d:d['events'][2].update(seq=8),
                       lambda d:d['events'][3].update(elapsedMs=0),
                       lambda d:d.update(endSequence=100),
                       lambda d:d.update(runId='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
                       lambda d:d.update(truncated=1)]:
            value=snapshot();mutate(value)
            with self.subTest(value=value), self.assertRaises(ContractError):self.validate(value)

    def test_initial_empty_log_and_opaque_controller_screen_are_valid(self):
        value=dict(marker(),endSequence=0,truncated=False,lostEvents=False,events=[])
        self.validate(value)
        value=snapshot();value['events'][1]['target']='c'+'4'*16
        self.validate(value)

    def test_marker_requires_full_selected_identity(self):
        args=dict(platform='android', application_id='io.reproof.plain', profile_digest='a'*64, run_id=RUN)
        self.assertEqual(validate_app_log_marker(marker(), **args), marker())
        for key, value in [('runId',RUN.upper()),('sessionId','../bad'),('platform','ios'),('profileDigest','b'*64),('startedAtMs',True)]:
            d=marker();d[key]=value
            if d==marker():continue
            with self.subTest(key=key), self.assertRaises(ContractError):validate_app_log_marker(d, **args)

    def test_snapshot_collection_pins_original_marker_across_all_reads(self):
        for change in [None, 'runId', 'sessionId', 'profileDigest', 'startedAtMs']:
            observed=[]
            def read(path):
                observed.append(path)
                if path=='app-log-session.json':
                    d=marker()
                    if len(observed)>1 and change:d[change]='changed'
                    return d
                self.assertEqual(path, 'app-logs/'+SESSION+'/app-log.json')
                return snapshot()
            args=dict(platform='android', application_id='io.reproof.plain', profile_digest='a'*64,
                      run_id=RUN, click_targets={'add'}, screen_targets={'main'})
            if change:
                with self.subTest(change=change), self.assertRaises(ContractError):collect_app_logs(read, **args)
            else:self.assertEqual(collect_app_logs(read, **args), snapshot())


if __name__=='__main__':unittest.main()
