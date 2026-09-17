import copy
import unittest
from reproloop.core import ContractError
from reproloop.ios_core import compile_ios_capture,validate_swift_expression,validate_test_summary


def ios_capture():
    return {'schemaVersion':2,'platform':'ios','applicationId':'io.reproloop.sample.ios','sessionId':'ios-qa',
            'fixture':{'id':'ios-counter','version':1,'inputs':{}},
            'startState':{'screen':'main','nodes':{'counter.name':'','counter.count':'0'}},
            'events':[{'id':'e1','seq':1,'action':'replace','target':'counter.name','parameters':{'value':'QA'}},
                      {'id':'e2','seq':2,'action':'tap','target':'counter.add','parameters':{}}],
            'truncated':False,'lostEvents':False,'endSequence':2}


def ios_oracle():
    return {'actual':'One add increments twice','expected':'One add increments once',
            'bugCondition':{'target':'counter.count','text':'2'},
            'expectedCondition':{'target':'counter.count','text':'1'}}


class IosContractTests(unittest.TestCase):
    def test_v2_keeps_semantic_ids_and_event_mapping(self):
        c=ios_capture();before=copy.deepcopy(c);s=compile_ios_capture(c,ios_oracle())
        self.assertEqual(s['platform'],'ios');self.assertEqual(s['steps'][0]['target'],'counter.name')
        self.assertEqual(s['steps'][1]['sourceEventIds'],['e2']);self.assertEqual(c,before)
    def test_android_global_back_and_unknown_platform_rejected(self):
        for mutate in [lambda c:c.update(platform='android'),
                       lambda c:c['events'][0].update(action='back',target='counter.back',parameters={}),
                       lambda c:c.update(applicationId='other.app')]:
            c=ios_capture();mutate(c)
            with self.assertRaises(ContractError):compile_ios_capture(c,ios_oracle())
    def test_explicit_navigation_back_supported(self):
        c=ios_capture();c['events'][1].update(action='navigate_back',target='counter.back')
        self.assertEqual(compile_ios_capture(c,ios_oracle())['steps'][1]['action'],'navigate_back')
    def test_v1_is_not_silently_accepted(self):
        c=ios_capture();c['schemaVersion']=1
        with self.assertRaises(ContractError):compile_ios_capture(c,ios_oracle())
    def test_swift_expression_rejects_extra_code(self):
        before='enum CounterLogic {\n    static func increment() -> Int { return 2 }\n}\n'
        validate_swift_expression(before,before.replace('return 2','return 1'))
        with self.assertRaises(ContractError):validate_swift_expression(before,before.replace('return 2','print("hidden"); return 1'))
        with self.assertRaises(ContractError):validate_swift_expression(before,before+'let changed = 1\n')
    def test_xctest_exit_success_is_not_enough(self):
        good={'totalTestCount':1,'passedTests':1,'failedTests':0,'skippedTests':0,'testFailures':[]}
        validate_test_summary(good)
        for invalid in [{},dict(good,totalTestCount=0,passedTests=0),dict(good,skippedTests=1),
                        dict(good,failedTests=1),dict(good,testFailures=[{'failure':'error'}])]:
            with self.assertRaises(ContractError):validate_test_summary(invalid)


class FinalizationTests(unittest.TestCase):
    def evidence(self):
        c=ios_capture();c['startedAtMs']=100
        m={k:c[k] for k in ['sessionId','endSequence','fixture','startState','startedAtMs']};m['finalized']=True
        return c,m,{'finalized':True,'endSequence':2}
    def test_complete_matching_session(self):
        from reproloop.ios_core import validate_finalization
        self.assertTrue(validate_finalization(*self.evidence(),min_started_at=99))
    def test_stale_or_incomplete_capture_rejected(self):
        from reproloop.ios_core import validate_finalization
        c,m,marker=self.evidence()
        with self.assertRaises(ContractError):validate_finalization(c,m,marker,min_started_at=101)
        m['finalized']=False
        with self.assertRaises(ContractError):validate_finalization(c,m,marker)
    def test_wrong_freeze_boundary_rejected(self):
        from reproloop.ios_core import validate_finalization
        c,m,marker=self.evidence();marker['endSequence']=1
        with self.assertRaises(ContractError):validate_finalization(c,m,marker)

if __name__=='__main__':unittest.main()
