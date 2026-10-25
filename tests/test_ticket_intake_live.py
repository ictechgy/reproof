"""Synthetic Live collection and sealed capsules, with no device or network."""
import copy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
import zipfile

from reproof import contracts
from reproof.app_logs import APP_LOG_MIME, MAX_APP_LOG_EVENTS
from reproof.issue_package import build_archive, canonical, inspect_archive
from reproof.live.issue_workflow import IssueWorkflow
from reproof.ticket_intake import RegisteredCapsule, intake_ticket
from tests import test_g2_lab_integration as live_support
from tests.test_app_logs import snapshot
from tests.test_recording_recovery import project_document, tap_input
from tests.test_ticket_intake import CANARY, FIXTURES


class LiveTicketSnapshotTests(unittest.TestCase):
    setUp = live_support.ReleaseLabTests.setUp
    tearDown = live_support.ReleaseLabTests.tearDown
    register = live_support.ReleaseLabTests.register
    create = live_support.ReleaseLabTests.create

    def capsule(self, sizes=(0, 2, 2, 5), *, final_flags=None):
        project = project_document()
        # Intake intentionally supports log-only capsules. Disable pixels at
        # collection, rather than editing a frozen original to remove media.
        project['evidencePolicy']['pixels'] = False
        registration = self.register(project=project)
        log = dict(snapshot(), platform='ios', applicationId='com.example.app')
        for sequence in range(len(log['events']) + 1, max(sizes) + 1):
            log['events'].append(dict(log['events'][-1], seq=sequence, elapsedMs=sequence))
        collected = []

        def collect():
            value = copy.deepcopy(log)
            value['events'] = value['events'][:sizes[len(collected)]]
            value['endSequence'] = len(value['events'])
            if final_flags and len(collected) == len(sizes) - 1:
                value.update(final_flags)
            collected.append(copy.deepcopy(value))
            return value

        self.provider.collect_app_logs = collect
        # This log-only synthetic provider is immediately ready without pixels.
        self.provider.start = lambda session, lab: session.update(state='active')
        session = self.create(registration)
        # Supply one explicitly synthetic dispatch receipt through the actual
        # recorder API. No real app input or native proof is claimed here.
        recorder = self.lab._session(session['id'])['releaseRecorder']
        recorder.admit_input('operation_one', 1, 'synthetic_provider', tap_input())
        recorder.mark_dispatched('operation_one')
        recorder.record_receipt('operation_one', 'injected')
        for _ in sizes[:-1]:
            self.lab.app_logs(session['id'], 'owner')
        # Exercise the exact terminal collection hook used by workflow.stop,
        # followed by its real durable barrier/freeze, without fixture services.
        IssueWorkflow._final_log_snapshot(SimpleNamespace(lab=self.lab),
            SimpleNamespace(registration=registration),
            {'sessionId': session['id'], 'ownerId': 'owner'})
        recording = self.lab.stop_release_recording(session['id'], 'owner',
            session['controllerId'], session['epoch'])
        self.assertEqual(recording['status'], 'frozen-complete')
        self.assertEqual(recording['original']['media'], [])
        references = recording['original']['observations']
        self.assertEqual(len(references), len(sizes))
        self.assertTrue(all(ref['mimeType'] == APP_LOG_MIME for ref in references))
        for ref, value in zip(references, collected):
            raw = self.lab._evidence_store.read(ref['digest'])
            self.assertEqual(raw, canonical(value))
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()),
                (ref['bytes'], ref['digest']))
        spec = json.loads((FIXTURES / 'scenario.json').read_text())
        spec['originalRecordingDigest'] = recording['recordingDigest']
        event_id = recording['original']['events'][0]['id']
        spec['actions'][0]['eventId'] = event_id
        spec['waits'][0]['afterEventId'] = event_id
        capsule = build_archive(recording, spec, self.lab._evidence_store.read)
        entry = RegisteredCapsule('tenant_a', project, capsule,
            self.lab._session(session['id'])['appLogDigest'], log['runId'],
            log['sessionId'], log['profileDigest'], ('add',), ('main',))
        ticket = {'schemaVersion': 1, 'ticketId': 'qa_live', 'tenantId': 'tenant_a',
            'projectId': project['id'], 'applicationId': 'ios_app', 'buildId': 'original',
            'sourceDigest': project['builds'][0]['sourceDigest'],
            'sessionId': log['sessionId'], 'recordingDigest': recording['recordingDigest'],
            'text': CANARY}
        return ticket, entry, recording, spec

    def test_live_prefixes_and_repeated_reads_with_stop_snapshot_are_review_only(self):
        ticket, entry, recording, spec = self.capsule()
        before = copy.deepcopy((ticket, entry.project, recording, spec))
        capsule_before = entry.capsule
        contents = inspect_archive(io.BytesIO(entry.capsule))
        for key in ('original', 'recordingDigest', 'status', 'lifecycleReceipts'):
            self.assertEqual(contents.recording[key], recording[key])
        self.assertEqual(contents.specification, spec)
        refs = recording['original']['observations']
        self.assertEqual(refs[1]['digest'], refs[2]['digest'])
        self.assertNotEqual(refs[2]['digest'], refs[-1]['digest'])
        with zipfile.ZipFile(io.BytesIO(entry.capsule)) as archive:
            self.assertEqual(len(json.loads(archive.read('objects/' + refs[-1]['digest']))['events']), 5)
        result = intake_ticket(canonical(ticket), tenant_id='tenant_a', registry=(entry,))
        self.assertEqual(result['status'], 'review-required')
        self.assertEqual(result['references']['appLogDigest'], refs[-1]['digest'])
        self.assertEqual(result['references']['recordingDigest'], recording['recordingDigest'])
        self.assertEqual(result['references']['specificationDigest'], contracts.digest(spec))
        self.assertEqual((result['executionAuthority'], result['approved'], result['aiTransfer']),
            ('none', False, False))
        for forbidden in (CANARY, 'events', 'assertions', 'returned', 'componentId'):
            self.assertNotIn(forbidden, json.dumps(result))
        self.assertEqual((ticket, entry.project, recording, spec), before)
        self.assertEqual(entry.capsule, capsule_before)

    def test_live_bounded_buffer_truncation_does_not_become_complete_evidence(self):
        ticket, entry, recording, _ = self.capsule(
            sizes=(MAX_APP_LOG_EVENTS, MAX_APP_LOG_EVENTS), final_flags={'truncated': True})
        self.assertEqual(len(recording['original']['observations']), 2)
        result = intake_ticket(canonical(ticket), tenant_id='tenant_a', registry=(entry,))
        self.assertEqual(result['status'], 'abstained')
        self.assertEqual(result['reason'], 'evidence_incomplete')
        self.assertNotIn(CANARY, json.dumps(result))


if __name__ == '__main__':
    unittest.main()
