"""Review-only proposals against a temporary coordinator and simulated provider."""
import copy
import json
import unittest
from unittest import mock

from reproof import contracts
from reproof.core import ContractError
from reproof.live.access import AccessError
from reproof.live.model import LiveError
from tests import test_issue_workflow as support


class LocatorProposalTests(unittest.TestCase):
    setUp = support.IssueWorkflowTests.setUp
    tearDown = support.IssueWorkflowTests.tearDown
    start = support.IssueWorkflowTests.start
    wait = support.IssueWorkflowTests.wait
    recorded = support.IssueWorkflowTests.recorded
    save = support.IssueWorkflowTests.save
    approve = support.IssueWorkflowTests.approve

    def fixture(self):
        issue, view = self.recorded()
        saved = self.save(issue, view['recording'])
        self.approve(issue, saved)
        spec = saved['specification']
        action = next(item for item in spec['actions'] if 'target' in item)
        body = {'baseRevision': spec['revision'], 'baseSpecificationDigest': saved['specificationDigest'],
                'eventId': action['eventId'], 'target': {'kind': action['target']['kind'], 'value': 'checkout_v2'}}
        return issue, spec, body

    def proposal(self, issue, body):
        return self.workflow.propose_locator(self.owner, issue, body)['proposal']

    def decide(self, issue, proposal, decision='apply'):
        return self.workflow.decide_locator(self.owner, issue, {'proposalId': proposal['id'], 'decision': decision})

    def test_propose_reject_is_inert_and_audited(self):
        issue, spec, body = self.fixture()
        before = self.workflow.get(self.owner, issue)
        calls = copy.deepcopy(self.env.control['calls'])
        proposal = self.proposal(issue, body)
        self.decide(issue, proposal, 'reject')
        after = self.workflow.get(self.owner, issue)
        for key in ('specification', 'approval', 'recording', 'campaign'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(self.env.control['calls'], calls)
        history = self.workflow.locator_proposals(self.owner, issue)['proposals']
        self.assertEqual(history[0]['decision']['decision'], 'reject')
        self.assertNotIn('owner', json.dumps(history))
        with self.assertRaises(LiveError): self.decide(issue, proposal)

    def test_apply_only_changes_target_and_revokes_old_approval(self):
        issue, spec, body = self.fixture()
        before = self.workflow.get(self.owner, issue)
        calls = copy.deepcopy(self.env.control['calls'])
        proposal = self.proposal(issue, body)
        result = self.decide(issue, proposal)['decision']
        after = self.workflow.get(self.owner, issue)
        expected = copy.deepcopy(spec)
        next(a for a in expected['actions'] if a['eventId'] == body['eventId'])['target'] = body['target']
        for key in ('actions', 'waits', 'bindings', 'fixtures', 'assertions', 'originalRecordingDigest'):
            self.assertEqual(after['specification'][key], expected[key])
        self.assertEqual(after['specification']['revision'], spec['revision'] + 1)
        self.assertIsNone(after['approval'])
        self.assertIsNone(after['campaign'])
        self.assertEqual(after['recording'], before['recording'])
        self.assertEqual(self.workflow._get('specification', before['issue']['specificationKey']), spec)
        self.assertEqual(result['resultingSpecificationDigest'], after['specificationDigest'])
        self.assertEqual(self.env.control['calls'], calls)
        with self.assertRaises(LiveError): self.decide(issue, proposal)
        self.assertEqual(self.workflow.get(self.owner, issue)['specification'], after['specification'])

    def test_roles_and_project_boundary(self):
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        for identity in ('viewer',):
            with self.assertRaises(AccessError):
                self.workflow.propose_locator(self.principals[identity], issue, body)
            with self.assertRaises(AccessError):
                self.workflow.decide_locator(self.principals[identity], issue, {'proposalId': proposal['id'], 'decision': 'apply'})
        other, _view = self.recorded()
        with self.assertRaises(LiveError): self.decide(other, proposal)

    def test_stale_revision_and_digest_are_rejected(self):
        issue, spec, body = self.fixture()
        for patch in ({'baseRevision': 2}, {'baseSpecificationDigest': '0'*64}, {'eventId': 'unknown'}):
            with self.assertRaises(LiveError): self.proposal(issue, {**body, **patch})
        proposal = self.proposal(issue, body)
        self.workflow.save_specification(self.owner, issue, {'baseRevision': spec['revision'],
            **{key: spec[key] for key in ('actions','waits','bindings','fixtures','assertions')}})
        with self.assertRaises(LiveError): self.decide(issue, proposal)
        self.assertIsNone(self.workflow.locator_proposals(self.owner, issue)['proposals'][0]['decision'])

    def test_unknown_fields_and_unbounded_or_text_candidates_rejected(self):
        issue, spec, body = self.fixture()
        invalid = [dict(body, assertions=[]), dict(body, evidence='secret raw log'),
            dict(body, target={'kind':'accessibility-id','value':'x'*161}),
            dict(body, target={'kind':'accessibility-id','value':'password=secret value'}),
            dict(body, target={'kind':'accessibility-id','value':'x','role':'button'}),
            dict(body, target={'kind':'semantic','value':'x'})]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ContractError):
                self.proposal(issue, candidate)
        self.assertEqual(self.workflow.locator_proposals(self.owner, issue)['proposals'], [])

    def test_atomic_rollback_when_decision_persistence_fails(self):
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        before = self.workflow.get(self.owner, issue)
        original = self.workflow._put
        def fail(kind, *args, **kwargs):
            if kind == 'locator-decision': raise OSError('simulated storage failure')
            return original(kind, *args, **kwargs)
        with mock.patch.object(self.workflow, '_put', side_effect=fail), self.assertRaises(OSError):
            self.decide(issue, proposal)
        after = self.workflow.get(self.owner, issue)
        self.assertEqual(after['specification'], before['specification'])
        self.assertEqual(after['approval'], before['approval'])
        self.assertEqual(self.workflow._all('locator-decision'), [])
        self.decide(issue, proposal)

    def test_revert_is_a_new_revision_needing_approval(self):
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        self.decide(issue, proposal)
        after = self.workflow.get(self.owner, issue)
        revert = self.proposal(issue, {**body, 'baseRevision': after['specification']['revision'],
            'baseSpecificationDigest': after['specificationDigest'], 'target': proposal['previousTarget']})
        self.decide(issue, revert)
        final = self.workflow.get(self.owner, issue)
        self.assertEqual(final['specification']['actions'], spec['actions'])
        self.assertEqual(final['specification']['revision'], 3)
        self.assertIsNone(final['approval'])
        self.assertEqual(len(self.workflow.locator_proposals(self.owner, issue)['proposals']), 2)

    def test_concurrent_application_creates_one_revision_and_one_decision(self):
        from concurrent.futures import ThreadPoolExecutor
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        def apply():
            try:
                self.decide(issue, proposal)
                return 'applied'
            except LiveError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: apply(), range(2)))
        self.assertCountEqual(outcomes, ['applied', 'locator_proposal_decided'])
        self.assertEqual(self.workflow.get(self.owner, issue)['specification']['revision'], 2)
        self.assertEqual(len(self.workflow._all('locator-decision')), 1)

    def test_revocation_before_commit_rolls_back_application(self):
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        before = self.workflow.get(self.owner, issue)
        original = self.workflow._put
        def revoke(kind, *args, **kwargs):
            result = original(kind, *args, **kwargs)
            if kind == 'issue':
                self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
            return result
        with mock.patch.object(self.workflow, '_put', side_effect=revoke), self.assertRaises(AccessError):
            self.decide(issue, proposal)
        after = self.workflow.get(self.owner, issue)
        self.assertEqual(after['specification'], before['specification'])
        self.assertEqual(after['approval'], before['approval'])
        self.assertEqual(self.workflow._all('locator-decision'), [])

    def test_proposals_are_bounded_and_cannot_create_arbitrary_audit_payloads(self):
        issue, spec, body = self.fixture()
        for _ in range(64): self.proposal(issue, body)
        with self.assertRaises(LiveError) as error: self.proposal(issue, body)
        self.assertEqual(error.exception.code, 'locator_proposal_limit')
        proposal = self.workflow.locator_proposals(self.owner, issue)['proposals'][0]
        with self.assertRaises(ContractError):
            self.workflow.decide_locator(self.owner, issue, {'proposalId': proposal['id'],
                'decision':'reject', 'reason': 'private user text'})
        self.assertEqual(len(self.workflow._all('locator-proposal')), 64)

    def test_operator_cannot_write_proposals_and_unrelated_viewer_cannot_read(self):
        issue, spec, body = self.fixture()
        self.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
        with self.assertRaises(AccessError): self.proposal(issue, body)
        self.access_store.revoke_membership('admin', 'checkout', 'viewer', 'viewer')
        with self.assertRaises(AccessError):
            self.workflow.locator_proposals(self.principals['viewer'], issue)

    def test_http_routes_reuse_authorized_methods_without_execution(self):
        from reproof.live.issue_http import dispatch
        issue, spec, body = self.fixture()
        handler = mock.Mock()
        handler.server.issue_workflow = self.workflow
        handler.command = 'POST'
        handler.body.return_value = body
        dispatch(handler, self.owner, ['api','release','issues',issue,'locator-proposals'])
        proposal = handler.respond.call_args.args[1]['proposal']
        handler.body.return_value = {'proposalId': proposal['id'], 'decision': 'reject'}
        dispatch(handler, self.owner, ['api','release','issues',issue,'locator-decisions'])
        self.assertEqual(handler.respond.call_args.args[1]['decision']['decision'], 'reject')
        handler.command = 'GET'
        dispatch(handler, self.owner, ['api','release','issues',issue,'locator-proposals'])
        self.assertEqual(len(handler.respond.call_args.args[1]['proposals']), 1)

    def test_history_rechecks_authorization_before_returning(self):
        issue, spec, body = self.fixture()
        self.proposal(issue, body)
        original = self.workflow._all
        def revoke(kind):
            result = original(kind)
            if kind == 'locator-decision':
                self.access_store.revoke_membership('admin', 'checkout', 'viewer', 'viewer')
            return result
        with mock.patch.object(self.workflow, '_all', side_effect=revoke), self.assertRaises(AccessError):
            self.workflow.locator_proposals(self.principals['viewer'], issue)

    def test_apply_rejects_active_replay(self):
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        document = self.workflow._get('issue', issue)
        document['state'] = 'replaying'
        self.workflow._put('issue', issue, document)
        with self.assertRaises(LiveError): self.decide(issue, proposal)
        self.assertEqual(self.workflow._all('locator-decision'), [])
        document['state'] = 'complete'
        self.workflow._put('issue', issue, document)

    def test_history_survives_reopening_without_restoring_approval(self):
        issue, spec, body = self.fixture()
        proposal = self.proposal(issue, body)
        self.decide(issue, proposal)
        before = self.workflow.locator_proposals(self.owner, issue)
        self.workflow.close()
        self.workflow = support.IssueWorkflow(self.env.root / 'browser-issues', self.env.lab,
                                             self.access, (self.runtime,))
        self.assertEqual(self.workflow.locator_proposals(self.owner, issue), before)
        self.assertIsNone(self.workflow.get(self.owner, issue)['approval'])
