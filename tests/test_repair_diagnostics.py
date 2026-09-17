"""Derived app logs retain their source binding without becoming replay inputs."""
import copy
import hashlib
import json
import unittest

from reproloop import contracts
from reproloop.execution.wire import canonical
from reproloop.project_repair import RepairError, proposal_packet
from tests.test_app_logs import snapshot
from tests.test_issue_package import example
from tests.test_project_repair import source_fixture, PRODUCT


APP_LOG_MIME = 'application/vnd.reproloop.app-log+json'


def diagnostic_policy(project):
    return {'schemaVersion': 1, 'projectDigest': contracts.digest(project),
        'applicationId': 'ios_app', 'profileDigest': 'a' * 64,
        'clickTargets': ['add'], 'screenTargets': ['main'],
        'eventFields': ['seq', 'elapsedMs', 'type', 'name', 'target'], 'maxEvents': 500}


def app_log():
    return dict(snapshot(), platform='ios', applicationId='com.example.app')


class RepairDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.project, self.sources, _ = source_fixture()
        self.project['evidencePolicy'].update(logs=True, aiEligible=True)
        self.recording, self.specification = example()
        self.original = self.recording['original']
        self.policy = diagnostic_policy(self.project)
        self.objects = {}
        self.reads = []

    def attach(self, value, mime=APP_LOG_MIME):
        raw = canonical(value); digest = hashlib.sha256(raw).hexdigest()
        reference = {'id': 'observation_' + str(len(self.original['observations']) + 1),
            'digest': digest, 'path': 'objects/' + digest, 'bytes': len(raw), 'mimeType': mime}
        self.objects[digest] = raw
        self.original['observations'].append(reference)
        self.specification['originalRecordingDigest'] = contracts.digest(self.original)
        return reference

    def derive(self):
        from reproloop.repair_diagnostics import derive_app_log_diagnostics
        def read(reference):
            self.reads.append(reference['digest'])
            return self.objects[reference['digest']]
        return derive_app_log_diagnostics(self.project, self.original, self.sources.digest,
            self.policy, read_object=read)

    def transfer(self, fields=None):
        return {'schemaVersion': 2, 'projectDigest': contracts.digest(self.project),
            'providerId': 'claude', 'approvedTransfer': True, 'sourcePaths': [PRODUCT],
            'specificationFields': ['actions'], 'diagnostics': {
                'policyDigest': contracts.digest(self.policy),
                'eventFields': fields or ['seq', 'type', 'name']}}

    def packet(self, diagnostics, policy=None):
        return proposal_packet(self.project, self.sources, self.specification,
            provider_id='claude', external=True, policy=policy or self.transfer(),
            diagnostics=diagnostics)

    def test_only_typed_logs_are_read_and_bound_to_the_original_build_source_and_run(self):
        self.attach({'text': 'synthetic-private-value'}, mime='application/json')
        source = self.attach(app_log())
        result = self.derive()
        self.assertEqual(self.reads, [source['digest']])
        self.assertEqual(result['purpose'], 'diagnostic-only')
        self.assertEqual(result['recordingDigest'], contracts.digest(self.original))
        self.assertEqual(result['sourceDigest'], self.sources.digest)
        self.assertEqual(result['artifactDigest'], self.project['builds'][0]['artifactDigest'])
        self.assertEqual(result['sourceObjects'], [source['digest']])
        run = result['runs'][0]
        self.assertEqual(run['clock'], 'app-elapsed-unmapped')
        self.assertEqual(run['sourceObjects'], [source['digest']])
        self.assertEqual(run['events'][3]['name'], 'returned')
        self.assertNotIn('synthetic-private-value', json.dumps(result))
        for name in ('runId', 'sessionId', 'startedAtMs', 'componentId'):
            self.assertNotIn(name, json.dumps(result))

    def test_repeated_snapshots_keep_one_prefix_and_explicitly_bound_omission(self):
        short = app_log(); short['events'] = short['events'][:3]; short['endSequence'] = 3
        self.attach(short); self.attach(app_log()); self.attach(app_log())
        self.policy['maxEvents'] = 3
        result = self.derive(); run = result['runs'][0]
        self.assertEqual(len(result['runs']), 1)
        self.assertEqual(run['totalEvents'], 5)
        self.assertEqual(run['omittedEvents'], 2)
        self.assertEqual(len(run['events']), 3)
        self.assertEqual(len(run['sourceObjects']), 2)
        self.assertFalse(run['truncated'])
        self.assertFalse(run['lostEvents'])

    def test_inconsistent_prefix_and_different_run_markers_are_rejected(self):
        first = app_log(); self.attach(first)
        for mutate in (lambda v: v['events'][0].update(name='resumed'),
                       lambda v: v.update(runId='99999999-2222-4333-8444-555555555555'),
                       lambda v: v.update(startedAtMs=101)):
            later = app_log(); mutate(later); self.attach(later)
            with self.subTest(mutate=mutate), self.assertRaises(RepairError): self.derive()
            self.original['observations'].pop()

    def test_secret_fields_corrupt_bytes_and_foreign_identity_are_rejected(self):
        for mutate in (lambda v: v['events'][0].update(message='synthetic-private-value'),
                       lambda v: v['events'][2].update(target='synthetic-private-value'),
                       lambda v: v.update(profileDigest='b' * 64),
                       lambda v: v.update(applicationId='com.example.foreign'),
                       lambda v: v.update(platform='android')):
            self.original['observations'].clear()
            value = app_log(); mutate(value); self.attach(value)
            with self.subTest(mutate=mutate), self.assertRaises(RepairError) as caught: self.derive()
            self.assertNotIn('synthetic-private-value', str(caught.exception))
        self.original['observations'].clear(); ref = self.attach(app_log())
        self.objects[ref['digest']] += b' '
        with self.assertRaises(RepairError): self.derive()

    def test_disabled_collection_wrong_source_and_empty_selection_cannot_supply_diagnostics(self):
        with self.assertRaises(RepairError): self.derive()
        self.attach(app_log())
        self.project['evidencePolicy']['logs'] = False
        self.policy['projectDigest'] = contracts.digest(self.project)
        with self.assertRaises(RepairError): self.derive()
        self.project['evidencePolicy']['logs'] = True
        self.project['builds'][0]['sourceDigest'] = 'f' * 64
        self.policy['projectDigest'] = contracts.digest(self.project)
        with self.assertRaises(RepairError): self.derive()

    def test_external_packet_contains_only_separately_approved_diagnostic_fields(self):
        self.attach(app_log()); result = self.derive()
        packet = self.packet(result)
        self.assertEqual(set(packet['diagnostics']['runs'][0]['events'][0]), {'seq', 'type', 'name'})
        self.assertNotIn('target', json.dumps(packet['diagnostics']))
        self.assertNotIn('elapsedMs', json.dumps(packet['diagnostics']))
        self.assertEqual(packet['diagnostics']['policyDigest'], contracts.digest(self.policy))
        self.assertEqual(set(result['runs'][0]['events'][0]), set(self.policy['eventFields']))
        self.assertEqual(set(packet['specification']), {'actions'})

    def test_existing_source_approval_does_not_authorize_logs_or_expanded_fields(self):
        self.attach(app_log()); result = self.derive()
        denied = []
        legacy = self.transfer(); legacy['schemaVersion'] = 1; del legacy['diagnostics']; denied.append(legacy)
        wrong = self.transfer(); wrong['diagnostics']['policyDigest'] = 'b' * 64; denied.append(wrong)
        denied.append(self.transfer(['component']))
        denied.append(self.transfer(['rawMessage']))
        for policy in denied:
            with self.subTest(policy=policy), self.assertRaises(RepairError) as caught:
                self.packet(result, policy)
            self.assertEqual(caught.exception.code, 'ai_transfer_denied')

    def test_a_derivative_cannot_be_rebound_to_another_recording(self):
        self.attach(app_log()); result = self.derive()
        self.specification['originalRecordingDigest'] = 'f' * 64
        with self.assertRaises(RepairError): self.packet(result)

    def test_full_supported_log_does_not_inherit_the_smaller_vm_message_limit(self):
        target = 'a' * 80
        self.policy.update(clickTargets=[target], maxEvents=2000,
            eventFields=['seq', 'elapsedMs', 'type', 'name', 'component', 'target'])
        value = app_log()
        event = dict(value['events'][2], target=target)
        value['events'] = [dict(event, seq=index + 1, elapsedMs=index) for index in range(2000)]
        value['endSequence'] = 2000
        reference = self.attach(value)
        self.assertGreater(reference['bytes'], 256 * 1024)
        result = self.derive()
        self.assertEqual(len(result['runs'][0]['events']), 2000)
        packet = self.packet(result, self.transfer(self.policy['eventFields']))
        self.assertGreater(len(canonical(packet['diagnostics'])), 256 * 1024)

    def test_multiple_runs_preserve_loss_flags_and_share_one_event_budget(self):
        first = app_log(); first.update(truncated=True, lostEvents=True)
        self.attach(first)
        second = app_log(); second.update(runId='99999999-2222-4333-8444-555555555555',
                                          sessionId='99999999-3333-4444-8555-666666666666')
        self.attach(second); self.policy['maxEvents'] = 6
        result = self.derive()
        self.assertEqual([len(run['events']) for run in result['runs']], [5, 1])
        self.assertEqual([run['omittedEvents'] for run in result['runs']], [0, 4])
        self.assertTrue(result['runs'][0]['lostEvents'])
        self.assertTrue(result['runs'][0]['truncated'])
        self.assertNotEqual(result['runs'][0]['runDigest'], result['runs'][1]['runDigest'])
        self.attach(app_log())
        with self.assertRaises(RepairError): self.derive()


if __name__ == '__main__': unittest.main()
