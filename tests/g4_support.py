import copy
from pathlib import Path
import tempfile
import threading
import time

from reproof import contracts
from reproof.fixtures import (AdapterCapabilities, FixtureCoordinator,
                                LoopbackFixtureAdapter)
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.issue_sessions import FixturePreparation
from reproof.live.model import Lab
from reproof.qualification import ScenarioRegistry
from reproof.scenario_runner import (ObservationEvidence, ObservationRegistry,
                                       ScenarioRunner, StaticVariableResolver,
                                       VariableResolverRegistry)
from tests.test_clock_sync import FakeClock
from tests.test_fixture_allocations import (LoopbackService, collection_policy,
                                            project_document)


SECRET = "g4-secret-value-never-persist"


class ScenarioProvider:
    def __init__(self, control):
        self.control = control

    def start(self, session, lab):
        self.session = session;self.lab = lab;self.closed = False
        self.render()

    def render(self):
        self.lab.publish_frame(self.session["id"], b"<svg>g4</svg>",
                               "image/svg+xml", 400, 800, "portrait")

    def resolve_locator(self, target):
        frame = self.lab.frame(self.session["id"])
        return {"target": copy.deepcopy(target), "x": .5, "y": .5,
                "frameId": frame["id"],
                "geometryVersion": frame["geometryVersion"],
                "providerIncarnation": self.lab.release_binding(
                    self.session["id"], "owner")["providerIncarnation"],
                "observedAtMs": int(time.time() * 1000)}

    def execute(self, action, payload):
        self.control["calls"].append((action, copy.deepcopy(payload)))
        if action == "text":self.control["last_text"] = payload["value"]
        if self.control.get("unknown_input"):
            raise RuntimeError("uncertain")
        self.render()
        return {"ok": True, "timing": "best-effort"}

    def execute_operation(self, action, payload, *, operation_id, frame=None):
        self.control.setdefault("operation_ids",[]).append(operation_id)
        return self.execute(action,payload)

    def close(self):
        if self.control.get("fail_close"):
            raise RuntimeError("cleanup")
        self.closed = True


class SnapshotObservationAdapter:
    provider_incarnation = "observation_adapter"

    def __init__(self, value="error", *, classes=("snapshot",), truncated=False,
                 stale=False, contradiction=False):
        self.value=value;self.coverage_classes=set(classes)
        self.truncated=truncated;self.stale=stale;self.contradiction=contradiction

    def observe(self, request):
        requirement=request.requirement
        start=requirement["windowMs"]["start"]
        end=requirement["windowMs"]["end"]
        if self.stale:start-=70_000;end-=70_000
        coverage=requirement["class"]
        values={"text": self.value}
        if self.contradiction:
            values={"text": [(start, "error"), (end, "success")]}
        envelope={
            "schemaVersion":1,"id":request.observation_id,
            "providerIncarnation":self.provider_incarnation,
            "applicationId":request.application_id,
            "intervalMs":{"start":start,"end":end},"clockUncertaintyMs":0,
            "scope":requirement["scope"],"targets":["checkout"],
            "properties":list(requirement["properties"]),
            "limits":{"nodes":10,"bytes":4096,"depth":4},
            "truncated":self.truncated,"errors":[],
            "completeness":"partial" if self.truncated else "complete",
            "coverage":coverage,
        }
        if coverage == "sampled":
            envelope["samplesMs"]=[start] if start==end else [start,end]
        return ObservationEvidence(envelope,values)


def runtime_policy():
    return {"schemaVersion":1,"class":"mobile-device","network":"loopback",
            "candidateCanEdit":False,"enforcedControls":["host_authority"],
            "attestations":[]}


def specification(original, *, revision=1, observation_class="snapshot"):
    def coverage():
        value={"class":observation_class,"windowMs":{"start":0,"end":0},
               "maxUncertaintyMs":0,"maxAgeMs":60_000,"scope":"root",
               "properties":["text"]}
        if observation_class=="sampled":value["samplingIntervalMs"]=100
        return value
    return {
        "schemaVersion":1,"id":"approved_scenario","revision":revision,
        "originalRecordingDigest":original["recordingDigest"],
        "actions":[
            {"eventId":"event_1","action":"tap",
             "target":{"kind":"accessibility-id","value":"checkout"},
             "parameters":{}},
            {"eventId":"event_2","action":"text",
             "target":{"kind":"accessibility-id","value":"account"},
             "parameters":{"variableId":"secret_text"}},
        ],
        "waits":[],"bindings":[{"name":"account","variableId":"secret_text"}],
        "fixtures":["seed_account"],
        "assertions":[
            {"id":"defect_visible","role":"defect",
             "predicate":{"kind":"property","observationId":"screen",
                          "property":"text","operator":"equals","value":"error"},
             "coverage":coverage(),"windowMs":0,"stabilityMs":0},
            {"id":"expected_visible","role":"expected",
             "predicate":{"kind":"property","observationId":"screen",
                          "property":"text","operator":"equals","value":"success"},
             "coverage":coverage(),"windowMs":0,"stabilityMs":0},
        ],
        "provenance":{"kind":"authored","source":"local-review",
                      "author":"qa_admin","revision":str(revision)},
    }


