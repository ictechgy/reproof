"""Portable real workflow/package/HTTP tests; no native media or device claims."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

from reproof import contracts
from reproof.core import ContractError
from reproof.issue_package import build_archive, PackageError
from reproof.live.access import AccessError
from reproof.live.client import IssueClient
from reproof.live.model import LiveError
from reproof.live.server import LiveServer
from tests import test_issue_workflow as support
from tests.g4_support import ScenarioProvider, SECRET
from tests.test_fixture_allocations import project_document
from tests.test_issue_media import png
from tests.test_issue_package import example
from tests.test_repair_diagnostics import app_log


class TicketReferenceFixture:
    start = support.IssueWorkflowTests.start
    wait = support.IssueWorkflowTests.wait
    recorded = support.IssueWorkflowTests.recorded
    save = support.IssueWorkflowTests.save
    approve = support.IssueWorkflowTests.approve
    tearDown = support.IssueWorkflowTests.tearDown

    def setUp(self):
        project = project_document()
        project['evidencePolicy']['logs'] = True
        if not getattr(self, 'registered_source', True):
            for build in project['builds']: build.pop('sourceDigest')
        with mock.patch('tests.g4_support.project_document', return_value=project):
            support.IssueWorkflowTests.setUp(self)
        image = png(8, 12)
        class Provider(ScenarioProvider):
            def render(provider):
                provider.lab.publish_frame(provider.session['id'], image, 'image/png', 8, 12, 'portrait')
            def collect_app_logs(provider):
                return app_log()
        self.env.lab.devices['device']['factory'] = lambda: Provider(self.env.control)
        self.env.lab.devices['device']['capabilities']['automaticAppLogs'] = True
        # Only this exact generated PNG fixture is accepted. This is not native
        # decoder coverage, which remains in the separate platform tests.
        def fixture_validator(body, mime):
            self.assertEqual((body, mime), (image, 'image/png'))
            return {'width': 8, 'height': 12, 'mimeType': mime}
        self.workflow.packages.media_validator = fixture_validator

    def fixture(self, *, approved=False):
        issue, view = self.recorded()
        self.assertEqual(view['issue']['state'], 'complete')
        saved = self.save(issue, view['recording'])
        if approved:
            self.approve(issue, saved)
        package = self.workflow.export(self.owner, issue)['package']
        body = {'ticketId': 'QA-42', 'packageId': package['id'], 'baseRevision': 1,
                'baseSpecificationDigest': saved['specificationDigest']}
        return issue, body, package

    def link(self, issue, body, principal=None):
        return self.workflow.link_ticket_reference(principal or self.owner, issue, body)

    def history(self, issue, principal=None):
        return self.workflow.ticket_references(principal or self.owner, issue)


class TicketReferenceTests(TicketReferenceFixture, unittest.TestCase):
    def test_links_exact_existing_capsule_without_mutating_execution_or_approval(self):
        issue, body, package = self.fixture(approved=True)
        before = self.workflow.get(self.owner, issue)
        calls = copy.deepcopy(self.env.control['calls'])
        packages = self.workflow.packages.list('checkout', authorize=lambda: True)
        with mock.patch.object(self.workflow, 'export', side_effect=AssertionError('must not export')):
            linked = self.link(issue, body)
        reference = linked['reference']
        self.assertEqual(linked['status'], 'matches-current-revision')
        self.assertEqual(reference['mode'], 'reference-only')
        self.assertEqual(reference['sourceDigest'], '1' * 64)
        self.assertEqual(reference['projectDigest'], self.runtime.registration.project_digest)
        self.assertEqual(reference['capsuleDigest'], package['archiveDigest'])
        self.assertEqual(reference['originalRecordingDigest'], before['recording']['recordingDigest'])
        self.assertEqual(reference['linkedBy'], 'author_' + hashlib.sha256(b'owner').hexdigest()[:24])
        self.assertIs(type(reference['linkedAtMs']), int)
        self.assertEqual(self.workflow.get(self.owner, issue), before)
        self.assertEqual(self.env.control['calls'], calls)
        self.assertEqual(self.workflow.packages.list('checkout', authorize=lambda: True), packages)
        self.assertIsNone(before['issue']['packageId'])  # Local export is not the issue source.
        self.assertEqual(self.history(issue)['references'], [{**reference, 'status': linked['status']}])
        self.assertNotIn('owner', json.dumps(reference))
        self.assertNotIn(SECRET, json.dumps(reference))
        self.assertEqual(len(self.workflow._all('ticket-reference')), 1)

    def test_duplicate_requests_preserve_original_audit_and_do_not_use_quota(self):
        issue, body, _ = self.fixture()
        first = self.link(issue, body)
        self.assertEqual(self.link(issue, body, self.principals['maintainer']), first)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.link(issue, body), range(16)))
        self.assertTrue(all(result == first for result in results))
        self.assertEqual(len(self.workflow._all('ticket-reference')), 1)

    def test_contract_rejects_ticket_text_urls_secrets_and_unbounded_fields(self):
        issue, body, _ = self.fixture()
        invalid = [dict(body, ticketId=value) for value in (
            '', 'x' * 65, 'https://tracker/QA-42?token=secret', 'QA 42', 'QA\n42',
            'QA\r42', 'QA\t42', 'QA\x0042', 'QA\x7f42', 'QA-42\n', '../QA', '<script>',
            'QA.42', '_QA', '-42', 'ＱＡ-42', 'é', 42, True, None, [], {})]
        invalid += [dict(body, **value) for value in (
            {'baseRevision': True}, {'baseRevision': 0}, {'baseRevision': 65},
            {'baseSpecificationDigest': 'a' * 65}, {'packageId': 'package_' + 'a' * 64},
            {'packageId': 'QA-42'}, {'packageId': '123'},
            {'ticketText': 'private ticket'}, {'url': 'https://tracker/42'},
            {'sourceDigest': '1' * 64}, {'tenantId': 'untrusted'}, {'actor': 'admin'},
            {'approval': True}, {'oracle': 'secret'})]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ContractError):
                self.link(issue, candidate)
        self.assertEqual(self.workflow._all('ticket-reference'), [])
        self.assertNotIn('private ticket', '\n'.join(row[0] for row in self.workflow._db.execute(
            'SELECT body FROM documents')))

    def test_stale_revision_or_digest_cannot_create_a_reference(self):
        issue, body, _ = self.fixture()
        for change in ({'baseRevision': 2}, {'baseSpecificationDigest': '0' * 64}):
            with self.subTest(change=change), self.assertRaises(LiveError) as caught:
                self.link(issue, {**body, **change})
            self.assertEqual(caught.exception.code, 'specification_changed')
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_specification_is_required(self):
        issue, body, _ = self.fixture()
        document = self.workflow._get('issue', issue)
        document['specificationKey'] = None
        self.workflow._put('issue', issue, document)
        with self.assertRaises(LiveError) as caught:
            self.link(issue, body)
        self.assertEqual(caught.exception.code, 'specification_required')
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_viewer_operator_and_outsider_cannot_write_or_read_links(self):
        issue, body, _ = self.fixture()
        self.link(issue, body)
        self.access_store.create_identity('admin', 'operator')
        self.access_store.grant_membership('admin', 'checkout', 'operator', 'operator')
        self.access_store.create_identity('admin', 'outsider')
        for identity in ('operator', 'outsider'):
            credential = self.access_store.issue_principal_credential('admin', identity, lifetime_seconds=600)
            self.principals[identity] = self.access_store.authenticate_principal(credential['token'])
        for identity in ('viewer', 'operator', 'outsider'):
            with self.subTest(identity=identity):
                with self.assertRaises(AccessError): self.link(issue, body, self.principals[identity])
                with self.assertRaises(AccessError): self.history(issue, self.principals[identity])
        self.assertEqual(len(self.workflow._all('ticket-reference')), 1)

    def test_both_capabilities_are_checked_even_when_maintain_is_available(self):
        issue, body, _ = self.fixture()
        authorize = self.access_store.authorize
        def deny_export(principal, project, capability):
            if capability == 'export.read': raise AccessError('forbidden', 'Synthetic denial')
            return authorize(principal, project, capability)
        with mock.patch.object(self.access_store, 'authorize', side_effect=deny_export):
            with self.assertRaises(AccessError): self.link(issue, body)
            with self.assertRaises(AccessError): self.history(issue)
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_same_project_other_recording_and_wrong_package_specification_are_rejected(self):
        issue, body, _ = self.fixture()
        other, other_body, _ = self.fixture()
        with self.assertRaises(LiveError) as caught:
            self.link(issue, {**body, 'packageId': other_body['packageId']})
        self.assertEqual(caught.exception.code, 'ticket_reference_mismatch')
        view = self.workflow.get(self.owner, issue)
        saved = self.save(issue, view['recording'], base=1)
        with self.assertRaises(LiveError) as caught:
            self.link(issue, {**body, 'baseRevision': 2, 'baseSpecificationDigest': saved['specificationDigest']})
        self.assertEqual(caught.exception.code, 'ticket_reference_mismatch')
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_cross_project_package_and_wrong_registered_project_digest_are_rejected(self):
        issue, body, _ = self.fixture()
        recording, spec = example()
        archive = build_archive(recording, spec, lambda _: None)
        other = self.workflow.packages.import_archive(io.BytesIO(archive), size=len(archive),
            digest=hashlib.sha256(archive).hexdigest(), project_id='other', project_digest='f' * 64,
            expires_at_ms=2**45, authorize=lambda: True)
        with self.assertRaises(PackageError) as caught:
            self.link(issue, {**body, 'packageId': other['id']})
        self.assertEqual(caught.exception.code, 'package_not_found')
        self.workflow.packages._db.execute('UPDATE packages SET project_digest=? WHERE id=?', ('f' * 64, body['packageId']))
        with self.assertRaises(LiveError) as caught: self.link(issue, body)
        self.assertEqual(caught.exception.code, 'ticket_reference_mismatch')
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_issue_binding_cannot_be_moved_to_another_project(self):
        issue, body, _ = self.fixture()
        document = self.workflow._get('issue', issue)
        document['projectId'] = 'other'
        self.workflow._put('issue', issue, document)
        with self.assertRaises(ContractError): self.link(issue, body)
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_historical_original_project_revision_cannot_be_linked_as_current(self):
        recording, spec = example()
        recording['original']['projectRevision'] = 'historical'
        recording['recordingDigest'] = contracts.digest(recording['original'])
        spec['originalRecordingDigest'] = recording['recordingDigest']
        archive = build_archive(recording, spec, lambda _: None)
        issue = self.workflow.import_archive(self.owner, 'checkout', io.BytesIO(archive),
            size=len(archive), digest=hashlib.sha256(archive).hexdigest())['issue']
        with self.assertRaises(LiveError) as caught:
            self.link(issue['id'], {'ticketId': 'QA-history', 'packageId': issue['packageId'],
                'baseRevision': spec['revision'], 'baseSpecificationDigest': contracts.digest(spec)})
        self.assertEqual(caught.exception.code, 'ticket_reference_mismatch')
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_expired_or_removed_package_never_returns_reference_metadata(self):
        issue, body, package = self.fixture()
        self.link(issue, body)
        self.workflow.packages.tombstone(package['id'], 'checkout', authorize=lambda: True)
        self.assertEqual(self.history(issue), {'references': [], 'unavailableCount': 1})
        with self.assertRaises(PackageError): self.link(issue, body)
        # Independently exercise expiry, not just the tombstone state.
        another = self.workflow.export(self.owner, issue)['package']
        request = {**body, 'packageId': another['id']}
        self.link(issue, request)
        self.workflow.packages._db.execute('UPDATE packages SET expires_ms=0 WHERE id=?', (another['id'],))
        self.assertEqual(self.history(issue), {'references': [], 'unavailableCount': 2})
        with self.assertRaises(PackageError): self.link(issue, request)

    def test_source_retention_is_checked_even_when_package_row_is_live(self):
        issue, body, package = self.fixture()
        self.link(issue, body)
        with mock.patch.object(self.env.lab._evidence_store, 'lookup', return_value=None):
            with self.assertRaises(LiveError): self.history(issue)
            with self.assertRaises(LiveError): self.link(issue, body)

    def test_saved_revision_change_marks_history_stale_and_never_retargets(self):
        issue, body, _ = self.fixture(approved=True)
        first = self.link(issue, body)['reference']
        view = self.workflow.get(self.owner, issue)
        self.save(issue, view['recording'], base=1)
        after = self.history(issue)['references'][0]
        self.assertEqual(after, {**first, 'status': 'stale'})
        self.assertIsNone(self.workflow.get(self.owner, issue)['approval'])
        with self.assertRaises(LiveError): self.link(issue, body)
        self.assertEqual(self.workflow._all('ticket-reference'), [first])

    def test_same_reference_identity_cannot_silently_retarget_a_changed_archive(self):
        issue, body, package = self.fixture()
        first = self.link(issue, body)['reference']
        # The newly approved qualification changes archive bytes, not the spec.
        view = self.workflow.get(self.owner, issue)
        self.approve(issue, view)
        second = self.workflow.export(self.owner, issue)['package']
        self.assertNotEqual(second['archiveDigest'], package['archiveDigest'])
        self.workflow.packages._db.execute('UPDATE packages SET archive_digest=?,archive_bytes=? WHERE id=?',
            (second['archiveDigest'], second['bytes'], package['id']))
        with self.assertRaises(LiveError) as caught: self.link(issue, body)
        self.assertEqual(caught.exception.code, 'ticket_reference_changed')
        self.assertEqual(self.history(issue)['references'], [{**first, 'status': 'stale'}])

    def test_failed_write_rolls_back_and_retry_can_succeed(self):
        issue, body, _ = self.fixture()
        put = self.workflow._put
        def fail(kind, *args, **kwargs):
            result = put(kind, *args, **kwargs)
            if kind == 'ticket-reference': raise OSError('Synthetic persistence failure')
            return result
        with mock.patch.object(self.workflow, '_put', side_effect=fail), self.assertRaises(OSError):
            self.link(issue, body)
        self.assertEqual(self.workflow._all('ticket-reference'), [])
        self.link(issue, body)

    def test_post_write_revocation_rolls_back(self):
        issue, body, _ = self.fixture()
        put = self.workflow._put
        def revoke(kind, *args, **kwargs):
            result = put(kind, *args, **kwargs)
            if kind == 'ticket-reference':
                self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
            return result
        with mock.patch.object(self.workflow, '_put', side_effect=revoke), self.assertRaises(AccessError):
            self.link(issue, body)
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_post_write_package_expiry_rolls_back(self):
        issue, body, _ = self.fixture()
        put = self.workflow._put
        def expire(kind, *args, **kwargs):
            result = put(kind, *args, **kwargs)
            if kind == 'ticket-reference':
                self.workflow.packages._db.execute('UPDATE packages SET expires_ms=0 WHERE id=?', (body['packageId'],))
            return result
        with mock.patch.object(self.workflow, '_put', side_effect=expire), self.assertRaises(PackageError):
            self.link(issue, body)
        self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_post_write_revision_change_rolls_back_all_workflow_writes(self):
        issue, body, _ = self.fixture()
        before = self.workflow.get(self.owner, issue)
        put = self.workflow._put
        def change(kind, *args, **kwargs):
            result = put(kind, *args, **kwargs)
            if kind == 'ticket-reference': self.save(issue, before['recording'], base=1)
            return result
        with mock.patch.object(self.workflow, '_put', side_effect=change), self.assertRaises(LiveError):
            self.link(issue, body)
        self.assertEqual(self.workflow._all('ticket-reference'), [])
        self.assertEqual(self.workflow.get(self.owner, issue), before)

    def test_history_rechecks_revocation_and_expiry_after_reading(self):
        issue, body, _ = self.fixture()
        self.link(issue, body)
        read = self.workflow._all
        def expire(kind):
            result = read(kind)
            if kind == 'ticket-reference':
                self.workflow.packages._db.execute('UPDATE packages SET expires_ms=0 WHERE id=?', (body['packageId'],))
            return result
        with mock.patch.object(self.workflow, '_all', side_effect=expire):
            self.assertEqual(self.history(issue), {'references': [], 'unavailableCount': 1})
        def revoke(kind):
            result = read(kind)
            if kind == 'ticket-reference':
                self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
            return result
        with mock.patch.object(self.workflow, '_all', side_effect=revoke), self.assertRaises(AccessError):
            self.history(issue)

    def test_per_issue_quota_preserves_idempotent_reads_and_global_document_bound(self):
        issue, body, _ = self.fixture()
        first = self.link(issue, body)
        for number in range(1, 64): self.link(issue, {**body, 'ticketId': f'QA-{number + 100}'})
        with self.assertRaises(LiveError) as caught: self.link(issue, {**body, 'ticketId': 'QA-limit'})
        self.assertEqual(caught.exception.code, 'ticket_reference_limit')
        self.assertEqual(self.link(issue, body), first)
        self.assertEqual(len(self.history(issue)['references']), 64)
        other, request, _ = self.fixture()
        for number in range(8192 - self.workflow._db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]):
            self.workflow._db.execute('INSERT INTO documents VALUES(?,?,?)', ('quota-fixture', str(number), '{}'))
        with self.assertRaises(LiveError) as caught: self.link(other, request)
        self.assertEqual(caught.exception.code, 'issue_document_limit')
        self.assertEqual(len(self.workflow._all('ticket-reference')), 64)

    def test_concurrent_specification_save_cannot_commit_a_mixed_reference(self):
        issue, body, _ = self.fixture()
        view = self.workflow.get(self.owner, issue)
        barrier = threading.Barrier(2)
        def link():
            barrier.wait()
            try: return self.link(issue, body)
            except LiveError as error: return error.code
        def save():
            barrier.wait()
            return self.save(issue, view['recording'], base=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            linked, saved = pool.submit(link), pool.submit(save)
            result = linked.result(); saved.result()
        if isinstance(result, dict):
            reference = self.history(issue)['references'][0]
            self.assertEqual(reference['status'], 'stale')
            self.assertEqual(reference['baseRevision'], 1)
            self.assertEqual(reference['baseSpecificationDigest'], body['baseSpecificationDigest'])
        else:
            self.assertEqual(result, 'specification_changed')
            self.assertEqual(self.workflow._all('ticket-reference'), [])

    def test_link_does_not_make_an_import_locally_approved(self):
        issue, body, package = self.fixture()
        with self.workflow.packages.open_archive(package['id'], 'checkout', authorize=lambda: True) as reader:
            archive = reader.body
        imported = self.workflow.import_archive(self.owner, 'checkout', io.BytesIO(archive),
            size=len(archive), digest=hashlib.sha256(archive).hexdigest())['issue']['id']
        exported = self.workflow.export(self.owner, imported)['package']
        self.link(imported, {**body, 'packageId': exported['id']})
        view = self.workflow.get(self.owner, imported)
        self.assertTrue(view['issue']['imported'])
        self.assertIsNone(view['approval'])
        self.assertEqual(view['specificationDigest'], body['baseSpecificationDigest'])
        with self.assertRaises(LiveError) as caught:
            self.approve(imported, view)
        self.assertEqual(caught.exception.code, 'local_binding_required')

    def test_changed_project_registration_abstains_instead_of_reporting_current(self):
        issue, body, _ = self.fixture()
        self.link(issue, body)
        project = self.runtime.registration.project
        project['revision'] = 'r2'
        self.access_store.register_project('admin', project)
        with self.assertRaises(ContractError): self.history(issue)
        with self.assertRaises(ContractError): self.link(issue, body)
        self.assertEqual(len(self.workflow._all('ticket-reference')), 1)

    def test_persistence_survives_restart_without_restoring_authority(self):
        issue, body, _ = self.fixture()
        tickets = ('QA-42', 'qa-42', '123', '0', 'A', 'qa_one', 'Q_' + 'a' * 62)
        saved = {ticket_id: self.link(issue, dict(body, ticketId=ticket_id)) for ticket_id in tickets}
        self.assertEqual(len({item['reference']['id'] for item in saved.values()}), len(tickets))
        self.workflow.close()
        self.workflow = support.IssueWorkflow(self.env.root / 'browser-issues', self.env.lab, self.access, (self.runtime,))
        for ticket_id, first in saved.items():
            with self.subTest(ticket_id=ticket_id):
                self.assertEqual(first['reference']['ticketId'], ticket_id)
                self.assertEqual(self.link(issue, dict(body, ticketId=ticket_id)), first)
        self.assertEqual({item['ticketId'] for item in self.history(issue)['references']}, set(tickets))
        self.assertIsNone(self.workflow.get(self.owner, issue)['approval'])
        self.assertIsNone(self.workflow.get(self.owner, issue)['campaign'])


class TicketReferenceSourceTests(TicketReferenceFixture, unittest.TestCase):
    registered_source = False

    def test_missing_registered_source_digest_is_not_inferred_from_the_package(self):
        recording, spec = example()
        archive = build_archive(recording, spec, lambda _: None)
        issue = self.workflow.import_archive(self.owner, 'checkout', io.BytesIO(archive),
            size=len(archive), digest=hashlib.sha256(archive).hexdigest())['issue']
        with self.assertRaises(LiveError) as caught:
            self.link(issue['id'], {'ticketId': 'QA-source', 'packageId': issue['packageId'],
                'baseRevision': spec['revision'], 'baseSpecificationDigest': contracts.digest(spec)})
        self.assertEqual(caught.exception.code, 'ticket_reference_mismatch')
        self.assertEqual(self.workflow._all('ticket-reference'), [])


class TicketReferenceHttpTests(TicketReferenceFixture, unittest.TestCase):
    # Inherit fixture helpers, not the storage suite again.
    def setUp(self):
        super().setUp()
        self.server = LiveServer(self.env.lab, access=self.access, issue_workflow=self.workflow)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tokens = {identity: self.access_store.issue_principal_credential('admin', identity,
                      lifetime_seconds=600)['token'] for identity in ('owner', 'viewer')}
        self.client = IssueClient(self.server.origin, self.tokens['owner'])

    def tearDown(self):
        self.server.shutdown(); self.thread.join(2); self.server.server_close()
        super().tearDown()

    def cli(self, *arguments):
        result = subprocess.run([sys.executable, '-m', 'reproof', 'live-issues', *arguments,
                                '--server', self.server.origin, '--credential-stdin'],
            input=self.tokens['owner'] + '\n', text=True, capture_output=True, timeout=15,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.tokens['owner'], result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_real_authenticated_http_and_cli_share_idempotent_reference(self):
        issue, body, _ = self.fixture()
        exported = self.cli('export', issue, '--output', str(self.env.root / 'review.repro.zip'))
        self.assertTrue(Path(exported['exported']).is_file())
        body['packageId'] = exported['package']['id']
        path = '/api/release/issues/' + issue + '/ticket-references'
        linked = self.client.call(path, body)
        result = self.cli('ticket-link', issue, '--ticket-id', body['ticketId'], '--package-id', body['packageId'],
                          '--revision', str(body['baseRevision']), '--specification-digest', body['baseSpecificationDigest'])
        self.assertEqual(result, linked)
        self.assertEqual(self.cli('ticket-references', issue), self.client.call(path))
        self.assertEqual(len(self.workflow._all('ticket-reference')), 1)
        viewer = IssueClient(self.server.origin, self.tokens['viewer'])
        for payload in (None, body):
            with self.assertRaises(LiveError) as caught: viewer.call(path, payload)
            self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(LiveError) as caught: self.client.call(path, {**body, 'ticketText': SECRET})
        self.assertEqual(caught.exception.status, 400)

    def test_http_cli_and_storage_preserve_external_ticket_identity(self):
        issue, body, _ = self.fixture()
        path = '/api/release/issues/' + issue + '/ticket-references'
        tickets = ('QA-42', 'qa-42', '123', '0', 'A', 'qa_one', 'Q_' + 'a' * 62)
        ids = set()
        for ticket_id in tickets:
            with self.subTest(ticket_id=ticket_id):
                linked = self.client.call(path, dict(body, ticketId=ticket_id))
                result = self.cli('ticket-link', issue, '--ticket-id', ticket_id,
                    '--package-id', body['packageId'], '--revision', str(body['baseRevision']),
                    '--specification-digest', body['baseSpecificationDigest'])
                self.assertEqual(result, linked)
                self.assertEqual(result['reference']['ticketId'], ticket_id)
                ids.add(result['reference']['id'])
        self.assertEqual(len(ids), len(tickets))
        history = self.cli('ticket-references', issue)
        self.assertEqual(history, self.client.call(path))
        self.assertEqual({item['ticketId'] for item in history['references']}, set(tickets))
        self.assertEqual({item['ticketId'] for item in self.workflow._all('ticket-reference')}, set(tickets))
        for value in ('https://tracker/QA-42?token=' + SECRET, 'QA 42', 'QA\n42',
                      'x' * 65, 123, True, None, [], {}):
            with self.subTest(value=value), self.assertRaises(LiveError) as caught:
                self.client.call(path, dict(body, ticketId=value))
            self.assertEqual(caught.exception.status, 400)
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(self.workflow._all('ticket-reference')), len(tickets))

    def test_http_authentication_csrf_and_browser_logout_are_enforced(self):
        issue, body, _ = self.fixture()
        path = '/api/release/issues/' + issue + '/ticket-references'
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('GET', path, headers={'Origin': self.server.origin})
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 401)
        finally: connection.close()
        csrf = self.client.csrf
        self.client.csrf = None
        with self.assertRaises(LiveError) as caught: self.client.call(path, body)
        self.assertEqual(caught.exception.status, 403)
        self.client.csrf = csrf
        self.client.call(path, body)
        self.client.call('/api/auth/logout', {})
        with self.assertRaises(LiveError) as caught: self.client.call(path)
        self.assertEqual(caught.exception.status, 401)

    def test_linking_logs_does_not_approve_replay_or_enable_unconfigured_repair(self):
        issue, body, _ = self.fixture()
        path = '/api/release/issues/' + issue
        before = self.client.call(path)
        logs = before['recording']['original']['observations']
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['mimeType'], 'application/vnd.reproof.app-log+json')
        self.client.call(path + '/ticket-references', body)
        replay = {'deviceId': 'device', 'clientId': 'ticket_review', 'specificationDigest': body['baseSpecificationDigest']}
        with self.assertRaises(LiveError) as caught: self.client.call(path + '/replay', replay)
        self.assertEqual(caught.exception.code, 'approval_required')
        self.client.call(path + '/approve', {'revision': 1, 'specificationDigest': body['baseSpecificationDigest'], 'bindImported': False})
        self.client.call(path + '/replay', replay)
        after = self.wait(issue, states={'reproduced', 'failed', 'quarantined', 'unknown'})
        self.assertEqual(after['issue']['state'], 'reproduced')
        self.assertEqual(len(after['campaign']['attempts']), 3)
        self.assertEqual(after['specification'], before['specification'])
        self.assertEqual(after['recording'], before['recording'])
        self.assertEqual(len(before['recording']['original']['events']), 2)
        proposal = self.client.call(path + '/locator-proposals', {
            'baseRevision': 1, 'baseSpecificationDigest': body['baseSpecificationDigest'],
            'eventId': 'event_1', 'target': {'kind': 'accessibility-id', 'value': 'checkout_v2'}})
        self.assertEqual(proposal['proposal']['mode'], 'review-only')
        self.assertEqual(self.client.call(path)['specification'], before['specification'])
        with self.assertRaises(LiveError) as caught:
            self.client.call(path + '/repairs', {'requestId': 'ticket_review',
                'specificationDigest': body['baseSpecificationDigest'], 'mode': 'propose'})
        self.assertEqual(caught.exception.code, 'repair_not_configured')
        self.assertNotIn(SECRET, json.dumps(self.client.call(path + '/ticket-references')))

    def test_authenticated_http_and_cli_find_exact_ticket_without_issue_id(self):
        issue, body, _ = self.fixture()
        self.link(issue, body)
        path = '/api/release/projects/checkout/ticket-lookup'
        expected = self.workflow.lookup_ticket_references(self.owner, 'checkout', {'ticketId': 'QA-42'})
        self.assertEqual(self.client.call(path, {'ticketId': 'QA-42'}), expected)
        self.assertEqual(self.cli('ticket-lookup', '--project', 'checkout', '--ticket-id', 'QA-42'), expected)
        self.assertEqual(self.cli('ticket-lookup', '--project', 'checkout', '--ticket-id', 'qa-42'),
                         {'references': [], 'unavailableCount': 0})
        with self.assertRaises(LiveError) as caught: self.client.call(path)
        self.assertEqual(caught.exception.status, 404)

    def test_lookup_rejects_missing_auth_csrf_viewer_cross_project_and_logout(self):
        path = '/api/release/projects/checkout/ticket-lookup'
        body = {'ticketId': 'QA-42'}
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('POST', path, json.dumps(body),
                               {'Origin': self.server.origin, 'Content-Type': 'application/json'})
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 401)
        finally: connection.close()
        viewer = IssueClient(self.server.origin, self.tokens['viewer'])
        with self.assertRaises(LiveError) as caught: viewer.call(path, body)
        self.assertEqual(caught.exception.status, 403)
        csrf = self.client.csrf
        self.client.csrf = None
        with self.assertRaises(LiveError) as caught: self.client.call(path, body)
        self.assertEqual(caught.exception.status, 403)
        self.client.csrf = csrf
        with self.assertRaises(LiveError) as caught:
            self.client.call('/api/release/projects/other/ticket-lookup', body)
        self.assertEqual(caught.exception.status, 403)
        self.client.call('/api/auth/logout', {})
        with self.assertRaises(LiveError) as caught: self.client.call(path, body)
        self.assertEqual(caught.exception.status, 401)

    def test_lookup_invalid_payload_is_sanitized_and_read_only(self):
        before = self.workflow._all('ticket-reference')
        path = '/api/release/projects/checkout/ticket-lookup'
        for body in ({'ticketId': 'https://tracker/?token=' + SECRET}, {'ticketId': 123},
                     {'ticketId': 'QA-42', 'ticketText': SECRET}, {'ticketId': 'QA-42', 'limit': 1}):
            with self.subTest(body=body), self.assertRaises(LiveError) as caught: self.client.call(path, body)
            self.assertEqual(caught.exception.status, 400)
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.workflow._all('ticket-reference'), before)


class TicketLookupTests(TicketReferenceFixture, unittest.TestCase):
    def lookup(self, ticket='QA-42', project='checkout', principal=None):
        return self.workflow.lookup_ticket_references(principal or self.owner, project, {'ticketId': ticket})

    def test_exact_identity_returns_all_matching_issues_without_authority_effects(self):
        first, body, _ = self.fixture(approved=True)
        second, other, _ = self.fixture()
        expected = [self.link(first, body)['reference'], self.link(second, other)['reference']]
        for ticket in ('qa-42', 'QA-420', '123', '0', 'A', 'qa_one', 'Q_' + 'a' * 62):
            self.link(first, {**body, 'ticketId': ticket})
            self.assertEqual([item['ticketId'] for item in self.lookup(ticket)['references']], [ticket])
        before = [self.workflow.get(self.owner, issue) for issue in (first, second)]
        calls = copy.deepcopy(self.env.control['calls'])
        result = self.lookup()
        self.assertEqual(result, {'references': sorted(
            [{**item, 'status': 'matches-current-revision'} for item in expected], key=lambda item: item['id']),
            'unavailableCount': 0})
        self.assertEqual(self.lookup('QA'), {'references': [], 'unavailableCount': 0})
        self.assertEqual([self.workflow.get(self.owner, issue) for issue in (first, second)], before)
        self.assertEqual(self.env.control['calls'], calls)
        self.assertEqual(self.lookup(), result)

    def test_other_project_same_ticket_is_not_read_counted_or_returned(self):
        issue, body, _ = self.fixture()
        reference = self.link(issue, body)['reference']
        # Adversarial foreign index entries cannot trigger an issue/package read,
        # affect the result limit, or contribute unavailable metadata/counts.
        for number in range(65):
            foreign = {**reference, 'id': f'ticketref_foreign_{number}', 'projectId': 'other',
                       'issueId': 'foreign_issue', 'packageId': 'foreign_package'}
            self.workflow._put('ticket-reference', foreign['id'], foreign)
        self.assertEqual(self.lookup()['references'], [{**reference, 'status': 'matches-current-revision'}])
        self.assertEqual(self.lookup()['unavailableCount'], 0)
        with self.assertRaises(AccessError): self.lookup(project='other')

    def test_invalid_input_and_extra_fields_are_rejected(self):
        for body in ({}, {'ticketId': 'QA-42', 'projectId': 'other'}, {'ticketId': 'QA-42', 'limit': 1},
                     *({'ticketId': value} for value in ('', 'QA 42', 'https://secret', 'QA-42\n', 'x' * 65,
                                                         123, True, None, [], {}))):
            with self.subTest(body=body), self.assertRaises(ContractError):
                self.workflow.lookup_ticket_references(self.owner, 'checkout', body)
        for project in ('../checkout', 'Checkout', '', None):
            with self.subTest(project=project), self.assertRaises(ContractError): self.lookup(project=project)

    def test_empty_result_still_requires_both_capabilities_and_active_identity(self):
        with self.assertRaises(AccessError): self.lookup(principal=self.principals['viewer'])
        authorize = self.access_store.authorize
        def deny_export(principal, project, capability):
            if capability == 'export.read': raise AccessError('forbidden', 'Synthetic denial')
            return authorize(principal, project, capability)
        with mock.patch.object(self.access_store, 'authorize', side_effect=deny_export):
            with self.assertRaises(AccessError): self.lookup()
        self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
        with self.assertRaises(AccessError): self.lookup()

    def test_operator_outsider_and_post_scan_credential_revocation_are_denied(self):
        for identity in ('operator', 'outsider'):
            self.access_store.create_identity('admin', identity)
            if identity == 'operator':
                self.access_store.grant_membership('admin', 'checkout', identity, 'operator')
            credential = self.access_store.issue_principal_credential('admin', identity, lifetime_seconds=600)
            principal = self.access_store.authenticate_principal(credential['token'])
            with self.subTest(identity=identity), self.assertRaises(AccessError): self.lookup(principal=principal)
        read = self.workflow._all
        def revoke(kind):
            result = read(kind)
            if kind == 'ticket-reference':
                self.access_store.revoke_credential('admin', self.owner.credential_id)
            return result
        with mock.patch.object(self.workflow, '_all', side_effect=revoke):
            with self.assertRaises(AccessError): self.lookup()

    def test_package_retention_hides_only_exact_matches_and_source_expiry_fails_closed(self):
        issue, body, package = self.fixture()
        self.link(issue, body)
        self.link(issue, {**body, 'ticketId': 'QA-other'})
        self.workflow.packages.tombstone(package['id'], 'checkout', authorize=lambda: True)
        self.assertEqual(self.lookup(), {'references': [], 'unavailableCount': 1})
        self.assertEqual(self.lookup('missing'), {'references': [], 'unavailableCount': 0})
        with mock.patch.object(self.env.lab._evidence_store, 'lookup', return_value=None):
            with self.assertRaises(LiveError): self.lookup()

    def test_stale_revision_preserves_original_identity(self):
        issue, body, _ = self.fixture()
        reference = self.link(issue, body)['reference']
        self.save(issue, self.workflow.get(self.owner, issue)['recording'], base=1)
        self.assertEqual(self.lookup()['references'], [{**reference, 'status': 'stale'}])

    def test_issue_project_binding_and_registration_changes_fail_closed(self):
        issue, body, _ = self.fixture()
        self.link(issue, body)
        document = self.workflow._get('issue', issue)
        self.workflow._put('issue', issue, {**document, 'projectId': 'other'})
        with self.assertRaises(ContractError): self.lookup()
        self.workflow._put('issue', issue, document)
        project = self.runtime.registration.project
        project['revision'] = 'changed'
        self.access_store.register_project('admin', project)
        with self.assertRaises(ContractError): self.lookup()

    def test_bound_rejects_overflow_before_reading_any_issue_and_never_truncates(self):
        issue, body, _ = self.fixture()
        reference = self.link(issue, body)['reference']
        for number in range(63):
            item = {**reference, 'id': f'ticketref_limit_{number}'}
            self.workflow._put('ticket-reference', item['id'], item)
        self.assertEqual(len(self.lookup()['references']), 64)
        item = {**reference, 'id': 'ticketref_overflow'}
        self.workflow._put('ticket-reference', item['id'], item)
        # Unavailable references count against the raw-match work bound too.
        self.workflow.packages.tombstone(body['packageId'], 'checkout', authorize=lambda: True)
        with mock.patch.object(self.workflow, '_ticket_snapshot', side_effect=AssertionError('unbounded read')):
            with self.assertRaises(LiveError) as caught: self.lookup()
        self.assertEqual(caught.exception.code, 'ticket_lookup_limit')

    def test_final_pass_rechecks_earlier_packages_and_revocation(self):
        issues = []
        for _ in range(2):
            issue, body, _ = self.fixture()
            self.link(issue, body)
            issues.append((issue, body))
        issues.sort()
        read = self.workflow._read_ticket_references
        def expire(principal, issue_id, candidates=None):
            result = read(principal, issue_id, candidates)
            if issue_id == issues[-1][0]:
                self.workflow.packages._db.execute('UPDATE packages SET expires_ms=0 WHERE id=?',
                                                  (issues[0][1]['packageId'],))
            return result
        with mock.patch.object(self.workflow, '_read_ticket_references', side_effect=expire):
            result = self.lookup()
        self.assertEqual(result['unavailableCount'], 1)
        self.assertEqual([item['issueId'] for item in result['references']], [issues[-1][0]])
        def revoke(principal, issue_id, candidates=None):
            result = read(principal, issue_id, candidates)
            self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
            return result
        with mock.patch.object(self.workflow, '_read_ticket_references', side_effect=revoke):
            with self.assertRaises(AccessError): self.lookup()

    def test_restart_preserves_exact_lookup(self):
        issue, body, _ = self.fixture()
        self.link(issue, body)
        before = self.lookup()
        self.workflow.close()
        self.workflow = support.IssueWorkflow(self.env.root / 'browser-issues', self.env.lab, self.access, (self.runtime,))
        self.assertEqual(self.lookup(), before)

    def test_deadline_fails_closed_even_for_empty_result(self):
        with mock.patch('reproof.live.ticket_references.time.monotonic', side_effect=[0, 31]):
            with self.assertRaises(LiveError) as caught: self.lookup()
        self.assertEqual(caught.exception.code, 'ticket_lookup_timeout')


if __name__ == '__main__': unittest.main()
