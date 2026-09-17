import unittest
from reproloop.core import ContractError
from reproloop.ios_core import compile_ios_capture,validate_swift_expression
from tests.test_ios_core import ios_capture,ios_oracle


class CaseCompilationTests(unittest.TestCase):
    def test_duplicate_submission_preserves_both_submits(self):
        capture=ios_capture();capture['fixture']['id']='ios-duplicate-submit'
        capture['events'].append({'id':'e3','seq':3,'action':'tap','target':'counter.add','parameters':{}});capture['endSequence']=3
        compiled=compile_ios_capture(capture,ios_oracle())
        self.assertEqual([s['sourceEventIds'] for s in compiled['steps']],[['e1'],['e2'],['e3']])
    def test_reset_is_supported_only_by_reset_fixture(self):
        capture=ios_capture();capture['fixture']['id']='ios-reset'
        capture['events'].append({'id':'e3','seq':3,'action':'tap','target':'counter.reset','parameters':{}});capture['endSequence']=3
        oracle=ios_oracle();oracle['bugCondition']['text']='1';oracle['expectedCondition']['text']='0'
        self.assertEqual(compile_ios_capture(capture,oracle)['steps'][-1]['target'],'counter.reset')
        capture['fixture']['id']='ios-counter'
        with self.assertRaises(ContractError):compile_ios_capture(capture,ios_oracle())
    def test_unknown_case_and_cross_case_oracle_rejected(self):
        capture=ios_capture();capture['fixture']['id']='ios-unregistered'
        with self.assertRaises(ContractError):compile_ios_capture(capture,ios_oracle())
        capture['fixture']['id']='ios-reset'
        with self.assertRaises(ContractError):compile_ios_capture(capture,ios_oracle())

    def test_case_specific_expression_guards(self):
        submit='enum SubmissionLogic { static func shouldAccept(submitted: Bool) -> Bool { return true } }'
        reset='enum ResetLogic { static func resetValue(previous: Int) -> Int { return previous } }'
        validate_swift_expression(submit,submit.replace('return true','return !submitted'),'duplicate-submit')
        validate_swift_expression(reset,reset.replace('return previous','return 0'),'reset')
        with self.assertRaises(ContractError):validate_swift_expression(submit,submit.replace('return true','return !submitted'),'reset')
        with self.assertRaises(ContractError):validate_swift_expression(reset,reset.replace('return previous','return unsafe()'),'reset')

if __name__=='__main__':unittest.main()