def qualification(project, original, spec, plan):
    return {
        "schemaVersion":1,"projectDigest":contracts.digest(project),
        "projectRevision":project["revision"],
        "recordingDigest":original["recordingDigest"],
        "specificationDigest":contracts.digest(spec),"originalBuildId":"original",
        "fixtureRules":[{"fixtureId":"seed_account",
                         "equivalenceDigest":plan.equivalence_digest}],
        "observationRequirements":[
            {"assertionId":item["id"],"observationId":"screen",
             "coverage":copy.deepcopy(item["coverage"])}
            for item in spec["assertions"]],
        "runtimePolicyDigest":contracts.digest(runtime_policy()),
        "validationRecipeIds":["regression_ui"],
        "attemptBudget":{"original":3,"candidate":3,"total":6},
    }


class G4Environment:
    def __init__(self, observation_adapter=None, *, recording_capacity_bytes=64*1024*1024):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.remote=LoopbackService(self.root);self.control={"calls":[]}
        def factory():
            provider=ScenarioProvider(self.control);self.control["provider"]=provider
            return provider
        self.device={"id":"device","name":"Synthetic","platform":"ios",
                     "kind":"demo","factory":factory,
                     "capabilities":{"actions":["tap","long_press","swipe","text",
                                                    "home","back","rotate","launch","terminate"],
                                     "inputMode":"gesture-batch","media":"demo-svg",
                                     "locatorKinds":["accessibility-id","resource-id"],
                                     "applicationIdentity":{"bundle":"com.example.app",
                                                            "artifactDigest":"0"*64},
                                     "recordingTextTarget":{"kind":"accessibility-id",
                                                            "value":"account"}}}
        self.lab=Lab([self.device],self.root/"lab",
                     recording_clock_sync=ClockSynchronizer(FakeClock()),
                     recording_wall_clock_ms=lambda:int(time.time()*1000))
        self.project=project_document()
        self.registration=self.lab.register_recording_project(
            self.project,collection_policy(),capacity_bytes=recording_capacity_bytes,
            journal_headroom_bytes=512*1024)
        self.fixtures=FixtureCoordinator(self.root/"fixtures")
        self.adapter=LoopbackFixtureAdapter(
            "fixture_service",f"http://127.0.0.1:{self.remote.port}",
            capabilities=AdapterCapabilities(True,True,60_000))
        self.plan=self.fixtures.register_plan(
            self.registration,application_id="ios_app",fixture_id="seed_account",
            adapter=self.adapter,check_recipe_ids=("check_account",),
            cleanup_recipe_id="cleanup_account")
        self.registry=ScenarioRegistry(self.root/"specs")
        self.variables=VariableResolverRegistry(self.registration)
        self.variables.register("secret_text",StaticVariableResolver(SECRET))
        self.observations=ObservationRegistry(self.registration)
        self.observations.register("screen",observation_adapter or SnapshotObservationAdapter())
        self.runner=ScenarioRunner(self.lab,self.registry,self.variables,self.observations)
        self.service=self.lab.create_issue_session_service(
            self.fixtures,root=self.root/"issues",scenario_registry=self.registry,
            scenario_runner=self.runner)

    def preparations(self, payload=None):
        return [FixturePreparation(self.plan,{} if payload is None else payload)]

    def record_original(self):
        handle=self.service.start_prepared_recording(
            device_id="device",owner="owner",controller_id="browser",
            registration=self.registration,application_id="ios_app",build_id="original",
            preparations=self.preparations())
        session=self.lab.get_session(handle.session_id,"owner")
        frame=self.lab.frame(handle.session_id,"owner")
        commands=[
            ({"action":"tap","payload":{"x":.5,"y":.5}},
             {"action":"tap","target":{"kind":"accessibility-id","value":"checkout"},
              "parameters":{}}),
            ({"action":"text","payload":{"value":SECRET}},
             {"action":"text","target":{"kind":"accessibility-id","value":"account"},
              "parameters":{"variableId":"secret_text"}}),
        ]
        for sequence,(live,typed) in enumerate(commands,1):
            self.service.input(handle,{
                "controllerId":session["controllerId"],"epoch":session["epoch"],
                "sequence":sequence,"commandId":f"original_command_{sequence}",
                "frameId":frame["id"],"geometryVersion":frame["geometryVersion"],
                **live},recording_input=typed)
            frame=self.lab.frame(handle.session_id,"owner")
        stopped=self.service.stop(handle)
        if stopped["issue"]["state"]!="complete":raise AssertionError(stopped)
        return stopped["recording"]

    def approve(self, original, **spec_kwargs):
        spec=specification(original,**spec_kwargs)
        q=qualification(self.project,original,spec,self.plan)
        approved=self.registry.register(
            self.registration,original,spec,q,runtime_policy(),fixture_plans=(self.plan,))
        return approved

    def close(self):
        self.registry.close();self.fixtures.close();self.lab.close_all()
        self.remote.close();self.temp.cleanup()
