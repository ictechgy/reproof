"""Trusted local composition for the shared issue workflow.

Configuration registers fixed loopback service adapters and environment variable
names. Archives and HTTP requests never supply these registrations or paths.
Company integrations may implement the same G4 adapter interfaces in a trusted
coordinator process; this loader does not import arbitrary modules or commands.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

from .. import contracts
from ..contracts.versions import exact
from ..fixtures import AdapterCapabilities, FixtureCoordinator, LoopbackFixtureAdapter
from ..issue_package import _json, canonical
from ..qualification import ScenarioRegistry
from ..scenario_runner import (
    ObservationEvidence, ObservationRegistry, ObservationRequest, ScenarioRunner,
    VariableResolverRegistry,
)
from .issue_sessions import FixturePreparation
from .issue_workflow import IssueWorkflow, ProjectIssueRuntime
from .model import check


def _endpoint(value):
    check(type(value) is str, "issue_configuration", "Invalid observation endpoint", 400)
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise contracts.ContractError("Observation endpoint must be an explicit loopback address") from None
    check(parsed.scheme == "http" and address.is_loopback and port is not None and 1 <= port <= 65535
          and parsed.username is None and parsed.password is None and parsed.path in {"", "/"}
          and not parsed.query and not parsed.fragment,
          "issue_configuration", "Observation endpoint must be an explicit loopback address", 400)
    return str(address), port


@dataclass(frozen=True, slots=True)
class EnvironmentVariableResolver:
    name: str
    variable_type: str

    def resolve(self, _context):
        # Resolve at the input/replay boundary, never while listing a project
        # or loading its configuration. No dotenv or credential file is read.
        value = os.environ.get(self.name)
        check(value is not None, "variable_unavailable", "Registered variable is unavailable", 409)
        if self.variable_type == "integer":
            check(re.fullmatch(r"-?(0|[1-9][0-9]{0,10})", value) is not None,
                  "variable_invalid", "Registered variable has the wrong type", 400)
            return int(value)
        if self.variable_type == "boolean":
            check(value in {"true", "false"}, "variable_invalid", "Registered variable has the wrong type", 400)
            return value == "true"
        return value


class LoopbackObservationAdapter:
    """Read actual evidence from a configured service, without adding coverage."""
    def __init__(self, *, project_digest, observation_id, provider_incarnation, base_url, coverage_classes):
        contracts.validate_digest(project_digest)
        contracts.validate_id(observation_id)
        contracts.validate_id(provider_incarnation)
        check(type(coverage_classes) in (list, tuple) and 1 <= len(coverage_classes) <= 3
              and all(type(item) is str for item in coverage_classes)
              and len(set(coverage_classes)) == len(coverage_classes)
              and set(coverage_classes) <= {"snapshot", "sampled", "continuous"},
              "issue_configuration", "Invalid observation coverage registration", 400)
        self.host, self.port = _endpoint(base_url)
        self.project_digest = project_digest
        self.observation_id = observation_id
        self.provider_incarnation = provider_incarnation
        self.coverage_classes = frozenset(coverage_classes)

    def observe(self, request):
        check(type(request) is ObservationRequest and request.observation_id == self.observation_id
              and request.requirement["class"] in self.coverage_classes,
              "observation_binding", "Observation request is not registered", 409)
        binding = request.lab.release_binding(request.session_id, request.owner)
        check(binding["projectDigest"] == self.project_digest,
              "observation_binding", "Observation project revision changed", 409)
        timeout = min(20, request.deadline_monotonic - time.monotonic())
        check(timeout > 0, "observation_timeout", "Observation deadline elapsed", 408)
        request_id = "observation_" + uuid.uuid4().hex
        body = canonical({"schemaVersion": 1, "requestId": request_id,
            "projectDigest": self.project_digest, "observationId": self.observation_id,
            "applicationId": request.application_id, "sessionId": request.session_id,
            "anchorMs": request.anchor_ms, "requirement": request.requirement})
        check(len(body) <= 64 * 1024, "observation_limit", "Observation request is too large", 413)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        transport = {}; expired = threading.Event()
        def expire():
            expired.set()
            sock = transport.get("socket")
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        timer = threading.Timer(timeout, expire); timer.daemon = True; timer.start()
        try:
            connection.connect(); transport["socket"] = connection.sock
            check(not expired.is_set(), "observation_timeout", "Observation deadline elapsed", 408)
            connection.request("POST", "/observations", body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
            response = connection.getresponse()
            check(response.status == 200 and response.getheader("Content-Type") == "application/json",
                  "observation_unavailable", "Observation service did not return evidence", 409)
            raw = response.read(2 * 1024 * 1024 + 1)
            check(not expired.is_set() and len(raw) <= 2 * 1024 * 1024,
                  "observation_limit", "Observation response exceeded its bound", 409)
            value = _json(raw)
        except (OSError, http.client.HTTPException):
            raise contracts.ContractError("Observation service is unavailable") from None
        finally:
            timer.cancel(); connection.close()
        exact(value, ("schemaVersion", "requestId", "envelope", "values", "absentProperties"))
        check(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
              and value["requestId"] == request_id, "observation_binding", "Observation response changed", 409)
        envelope = contracts.validate_observation(value["envelope"])
        check(envelope["id"] == self.observation_id and envelope["applicationId"] == request.application_id
              and envelope["providerIncarnation"] == self.provider_incarnation
              and envelope["coverage"] in self.coverage_classes,
              "observation_binding", "Observation evidence changed identity", 409)
        # G4 validates property values, intervals, absence, age and requested
        # coverage. A partial/truncated envelope stays partial here.
        absent = value["absentProperties"]
        check(type(value["values"]) is dict and type(absent) is list and len(absent) <= 128,
              "observation_invalid", "Observation values are invalid", 409)
        return ObservationEvidence(envelope, value["values"], tuple(absent))


def load_issue_configuration(path):
    source = Path(path)
    check(source.is_file() and not source.is_symlink() and source.stat().st_size <= 1024 * 1024,
          "issue_configuration", "Issue configuration is unavailable or too large", 400)
    with source.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    check(len(raw) <= 1024 * 1024, "issue_configuration", "Issue configuration is too large", 400)
    document = _json(raw)
    exact(document, ("schemaVersion", "kind", "projects"), ("repairStorageBytes",))
    if 'repairStorageBytes' in document:
        check(type(document['repairStorageBytes']) is int
              and 66 * 1024 * 1024 <= document['repairStorageBytes'] <= 512 * 1024 ** 3,
              'issue_configuration', 'Invalid repair storage bound', 400)
    check(type(document["schemaVersion"]) is int and document["schemaVersion"] == 1
          and document["kind"] == "reproloop-issue-runtime"
          and type(document["projects"]) is list and 1 <= len(document["projects"]) <= 128,
          "issue_configuration", "Invalid issue runtime configuration", 400)
    seen = set()
    for project in document["projects"]:
        exact(project, ("projectId", "projectDigest", "runtimePolicy", "validationRecipeIds", "fixtures", "variables", "observations"), ("repair",))
        contracts.validate_id(project["projectId"]); contracts.validate_digest(project["projectDigest"])
        check(project["projectId"] not in seen, "issue_configuration", "Duplicate issue project", 400)
        seen.add(project["projectId"]); contracts.validate_execution_policy(project["runtimePolicy"])
        for key, limit in (("fixtures", 8), ("variables", 256), ("observations", 128), ("validationRecipeIds", 128)):
            check(type(project[key]) is list and len(project[key]) <= limit,
                  "issue_configuration", "Issue registrations exceed their bound", 400)
        for fixture in project["fixtures"]:
            exact(fixture, ("applicationId", "fixtureId", "endpointId", "baseUrl", "checkRecipeIds", "cleanupRecipeId", "payload"))
            for key in ("applicationId", "fixtureId", "endpointId", "cleanupRecipeId"): contracts.validate_id(fixture[key])
            _endpoint(fixture["baseUrl"])
            check(type(fixture["checkRecipeIds"]) is list and len(fixture["checkRecipeIds"]) <= 32
                  and type(fixture["payload"]) is dict and len(canonical(fixture["payload"])) <= 64 * 1024,
                  "issue_configuration", "Invalid preparation registration", 400)
            for identifier in fixture["checkRecipeIds"]: contracts.validate_id(identifier)
        for variable in project["variables"]:
            exact(variable, ("variableId", "environment")); contracts.validate_id(variable["variableId"])
            check(type(variable["environment"]) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", variable["environment"]),
                  "issue_configuration", "Invalid variable environment name", 400)
        for observation in project["observations"]:
            exact(observation, ("observationId", "providerIncarnation", "baseUrl", "coverage"))
            LoopbackObservationAdapter(project_digest=project["projectDigest"], observation_id=observation["observationId"],
                provider_incarnation=observation["providerIncarnation"], base_url=observation["baseUrl"], coverage_classes=observation["coverage"])
        for identifier in project["validationRecipeIds"]: contracts.validate_id(identifier)
        if 'repair' in project: _validate_repair_configuration(project['repair'], project['projectDigest'])
    return document


def _validate_repair_configuration(value, project_digest):
    from ..project_repair import RepairError, _paths, validate_transfer_policy
    from ..repair_diagnostics import validate_diagnostic_policy
    exact(value, ('sourceRoot', 'sourcePaths', 'protectedPaths', 'originalArtifactRoot',
        'originalArtifactPaths', 'artifactIdentity', 'buildRecipeId', 'agent'),
        ('transferPolicy', 'diagnosticPolicy', 'protectedProfileId'))
    def absolute(path):
        check(type(path) is str and 1 <= len(path) <= 4096 and '\0' not in path and Path(path).is_absolute(),
              'issue_configuration', 'Repair paths must be explicit absolute paths', 400)
    absolute(value['sourceRoot']); absolute(value['originalArtifactRoot'])
    try:
        _paths(value['sourcePaths']); _paths(value['protectedPaths'], allow_empty=True)
        _paths(value['originalArtifactPaths'])
        if 'diagnosticPolicy' in value:
            validate_diagnostic_policy(value['diagnosticPolicy'], project_digest=project_digest)
    except RepairError:
        raise contracts.ContractError('Invalid repair path manifest') from None
    contracts.validate_id(value['buildRecipeId'])
    if 'protectedProfileId' in value:
        contracts.validate_id(value['protectedProfileId'])
    check(type(value['artifactIdentity']) is str and value['artifactIdentity'] in {'file-sha256', 'tree-sha256'},
          'issue_configuration', 'Invalid original artifact identity', 400)
    agent = value['agent']
    check(type(agent) is dict, 'issue_configuration', 'Invalid proposal adapter', 400)
    if agent.get('kind') == 'local-patch':
        exact(agent, ('kind', 'patchFile')); absolute(agent['patchFile'])
        check('transferPolicy' not in value, 'issue_configuration', 'Local proposals do not use an AI transfer policy', 400)
    else:
        exact(agent, ('kind', 'model'), ('executable',))
        check(agent['kind'] == 'claude' and type(agent['model']) is str
              and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', agent['model']) is not None,
              'issue_configuration', 'An explicitly selected proposal model is required', 400)
        if 'executable' in agent: absolute(agent['executable'])
        try:
            policy = validate_transfer_policy(value.get('transferPolicy'), project_digest=project_digest,
                provider_id='claude', source_paths=value['sourcePaths'])
        except RepairError:
            raise contracts.ContractError('Invalid proposal transfer policy') from None
        diagnostic = value.get('diagnosticPolicy')
        selection = policy.get('diagnostics')
        check((diagnostic is None and selection is None) or diagnostic is not None
            and type(selection) is dict and selection['policyDigest'] == contracts.digest(diagnostic)
            and set(selection['eventFields']) <= set(diagnostic['eventFields']),
            'issue_configuration', 'Diagnostic transfer must match the registered fields and policy', 400)


class IssueRuntimeBundle:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.workflow = None; self.fixtures = []; self.registries = []
        self.protected_repairs = None
        self.protected_recovery = None

    def close(self):
        if self.protected_recovery is not None:
            self.protected_recovery.close(deadline_monotonic=time.monotonic()+10)
            self.protected_recovery = None
        if self.protected_repairs is not None:
            self.protected_repairs.revoke()
        if self.workflow is not None:
            self.workflow.close(); self.workflow = None
        if self.protected_repairs is not None:
            self.protected_repairs.close(); self.protected_repairs = None
        for resource in reversed(self.registries + self.fixtures): resource.close()
        self.registries.clear(); self.fixtures.clear()


def compose_issue_workflow(lab, access, configuration, *, root, video_helper=None, media_helper=None,
                           defer_repairs=False, protected_repairs=None):
    check(type(defer_repairs) is bool and (not defer_repairs or protected_repairs is None),
          'issue_configuration', 'Invalid deferred repair composition', 400)
    bundle = IssueRuntimeBundle(root); runtimes = []; root = Path(root)
    try:
        for configured in configuration["projects"]:
            registration = access.registration(configured["projectId"])
            check(registration.project_digest == configured["projectDigest"],
                  "stale_project", "Issue runtime project revision changed", 409)
            project = registration.project
            directory = root / "runtimes" / project["id"]
            fixtures = FixtureCoordinator(directory / "fixtures"); bundle.fixtures.append(fixtures)
            registry = ScenarioRegistry(directory / "specifications"); bundle.registries.append(registry)
            variables = VariableResolverRegistry(registration); observations = ObservationRegistry(registration)
            declarations = {item["id"]: item for item in project["variables"]}
            for variable in configured["variables"]:
                check(variable["variableId"] in declarations, "issue_configuration", "Variable is not registered", 400)
                variables.register(variable["variableId"], EnvironmentVariableResolver(variable["environment"], declarations[variable["variableId"]]["type"]))
            for observation in configured["observations"]:
                observations.register(observation["observationId"], LoopbackObservationAdapter(
                    project_digest=registration.project_digest, observation_id=observation["observationId"],
                    provider_incarnation=observation["providerIncarnation"], base_url=observation["baseUrl"], coverage_classes=observation["coverage"]))
            preparations = []
            recipes = {item["id"]: item for item in project["fixtures"]}
            for fixture in configured["fixtures"]:
                check(fixture["fixtureId"] in recipes, "issue_configuration", "Fixture is not registered", 400)
                caps = recipes[fixture["fixtureId"]]["capabilities"]
                adapter = LoopbackFixtureAdapter(fixture["endpointId"], fixture["baseUrl"], capabilities=AdapterCapabilities(
                    caps["remoteFencing"], caps["terminalStatus"], caps["idempotencyRetentionMs"]))
                plan = fixtures.register_plan(registration, application_id=fixture["applicationId"], fixture_id=fixture["fixtureId"],
                    adapter=adapter, check_recipe_ids=tuple(fixture["checkRecipeIds"]), cleanup_recipe_id=fixture["cleanupRecipeId"])
                preparations.append(FixturePreparation(plan, copy.deepcopy(fixture["payload"])))
            runner = ScenarioRunner(lab, registry, variables, observations)
            service = lab.create_issue_session_service(fixtures, root=directory / "sessions", scenario_registry=registry, scenario_runner=runner)
            factory = None
            if video_helper is not None:
                from .video import AVFoundationSegmentEncoder, VideoLimits
                AVFoundationSegmentEncoder(video_helper, lab._evidence_store, VideoLimits())
                factory = lambda: lab.create_video_sink(helper=video_helper)
            runtimes.append(ProjectIssueRuntime(registration, service, tuple(preparations), configured["runtimePolicy"],
                tuple(configured["validationRecipeIds"]), factory))
        bundle.workflow = IssueWorkflow(root / "workflow", lab, access, runtimes, media_helper=media_helper)
        if not defer_repairs:
            compose_issue_repairs(bundle, configuration, protected_repairs=protected_repairs)
        return bundle
    except Exception:
        bundle.close(); raise


def compose_issue_repairs(bundle, configuration, *, protected_repairs=None):
    """Attach jobs after fixed local adapters bind to the newly created runners.

    The normal proposal path calls this immediately. Protected native adapters
    use ``defer_repairs=True``, assemble against these exact runtimes, then pass
    their process-owned composition here. No callback is loaded from JSON.
    """
    from ..repair_composition import ProtectedRepairComposition
    from ..repair_execution import RepairExecutionError
    check(type(bundle) is IssueRuntimeBundle and bundle.root is not None and bundle.workflow is not None
        and not bundle.workflow._closed and bundle.workflow.repairs is None and bundle.protected_repairs is None
        and (protected_repairs is None or type(protected_repairs) is ProtectedRepairComposition),
        'repair_configuration', 'Issue repair composition is unavailable or already attached', 409)
    if protected_repairs is not None:
        try:
            protected_repairs.claim(bundle)
        except RepairExecutionError:
            raise contracts.ContractError('Protected repair composition is already owned or closed') from None
        bundle.protected_repairs = protected_repairs
    try:
        repairs = []
        for configured in configuration['projects']:
            if 'repair' not in configured: continue
            from ..agents import ClaudeProjectAgent, ProjectPatchAgent
            from ..project_repair import RepairError, RepairSource, validate_transfer_policy
            from ..repair_diagnostics import validate_diagnostic_policy
            from .project_repair_jobs import ProjectRepairConfiguration, ProjectRepairJobs
            value = configured['repair']
            _validate_repair_configuration(value, configured['projectDigest'])
            runtime = bundle.workflow._runtime(configured['projectId'])
            check(runtime.registration.project_digest == configured['projectDigest']
                  and contracts.digest(runtime.runtime_policy) == contracts.digest(configured['runtimePolicy'])
                  and tuple(sorted(runtime.validation_recipe_ids)) == tuple(sorted(configured['validationRecipeIds'])),
                  'repair_configuration', 'Repair project differs from the composed issue runtime', 409)
            project = runtime.registration.project
            try:
                source = RepairSource(project, value['sourceRoot'], source_paths=value['sourcePaths'],
                    protected_paths=value['protectedPaths'], original_artifact_root=value['originalArtifactRoot'],
                    original_artifact_paths=value['originalArtifactPaths'], artifact_identity=value['artifactIdentity'])
                selected = value['agent']
                agent = (ProjectPatchAgent(selected['patchFile']) if selected['kind'] == 'local-patch'
                    else ClaudeProjectAgent(selected.get('executable'), model=selected['model']))
                # Validate declarations only. Startup does not read company
                # source, resolve secrets, collect logs or invoke a provider.
                if value.get('diagnosticPolicy') is not None:
                    validate_diagnostic_policy(value['diagnosticPolicy'], project=project)
                if agent.external:
                    check(project['evidencePolicy']['aiEligible'] is True,
                          'issue_configuration', 'This project does not authorize AI transfer', 400)
                    validate_transfer_policy(value.get('transferPolicy'), project_digest=contracts.digest(project),
                        provider_id=agent.provider_id, source_paths=project['editablePaths'])
            except RepairError:
                raise contracts.ContractError('Repair source or transfer policy is invalid') from None
            executor = None
            if 'protectedProfileId' in value:
                check(protected_repairs is not None, 'repair_configuration',
                      'Selected protected profile is not registered locally', 409)
                try:
                    executor = protected_repairs.executor_for(value['protectedProfileId'], runtime, source,
                        build_recipe_id=value['buildRecipeId'], owner=bundle)
                except RepairExecutionError:
                    raise contracts.ContractError('Protected profile differs from the issue runtime') from None
            repairs.append(ProjectRepairConfiguration(source, agent, value['buildRecipeId'],
                tuple(configured['validationRecipeIds']), value.get('transferPolicy'),
                executor=executor, diagnostic_policy=value.get('diagnosticPolicy')))
        if repairs:
            ProjectRepairJobs(bundle.root / 'repairs', bundle.workflow, tuple(repairs),
                disk_limit=configuration.get('repairStorageBytes', 256 * 1024 * 1024))
        return bundle.workflow.repairs
    except Exception:
        if bundle.protected_repairs is not None:
            bundle.protected_repairs.close(); bundle.protected_repairs = None
        raise
