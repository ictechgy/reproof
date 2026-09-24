"""General proposal packets and adapters cannot acquire tools or implicit transfer permission."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof.agents import AgentUnavailable
from reproof.project_repair import RepairError
from tests.test_project_repair import BEFORE, EDIT, PRODUCT, source_fixture


class ProjectAgentTests(unittest.TestCase):
    def test_external_source_and_specification_need_exact_transfer_policy(self):
        from reproof.project_repair import proposal_packet
        project, source, _ = source_fixture()
        specification = {'actions': [{'action': 'tap'}], 'assertions': [{'expected': 'success'}],
                         'waits': [], 'provenance': {'author': 'not-for-provider'}}
        policy = {'schemaVersion': 1, 'projectDigest': __import__('reproof.contracts', fromlist=['digest']).digest(project),
            'providerId': 'claude', 'approvedTransfer': True, 'sourcePaths': [PRODUCT],
            'specificationFields': ['actions', 'assertions', 'waits']}
        for bad in (None, policy):
            with self.assertRaises(RepairError):
                proposal_packet(project, source, specification, provider_id='claude', external=True, policy=bad)
        project['evidencePolicy']['aiEligible'] = True
        from reproof.contracts import digest
        policy['projectDigest'] = digest(project)
        packet = proposal_packet(project, source, specification, provider_id='claude', external=True, policy=policy)
        self.assertEqual(packet['sourceFiles'], {PRODUCT: BEFORE.decode()})
        self.assertNotIn('not-for-provider', json.dumps(packet))
        self.assertNotIn('protected independent harness', json.dumps(packet))
        for change in ({'providerId': 'different'}, {'sourcePaths': ['checks/ui.json']},
                       {'approvedTransfer': False}, {'projectDigest': 'a' * 64},
                       {'specificationFields': ['provenance']}):
            with self.subTest(change=change), self.assertRaises(RepairError):
                proposal_packet(project, source, specification, provider_id='claude', external=True,
                                policy=dict(policy, **change))

    def test_patch_adapter_is_bounded_strict_local_input_and_not_ai(self):
        from reproof.agents import ProjectPatchAgent
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'proposal.json'
            path.write_text(json.dumps({'edits': [EDIT]}))
            agent = ProjectPatchAgent(path)
            self.assertFalse(agent.external)
            self.assertEqual(agent.provider_id, 'local-patch')
            self.assertEqual(agent.propose_project({}, cancellation=threading.Event()), [EDIT])
            path.write_text('{"edits": [], "edits": []}')
            with self.assertRaises(AgentUnavailable):
                agent.propose_project({}, cancellation=threading.Event())

    def test_claude_project_adapter_uses_prompt_only_and_does_not_expose_outputs(self):
        from reproof.agents import ClaudeProjectAgent
        observed = {}
        def run(command, work, **kwargs):
            observed.update(command=command, work=work, **kwargs)
            return json.dumps({'edits': [EDIT]})
        agent = ClaudeProjectAgent(executable='/owned/tools/claude')
        with mock.patch('reproof.agents.run_command', side_effect=run):
            self.assertEqual(agent.propose_project({'sourceFiles': {PRODUCT: BEFORE.decode()}},
                cancellation=threading.Event()), [EDIT])
        self.assertEqual(observed['command'][observed['command'].index('--tools') + 1], '')
        self.assertIn('--no-session-persistence', observed['command'])
        self.assertNotIn(BEFORE.decode(), json.dumps(agent.last_receipt))
        self.assertFalse(Path(observed['work']).exists())

    def test_cancelled_command_never_starts_and_running_command_is_stopped(self):
        from reproof.repair import run_command, CommandError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); marker = root / 'started'
            cancel = threading.Event(); cancel.set()
            with self.assertRaises(CommandError):
                run_command([sys.executable, '-c', 'raise SystemExit(0)'], root, cancellation=cancel)
            cancel.clear(); errors = []
            def execute():
                try:
                    run_command([sys.executable, '-c',
                        'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(30)'],
                        root, cancellation=cancel, timeout=5)
                except CommandError: errors.append(True)
            thread = threading.Thread(target=execute); thread.start()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(marker.exists()); cancel.set(); thread.join(3)
            self.assertFalse(thread.is_alive()); self.assertEqual(errors, [True])


if __name__ == '__main__': unittest.main()
