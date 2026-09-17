"""Repair proposals use actual G4 baselines and never imply candidate verification."""
import copy
import json
import threading
import unittest

from reproloop.agents import AgentUnavailable
from reproloop.repair_journal import RepairJournal
from tests.g9_support import RepairEnvironment
from tests.test_project_repair import BEFORE, EDIT, PRODUCT


class LocalProposalDouble:
    provider_id = 'local-patch'
    external = False

    def __init__(self, edits=None, effect=None):
        self.edits = [EDIT] if edits is None else edits
        self.effect = effect
        self.calls = 0

    def propose_project(self, packet, *, cancellation):
        self.calls += 1
        self.packet = copy.deepcopy(packet)
        if self.effect:
            self.effect(cancellation)
        return copy.deepcopy(self.edits)


class ProjectRepairJobTests(unittest.TestCase):
    def setUp(self):
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)
        self.baseline = self.env.baseline()
        self.journal = RepairJournal(self.env.root / 'repairs', disk_limit=256 * 1024 * 1024)
        self.addCleanup(self.journal.close)

    def repair(self, agent=None, **kwargs):
        from reproloop.project_repair import ProjectRepair
        self.agent = agent or LocalProposalDouble()
        return ProjectRepair(self.env.source, self.env.registry, self.env.engine, self.journal,
            self.agent, build_recipe_id='build_app', validation_recipe_ids=('regression_ui',), **kwargs)

    def create(self, repair, *, mode='propose', request_id='request', campaign_id=None):
        return repair.create(self.env.approved, campaign_id=campaign_id or self.baseline['campaignId'],
            issue_id='issue', owner_id='owner', request_id=request_id, mode=mode)

    def run_job(self, repair, **kwargs):
        job = self.create(repair, **kwargs)
        return repair.execute(job['id'], self.env.approved, authorize=lambda: True)

    def test_general_fix_publishes_bounded_patch_and_unchanged_baseline(self):
        repair = self.repair(); result = self.run_job(repair)
        self.assertEqual(result['status'], 'proposal-ready')
        self.assertFalse(result['result']['verified'])
        self.assertEqual(result['result']['changedPaths'], [PRODUCT])
        self.assertEqual(result['plan']['baselineDigest'], __import__('reproloop').contracts.digest(self.baseline))
        self.assertEqual(result['plan']['attemptBudget'], self.env.approved.qualification['attemptBudget'])
        self.assertEqual(result['provider']['kind'], 'local-test-adapter')
        output = repair.proposal(result['id'])
        self.assertEqual(output['edits'], [EDIT])
        self.assertIn('+    if ready', output['patch'])
        self.assertEqual((self.env.root / 'source' / PRODUCT).read_bytes(), BEFORE)
        self.assertNotIn('protected independent harness', json.dumps(self.agent.packet))
        self.assertIsNone(result['result']['afterEvidence'])

    def test_missing_execution_environment_blocks_before_proposal_or_candidate(self):
        repair = self.repair(); result = self.run_job(repair, mode='verify')
        self.assertEqual((result['status'], result['reason']), ('blocked', 'protected_verification_unavailable'))
        self.assertEqual(self.agent.calls, 0)
        self.assertEqual(result['outputs'], {})
        self.assertFalse(repair.availability()['verificationAvailable'])

    def test_unqualified_baseline_and_unavailable_agent_are_terminal(self):
        repair = self.repair()
        result = self.run_job(repair, campaign_id='missing')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'baseline_unqualified')
        self.assertEqual(self.agent.calls, 0)
        def unavailable(_): raise AgentUnavailable('provider output must stay private')
        repair = self.repair(LocalProposalDouble(effect=unavailable))
        result = self.run_job(repair, request_id='unavailable')
        self.assertEqual((result['status'], result['reason']), ('failed', 'agent_unavailable'))
        self.assertEqual(self.agent.calls, 1)
        self.assertNotIn('provider output', json.dumps(result))

    def test_noop_and_assertion_edits_never_publish_outputs(self):
        for index, edits in enumerate(([dict(EDIT, new=EDIT['old'])],
                [dict(EDIT, path='checks/ui.json', old='true', new='false')])):
            with self.subTest(index=index):
                repair = self.repair(LocalProposalDouble(edits))
                result = self.run_job(repair, request_id='invalid_' + str(index))
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['outputs'], {})

    def test_changed_original_is_preserved_but_candidate_is_rejected(self):
        def mutate(_): (self.env.root / 'source' / PRODUCT).write_bytes(b'concurrent user edit')
        repair = self.repair(LocalProposalDouble(effect=mutate)); result = self.run_job(repair)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'original_changed')
        self.assertEqual(result['outputs'], {})
        self.assertEqual((self.env.root / 'source' / PRODUCT).read_bytes(), b'concurrent user edit')

    def test_cancel_and_revocation_prevent_late_result_publication(self):
        for index, revoke in enumerate((False, True)):
            with self.subTest(revoke=revoke):
                allowed = [True]
                def cancel(_):
                    if revoke: allowed[0] = False
                    else: self.journal.cancel(job['id'])
                repair = self.repair(LocalProposalDouble(effect=cancel))
                job = self.create(repair, request_id='cancel_' + str(index))
                result = repair.execute(job['id'], self.env.approved, authorize=lambda: allowed[0])
                self.assertEqual(result['status'], 'cancelled')
                self.assertTrue(result['cancelRequested'])
                self.assertEqual(result['outputs'], {})

    def test_unapproved_external_transfer_never_calls_provider(self):
        agent = LocalProposalDouble(); agent.external = True; agent.provider_id = 'claude'
        repair = self.repair(agent); result = self.run_job(repair)
        self.assertEqual((result['status'], result['reason']), ('blocked', 'ai_transfer_denied'))
        self.assertEqual(agent.calls, 0)

    def test_idempotency_cannot_restart_terminal_work_or_change_mode(self):
        from reproloop.repair_journal import RepairJournalError
        repair = self.repair(); result = self.run_job(repair)
        self.assertEqual(self.create(repair)['id'], result['id'])
        self.assertEqual(repair.execute(result['id'], self.env.approved, authorize=lambda: True), result)
        self.assertEqual(self.agent.calls, 1)
        with self.assertRaises(RepairJournalError): self.create(repair, mode='verify')


if __name__ == '__main__': unittest.main()
