"""Portable inert tests: standard library, no subprocess/device/network/server."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from reproof.issue_package import canonical
from reproof.sdk_review import (MAX_BYTES, MAX_EVENTS, ReviewInvalid, export_digest,
                               linked_review, normalize_export)
from reproof.sdk_review_demo import run_demo, sample_export, sample_join

ROOT = Path(__file__).resolve().parents[1]
CANARY = 'SECRET_CANARY_do_not_retain'


class SDKReviewTests(unittest.TestCase):
    def rejected(self, files):
        with self.assertRaises(ReviewInvalid) as ctx:
            normalize_export(**files)
        self.assertEqual(str(ctx.exception), 'sdk_export_invalid')
        self.assertNotIn(CANARY, str(ctx.exception))

    def test_both_native_formats_are_review_only(self):
        for p in ('android', 'ios'):
            files = sample_export(p)
            env = normalize_export(**files)
            self.assertEqual(env._digest, export_digest(**files))
            result = env.summary()
            self.assertEqual(result['status'], 'review-only')
            self.assertFalse(result['repairReady'])
            self.assertIn('installedApplicationIdentity', result['unresolved'])
            self.assertEqual(result['applicationIdentityEvidence'],
                             'policy-declared-not-installed-attestation')
            self.assertFalse(result['approved'])
            self.assertEqual(result['executionAuthority'], 'none')
            for private in (env._digest, env._session, 'QA', 'bugCondition', 'parameters'):
                self.assertNotIn(private, json.dumps(result))
            self.assertNotIn(env._digest, repr(env))
            result['actions'].clear()
            self.assertEqual(len(env.summary()['actions']), 2)

    def test_loss_truncation_and_finalization_fail_closed(self):
        for p in ('android', 'ios'):
            for key in ('lostEvents', 'truncated'):
                f = sample_export(p); c = json.loads(f['capture']); c[key] = True
                self.rejected(dict(f, capture=canonical(c)))
            for key, value in [('finalized', False), ('sessionId', 'different'),
                               ('schemaVersion', True), ('startState', {})]:
                f = sample_export(p); m = json.loads(f['metadata']); m[key] = value
                self.rejected(dict(f, metadata=canonical(m)))
        for key, value in [('incomplete', True), ('unsupported', True), ('lostEvents', True)]:
            f = sample_export('android'); m = json.loads(f['metadata']); m[key] = value
            self.rejected(dict(f, metadata=canonical(m)))
        for key, value in [('finalized', False), ('endSequence', 1), ('endSequence', True)]:
            f = sample_export('ios'); m = json.loads(f['marker']); m[key] = value
            self.rejected(dict(f, marker=canonical(m)))

    def test_identity_timestamp_and_boolean_confusion(self):
        for p in ('android', 'ios'):
            for key, value in [('schemaVersion', True), ('startedAtMs', True),
                               ('endSequence', True), ('sessionId', {})]:
                f = sample_export(p); c = json.loads(f['capture']); c[key] = value
                self.rejected(dict(f, capture=canonical(c)))
        f = sample_export('ios'); m = json.loads(f['metadata']); m['fixture']['version'] = True
        self.rejected(dict(f, metadata=canonical(m)))

    def test_unknown_nested_duplicate_and_oversize_rejected(self):
        for p in ('android', 'ios'):
            f = sample_export(p)
            for part in ('capture', 'metadata'):
                obj = json.loads(f[part]); obj[CANARY] = {'nested': CANARY}
                self.rejected(dict(f, **{part: canonical(obj)}))
                self.rejected(dict(f, **{part: b'{"schemaVersion":1,' + f[part][1:]}))
                self.rejected(dict(f, **{part: b' ' * (MAX_BYTES + 1)}))
                self.rejected(dict(f, **{part: b'{"x":NaN}'}))
            c = json.loads(f['capture']); c['events'][0]['parameters']['value'] = CANARY
            self.rejected(dict(f, capture=canonical(c)))
            c = json.loads(f['capture']); c['events'] *= MAX_EVENTS
            self.rejected(dict(f, capture=canonical(c)))
            c = json.loads(f['capture']); c['events'][0]['dispatch'] = 'injected'
            self.rejected(dict(f, capture=canonical(c)))
        self.rejected(dict(sample_export('android'), marker=b'{}'))
        self.rejected(dict(sample_export('ios'), marker=None))

    def test_current_native_uuid_and_timing_policy(self):
        for p in ('android', 'ios'):
            for value in (None, True, -1, 600001, 0):
                f = sample_export(p); c = json.loads(f['capture'])
                c['events'][0]['elapsedMs'] = 100
                c['events'][1]['elapsedMs'] = value
                self.rejected(dict(f, capture=canonical(c)))
            f = sample_export(p); c = json.loads(f['capture'])
            del c['events'][0]['elapsedMs']
            self.rejected(dict(f, capture=canonical(c)))
            c = json.loads(f['capture']); c['sessionId'] = 'legacy-non-uuid'
            self.rejected(dict(f, capture=canonical(c)))
        f = sample_export('ios'); c = json.loads(f['capture']); m = json.loads(f['metadata'])
        c['fixture']['id'] = 'ios-duplicate-submit'; m['fixture'] = c['fixture']
        self.rejected(dict(f, capture=canonical(c), metadata=canonical(m)))

    def test_adversarial_parser_boundaries_are_non_echoing(self):
        f = sample_export('android')
        for raw in (b'[' * 2000 + b'0' + b']' * 2000,
                    b'{"x":' + b'1' * 5000 + b'}',
                    b'{"x":{"nested":Infinity}}', b'{"x":"\\ud800"}',
                    b'{"x":"\xff"}'):
            self.rejected(dict(f, capture=raw))

    def test_every_binding_field_is_exact(self):
        for p in ('android', 'ios'):
            _, env, binding, ticket, entry = sample_join(p, ROOT)
            args = dict(envelope=env, ticket=ticket, tenant_id='local_demo', entry=entry)
            self.assertEqual(linked_review(**args, binding=binding)['status'], 'review-required')
            self.assertEqual(linked_review(**args, binding=None)['reason'], 'collector_binding_missing')
            for field, val in [('export_digest', '0' * 64), ('sdk_session_id', 'other'),
                ('sdk_platform', 'other'), ('sdk_application_id', 'other'),
                ('capsule_digest', '0' * 64), ('ticket_identity', ())]:
                result = linked_review(**args, binding=replace(binding, **{field: val}))
                self.assertEqual(result['reason'], 'collector_binding_mismatch', field)
                self.assertNotIn('draft', result)
            # Exact bytes, not merely semantically equivalent JSON.
            files = sample_export(p); files['capture'] += b'\n'
            changed = normalize_export(**files)
            self.assertEqual(linked_review(**dict(args, envelope=changed), binding=binding)['status'], 'abstained')

    def test_separate_registry_strictness_preserved(self):
        _, env, binding, ticket, entry = sample_join('android', ROOT)
        for bad in (replace(entry, run_id='bad'), replace(entry, profile_digest='0' * 64),
                    replace(entry, session_id='bad')):
            result = linked_review(envelope=env, binding=binding, ticket=ticket,
                                   tenant_id='local_demo', entry=bad)
            self.assertEqual(result['reason'], 'separate_evidence_ineligible')
            self.assertNotIn('draft', result)
        project = copy.deepcopy(entry.project)
        project['applications'][0]['bundle'] = 'other.app'
        result = linked_review(envelope=env, binding=binding, ticket=ticket,
            tenant_id='local_demo', entry=replace(entry, project=project))
        self.assertEqual(result['reason'], 'collector_binding_mismatch')

    def test_joined_demo_never_claims_native_or_ai(self):
        out = run_demo(ROOT)
        self.assertFalse(out['actualAI']); self.assertFalse(out['nativeExecution'])
        self.assertFalse(out['companyAcceptance']); self.assertFalse(out['offlineCausalStageInvoked'])
        for p in out['platforms'].values():
            self.assertEqual(p['linked']['draft']['status'], 'review-required')
            self.assertTrue(p['incompleteRejectedBeforeDraft'])
            self.assertEqual(p['missingBinding']['status'], 'abstained')
            self.assertEqual(p['mismatchedBinding']['status'], 'abstained')


if __name__ == '__main__':
    unittest.main()
