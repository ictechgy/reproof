"""Independent validation receipts bind each check to one candidate and plan."""
from dataclasses import replace
import threading
import time
import unittest


def plan_document():
    return {'schemaVersion': 1, 'id': 'independent-checks', 'projectDigest': '1' * 64,
        'checks': [
            {'id': 'behavior', 'recipeId': 'regression_ui', 'kind': 'external-observation',
             'evidenceSourceId': 'approved-observer'},
            {'id': 'protected-harness', 'recipeId': 'regression_logic', 'kind': 'trusted-runner',
             'evidenceSourceId': 'independent-runner'}], 'candidateReports': 'supplemental-only'}


class TrustedValidationTests(unittest.TestCase):
    def binding(self, **changes):
        from reproof.validation import ValidationBinding
        value = ValidationBinding(operation_id='repair_attempt_1', repair_plan_digest='2' * 64,
            project_digest='1' * 64, source_digest='3' * 64, artifact_digest='4' * 64)
        return replace(value, **changes)

    def authority(self, callback=None):
        from reproof.validation import TrustedValidationAuthority, ValidationObservation
        authority = TrustedValidationAuthority(plan_document())
        callback = callback or (lambda context, **_: ValidationObservation(
            context.digest, 'pass', '5' * 64, True, True))
        authority.register('approved-observer', callback, kind='external-observation')
        authority.register('independent-runner', callback, kind='trusted-runner')
        return authority

    def test_complete_independent_receipt_is_bound_to_exact_candidate(self):
        from reproof.validation import ValidationError
        authority = self.authority(); binding = self.binding()
        result = authority.run(binding, cancellation=threading.Event(), timeout_seconds=1)
        self.assertEqual(result.public()['status'], 'pass')
        authority.require_pass(result, binding)
        self.assertEqual({item['checkId'] for item in result.public()['checks']},
                         {'behavior', 'protected-harness'})
        for changed in (self.binding(source_digest='a' * 64), self.binding(artifact_digest='a' * 64),
                        self.binding(operation_id='repair_attempt_2'), self.binding(repair_plan_digest='a' * 64)):
            with self.assertRaises(ValidationError): authority.require_pass(result, changed)

    def test_candidate_json_and_foreign_authority_never_issue_a_pass(self):
        from reproof.validation import ValidationError
        authority = self.authority(lambda context, **_: {'contextDigest': context.digest, 'passed': True})
        receipt = authority.run(self.binding(), cancellation=threading.Event(), timeout_seconds=1)
        self.assertEqual(receipt.public()['status'], 'quarantined')
        with self.assertRaises(ValidationError): authority.require_pass(receipt, self.binding())
        good = self.authority(); result = good.run(self.binding(), cancellation=threading.Event(), timeout_seconds=1)
        with self.assertRaises(ValidationError): authority.require_pass(result, self.binding())
        with self.assertRaises(ValidationError): good.require_pass(result.public(), self.binding())

    def test_stale_observation_and_unconfirmed_cleanup_do_not_pass(self):
        from reproof.validation import ValidationObservation
        cases = [lambda context, **_: ValidationObservation('f' * 64, 'pass', '5' * 64, True, True),
                 lambda context, **_: ValidationObservation(context.digest, 'pass', '5' * 64, False, True),
                 lambda context, **_: ValidationObservation(context.digest, 'pass', '5' * 64, True, False),
                 lambda context, **_: ValidationObservation(context.digest, 'unknown', '5' * 64, True, True)]
        for callback in cases:
            with self.subTest(callback=callback):
                result = self.authority(callback).run(self.binding(), cancellation=threading.Event(), timeout_seconds=1)
                self.assertNotEqual(result.public()['status'], 'pass')

    def test_missing_or_wrong_kind_source_blocks_before_dispatch(self):
        from reproof.validation import TrustedValidationAuthority, ValidationError
        authority = TrustedValidationAuthority(plan_document()); called = []
        authority.register('approved-observer', lambda *_args, **_kw: called.append(True), kind='trusted-runner')
        with self.assertRaises(ValidationError):
            authority.run(self.binding(), cancellation=threading.Event(), timeout_seconds=1)
        self.assertEqual(called, [])

    def test_cancellation_and_late_result_are_terminal(self):
        from reproof.validation import ValidationError, ValidationObservation
        started = threading.Event(); release = threading.Event()
        def wait(context, **_):
            started.set(); release.wait(1)
            return ValidationObservation(context.digest, 'pass', '5' * 64, True, True)
        authority = self.authority(wait)
        before = time.monotonic()
        result = authority.run(self.binding(), cancellation=threading.Event(), timeout_seconds=.05)
        self.assertTrue(started.is_set())
        self.assertLess(time.monotonic() - before, .5)
        self.assertEqual(result.public()['status'], 'quarantined')
        release.set(); time.sleep(.02)
        with self.assertRaises(ValidationError): authority.require_pass(result, self.binding())
        cancel = threading.Event(); cancel.set()
        self.assertEqual(self.authority().run(self.binding(), cancellation=cancel, timeout_seconds=1).public()['status'], 'cancelled')

    def test_reused_run_id_and_adapter_replacement_are_rejected(self):
        from reproof.validation import ValidationError
        authority = self.authority(); binding = self.binding()
        authority.run(binding, cancellation=threading.Event(), timeout_seconds=1)
        with self.assertRaises(ValidationError):
            authority.run(binding, cancellation=threading.Event(), timeout_seconds=1)
        with self.assertRaises(ValidationError):
            authority.register('approved-observer', lambda *_: None, kind='external-observation')

    def test_missing_or_malformed_termination_evidence_quarantines_the_authority(self):
        from reproof.validation import ValidationError, ValidationObservation
        def failed(*_, **__): raise RuntimeError('private observer details')
        for callback in (failed, lambda context, **_: ValidationObservation(context.digest, {}, '5' * 64, True, True)):
            with self.subTest(callback=callback):
                authority = self.authority(callback)
                result = authority.run(self.binding(), cancellation=threading.Event(), timeout_seconds=1)
                self.assertEqual(result.public()['status'], 'quarantined')
                with self.assertRaises(ValidationError): authority.ready()

    def test_cancelled_return_still_requires_bound_termination_and_cleanup(self):
        from reproof.validation import ValidationObservation
        cancel = threading.Event()
        def callback(context, **_):
            cancel.set()
            return ValidationObservation(context.digest, 'pass', '5' * 64, True, False)
        result = self.authority(callback).run(self.binding(), cancellation=cancel, timeout_seconds=1)
        self.assertEqual(result.public()['status'], 'quarantined')


if __name__ == '__main__': unittest.main()
