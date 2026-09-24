"""Authorization, immutable approval and real prepared replay orchestration."""
import copy
import hashlib
import io
import json
import time
import unittest
from unittest import mock

from reproof import contracts
from reproof.live.access import AccessController, AccessError, AccessStore
from reproof.live.issue_workflow import IssueWorkflow, ProjectIssueRuntime
from reproof.live.model import LiveError
from tests.g4_support import G4Environment, runtime_policy, specification, SECRET


class IssueWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.access_store = AccessStore(self.env.root / "coordinator-v2")
        store = self.access_store
        store.bootstrap_administrator("admin")
        store.register_project("admin", self.env.project)
        self.principals = {}
        for identity, roles in (("owner", ("operator", "maintainer")), ("viewer", ("viewer",)),
                                ("maintainer", ("maintainer",))):
            store.create_identity("admin", identity)
            for role in roles:
                store.grant_membership("admin", "checkout", identity, role)
            token = store.issue_principal_credential("admin", identity, lifetime_seconds=600)["token"]
            self.principals[identity] = store.authenticate_principal(token)
        store.assign_device("admin", "device", project_id="checkout")
        self.access = AccessController(store)
        self.access.bind_project(self.env.registration)
        self.runtime = ProjectIssueRuntime(self.env.registration, self.env.service,
            tuple(self.env.preparations()), runtime_policy(), ("regression_ui",))
        self.workflow = IssueWorkflow(self.env.root / "browser-issues", self.env.lab,
                                      self.access, (self.runtime,))
        self.owner = self.principals["owner"]

    def tearDown(self):
        self.workflow.close()
        self.access_store.close()
        self.env.close()

    def start(self):
        return self.workflow.start(self.owner, {"projectId": "checkout", "applicationId": "ios_app",
            "buildId": "original", "deviceId": "device", "clientId": "browser",
            "preparationIds": ["seed_account"], "unprepared": False})["issue"]["id"]

    def wait(self, issue_id, *, states):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            document = self.workflow.get(self.owner, issue_id)
            if document["issue"]["state"] in states:
                return document
            time.sleep(.01)
        self.fail("Issue did not reach the expected state")

    def recorded(self):
        issue_id = self.start()
        active=self.wait(issue_id, states={"recording"})
        session=self.env.lab.get_session(active['issue']['sessionId'],'owner')
        for sequence,action in enumerate((
            {'action':'tap','parameters':{},'target':{'kind':'accessibility-id','value':'checkout'}},
            {'action':'text','parameters':{'variableId':'secret_text'},
             'target':{'kind':'accessibility-id','value':'account'}},
        ),1):
            self.workflow.input(self.owner,issue_id,{'input':action,'operationId':f'manual_{sequence}',
                'sequence':sequence,'controllerId':session['controllerId'],'epoch':session['epoch']})
        self.workflow.stop(self.owner, issue_id)
        return issue_id, self.wait(issue_id, states={"complete", "failed", "quarantined"})

    def save(self, issue_id, recording, base=0):
        spec = specification(recording)
        return self.workflow.save_specification(self.owner, issue_id, {"baseRevision": base,
            **{key: spec[key] for key in ("actions", "waits", "bindings", "fixtures", "assertions")}})

    def approve(self, issue_id, saved, **kwargs):
        return self.workflow.approve(self.owner, issue_id, {
            "specificationDigest": saved["specificationDigest"],
            "revision": saved["specification"]["revision"], "bindImported": False, **kwargs})

    def test_project_catalog_is_filtered_and_does_not_disclose_variable_values(self):
        result = self.workflow.projects(self.principals["viewer"])
        project = result["projects"][0]
        self.assertEqual(project["id"], "checkout")
        self.assertNotIn("replay.execute", project["capabilities"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(project["devices"][0]["id"], "device")

    def test_issue_view_loads_the_actual_g3_vendor_manifest(self):
        from dataclasses import replace
        from reproof.live.video import VideoFrameSink
        from tests.test_video_state_machine import FakeEncoder, limits
        from tests.test_issue_media import png
        from tests.g4_support import ScenarioProvider
        class Provider(ScenarioProvider):
            def render(self):
                self.lab.publish_frame(self.session['id'], png(8, 12), 'image/png', 8, 12, 'portrait')
        self.env.lab.devices['device']['factory'] = lambda: Provider(self.env.control)
        self.workflow.runtimes['checkout'] = replace(self.runtime, frame_sink_factory=lambda: VideoFrameSink(
            self.env.lab._evidence_store, self.env.root / 'video', encoder=FakeEncoder(), limits=limits()))
        issue_id = self.start()
        self.wait(issue_id, states={'recording'})
        self.workflow.stop(self.owner, issue_id)
        final = self.wait(issue_id, states={'complete', 'failed', 'quarantined'})
        self.assertIsNotNone(final['video'])
        self.assertEqual(final['video']['recordingId'], final['recording']['original']['recordingId'])
        self.assertEqual(final['video']['status'], 'complete')

    def test_invalid_client_is_rejected_before_fixture_preparation(self):
        before = len(self.env.control["calls"])
        with self.assertRaises(LiveError):
            self.workflow.start(self.owner, {"projectId": "checkout", "applicationId": "ios_app",
                "buildId": "original", "deviceId": "device", "clientId": "invalid/client",
                "preparationIds": ["seed_account"], "unprepared": False})
        self.assertEqual(len(self.env.control["calls"]), before)
        self.assertEqual(self.workflow.list(self.owner)["issues"], [])

    def test_issue_publication_failure_withdraws_its_imported_package(self):
        from tests.test_issue_package import example
        from reproof.issue_package import build_archive
        recording, spec = example()
        body = build_archive(recording, spec, lambda _: None)
        original_put = self.workflow._put
        def fail_issue(kind, *args, **kwargs):
            if kind == "issue":
                raise LiveError("issue_store_limit", "Issue index is full", 409)
            return original_put(kind, *args, **kwargs)
        with mock.patch.object(self.workflow, "_put", side_effect=fail_issue):
            with self.assertRaises(LiveError):
                self.workflow.import_archive(self.owner, "checkout", io.BytesIO(body),
                    size=len(body), digest=hashlib.sha256(body).hexdigest())
        self.assertEqual(self.workflow.packages.list("checkout", authorize=lambda: True), [])

    def test_stop_finishes_prepared_recording_and_approval_binds_exact_revision(self):
        issue_id, view = self.recorded()
        self.assertEqual(view["issue"]["state"], "complete")
        self.assertEqual(len(view["recording"]["original"]["preparation"]), 2)
        saved = self.save(issue_id, view["recording"])
        wrong = copy.deepcopy(saved)
        wrong["specificationDigest"] = "f" * 64
        with self.assertRaises(LiveError): self.approve(issue_id, wrong)
        approval = self.approve(issue_id, saved)["approval"]
        self.assertEqual(approval["specificationDigest"], contracts.digest(saved["specification"]))
        changed = self.save(issue_id, view["recording"], base=1)
        self.assertEqual(changed["specification"]["revision"], 2)
        self.assertIsNone(self.workflow.get(self.owner, issue_id)["approval"])
        with self.assertRaises(LiveError): self.approve(issue_id, saved)

    def test_explicitly_unprepared_start_preserves_unknown_conditions_in_original(self):
        issue_id = self.workflow.start(self.owner, {'projectId': 'checkout', 'applicationId': 'ios_app',
            'buildId': 'original', 'deviceId': 'device', 'clientId': 'unprepared_qa',
            'preparationIds': [], 'unprepared': True})['issue']['id']
        self.wait(issue_id, states={'recording'})
        self.workflow.stop(self.owner, issue_id)
        result = self.wait(issue_id, states={'complete', 'failed', 'quarantined'})
        self.assertIn({'kind': 'preparation_unknown', 'sequence': 0}, result['recording']['original']['unknowns'])
        self.assertEqual(result['recording']['original']['preparation'], [])
        self.assertEqual(result['recording']['status'], 'frozen-incomplete')
        self.assertEqual(result['lifecycle']['deviceCleanup'], 'complete')

    def test_viewer_cannot_author_or_approve_and_maintainer_cannot_operate(self):
        issue_id, view = self.recorded()
        saved = self.save(issue_id, view["recording"])
        with self.assertRaises(AccessError):
            self.workflow.approve(self.principals["viewer"], issue_id, {
                "revision": 1, "specificationDigest": saved["specificationDigest"], "bindImported": False})
        self.approve(issue_id, saved)
        with self.assertRaises(AccessError):
            self.workflow.replay(self.principals["maintainer"], issue_id, {
                "deviceId": "device", "clientId": "replay_browser",
                "specificationDigest": saved["specificationDigest"]})

    def test_real_approved_replay_consumes_one_frozen_budget_without_resetting_it(self):
        issue_id, view = self.recorded()
        saved = self.save(issue_id, view["recording"])
        self.approve(issue_id, saved)
        before = len(self.env.control["calls"])
        request = {"deviceId": "device", "clientId": "replay_browser",
                   "specificationDigest": saved["specificationDigest"]}
        self.workflow.replay(self.owner, issue_id, request)
        final = self.wait(issue_id, states={"reproduced", "failed", "unknown", "quarantined"})
        self.assertEqual(final["issue"]["state"], "reproduced", final["campaign"])
        self.assertEqual(len(final["campaign"]["attempts"]), 3)
        self.assertEqual(len(self.env.control["calls"]) - before, 6)
        self.assertNotIn(SECRET, json.dumps(final))
        self.assertEqual(self.workflow.replay(self.owner, issue_id, request)["campaign"], final["campaign"])
        self.assertEqual(len(self.env.control["calls"]) - before, 6)

    def test_revoked_operator_recording_is_stopped_and_its_fixture_is_cleaned(self):
        issue_id=self.start()
        self.wait(issue_id,states={'recording'})
        self.access_store.revoke_membership('admin','checkout','owner','operator')
        final=self.wait(issue_id,states={'cancelled','quarantined','failed'})
        self.assertEqual(final['issue']['state'],'cancelled')
        self.assertEqual(final['issue']['reason'],'authorization_revoked')
        self.assertEqual(final['lifecycle']['deviceCleanup'],'complete')
        self.assertTrue(all(item['status']=='complete' for item in final['lifecycle']['cleanup']))
        self.assertEqual(self.env.lab.list_devices()[0]['state'],'available')
        self.assertNotIn(SECRET, json.dumps(final))


if __name__ == "__main__": unittest.main()
