"""General project repair with immutable source and supervisor-owned verdicts."""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
from pathlib import Path
import threading
import time

from . import contracts
from .execution.artifacts import ArtifactError, BlobSet
from .execution.wire import canonical, safe_transfer_path


class RepairError(RuntimeError):
    def __init__(self, code, message='Protected project repair was rejected'):
        super().__init__(message)
        self.code = code


def _require(condition, code='repair_invalid'):
    if not condition:
        raise RepairError(code)


def artifact_digest(blobs, identity):
    """Measure inert artifact bytes; this does not validate an app's format."""
    _require(type(blobs) is BlobSet and identity in {'file-sha256', 'tree-sha256'},
             'artifact_identity')
    if identity == 'file-sha256':
        _require(len(blobs.entries) == 1, 'artifact_identity')
        return hashlib.sha256(blobs.entries[0][1]).hexdigest()
    return contracts.digest({path: hashlib.sha256(data).hexdigest()
                             for path, data in blobs.entries})


def _paths(value, *, allow_empty=False):
    _require(type(value) in (tuple, list) and (allow_empty or value)
             and len(value) <= 1024, 'source_policy')
    try:
        for path in value:
            safe_transfer_path(path)
        # The transfer manifest also rejects case/normalization and prefix collisions.
        if value:
            BlobSet(tuple((path, b'') for path in value))
    except (ArtifactError, contracts.ContractError, TypeError, ValueError):
        raise RepairError('source_policy') from None
    return tuple(sorted(value))


class RepairSource:
    """An operator-selected public source manifest and its original build.

    Only declared paths are read or copied. All build, fixture and regression
    recipe files must be included and protected. Other tests/harness inputs are
    declared explicitly in protected_paths. No candidate code runs on the host.
    """
    def __init__(self, project, source_root, *, source_paths, protected_paths,
                 original_artifact_root, original_artifact_paths,
                 artifact_identity='file-sha256'):
        try:
            project = contracts.validate_project_revision(project)
            self._project_json = canonical(project).decode('utf-8')
        except (contracts.ContractError, TypeError, ValueError, UnicodeError):
            raise RepairError('source_policy') from None
        self.project_digest = contracts.digest(project)
        self.source_root = Path(source_root).absolute()
        self.source_paths = _paths(source_paths)
        self.editable_paths = tuple(sorted(project['editablePaths']))
        recipes = {item['productFile'] for item in project['recipes'] + project['fixtures']}
        self.protected_paths = tuple(sorted(set(_paths(protected_paths, allow_empty=True)) | recipes))
        _require(set(self.editable_paths) <= set(self.source_paths)
                 and set(self.protected_paths) <= set(self.source_paths)
                 and not set(self.editable_paths) & set(self.protected_paths), 'source_policy')
        self.original_artifact_root = Path(original_artifact_root).absolute()
        self.original_artifact_paths = _paths(original_artifact_paths)
        _require(artifact_identity in {'file-sha256', 'tree-sha256'}, 'artifact_identity')
        self.artifact_identity = artifact_identity
        self._frozen = {}
        self._lock = threading.RLock()

    @property
    def project(self):
        return json.loads(self._project_json)

    @property
    def policy_digest(self):
        return contracts.digest({'projectDigest': self.project_digest,
            'sourcePaths': self.source_paths, 'protectedPaths': self.protected_paths,
            'editablePaths': self.editable_paths,
            'artifactPaths': self.original_artifact_paths,
            'artifactIdentity': self.artifact_identity})

    def freeze(self, build_id):
        build = next((item for item in self.project['builds'] if item['id'] == build_id), None)
        _require(build is not None and 'sourceDigest' in build, 'source_provenance')
        try:
            sources = BlobSet.from_directory(self.source_root, self.source_paths)
            artifacts = BlobSet.from_directory(self.original_artifact_root, self.original_artifact_paths)
        except (ArtifactError, OSError):
            raise RepairError('original_changed') from None
        _require(sources.digest == build['sourceDigest']
                 and artifact_digest(artifacts, self.artifact_identity) == build['artifactDigest'],
                 'source_provenance')
        with self._lock:
            previous = self._frozen.get(sources.digest)
            binding = (build_id, artifacts.digest, self.policy_digest)
            _require(previous is None or previous == binding, 'original_changed')
            self._frozen[sources.digest] = binding
        return sources

    def require_original(self, frozen, build_id):
        _require(type(frozen) is BlobSet, 'original_changed')
        with self._lock:
            previous = self._frozen.get(frozen.digest)
        _require(previous is not None and previous[0] == build_id
                 and previous[2] == self.policy_digest, 'original_changed')
        try:
            current = self.freeze(build_id)
        except RepairError:
            raise RepairError('original_changed') from None
        _require(current == frozen, 'original_changed')
        return frozen

    def apply(self, frozen, edits):
        _require(type(frozen) is BlobSet, 'original_changed')
        with self._lock:
            binding = self._frozen.get(frozen.digest)
        _require(binding is not None, 'original_changed')
        self.require_original(frozen, binding[0])
        _require(type(edits) is list and 1 <= len(edits) <= 64, 'patch_invalid')
        try:
            _require(len(canonical(edits)) <= 256 * 1024, 'patch_limit')
            entries = dict(frozen.entries)
            replacements = {}
            for edit in edits:
                _require(type(edit) is dict and set(edit) == {'path', 'old', 'new'}, 'patch_invalid')
                path, old, new = edit['path'], edit['old'], edit['new']
                _require(type(path) is str and path in self.editable_paths, 'protected_path')
                _require(type(old) is str and type(new) is str and old and old != new
                         and '\0' not in old + new
                         and len(old.encode('utf-8')) <= 128 * 1024
                         and len(new.encode('utf-8')) <= 128 * 1024, 'patch_invalid')
                text = entries[path].decode('utf-8')
                _require('\0' not in text and len(entries[path]) <= 1024 * 1024, 'patch_limit')
                _require(text.count(old) == 1, 'patch_anchor')
                start = text.index(old); end = start + len(old)
                ranges = replacements.setdefault(path, [])
                _require(all(end <= first or start >= last for first, last, _ in ranges), 'patch_overlap')
                ranges.append((start, end, new))
            for path, ranges in replacements.items():
                text = entries[path].decode('utf-8')
                for start, end, replacement in sorted(ranges, reverse=True):
                    text = text[:start] + replacement + text[end:]
                entries[path] = text.encode('utf-8')
            candidate = BlobSet(tuple(entries.items()))
            _require(candidate.digest != frozen.digest, 'patch_noop')
            _require(all(entries[path] == raw for path, raw in frozen.entries
                         if path not in self.editable_paths), 'protected_path')
            return candidate
        except (contracts.ContractError, ArtifactError, ValueError, UnicodeError, KeyError, TypeError):
            raise RepairError('patch_invalid') from None


def validate_transfer_policy(policy, *, project_digest, provider_id, source_paths):
    """Validate recipient selection without reading source or diagnostic bytes."""
    try:
        version = policy.get('schemaVersion') if type(policy) is dict else None
        keys = {'schemaVersion', 'projectDigest', 'providerId', 'approvedTransfer',
                'sourcePaths', 'specificationFields'}
        if type(version) is int and version == 2: keys.add('diagnostics')
        _require(type(policy) is dict and set(policy) == keys and type(version) is int and version in (1, 2)
            and policy['projectDigest'] == project_digest and policy['providerId'] == provider_id
            and policy['approvedTransfer'] is True, 'ai_transfer_denied')
        paths = _paths(policy['sourcePaths'])
        _require(set(paths) <= set(source_paths), 'ai_transfer_denied')
        fields = policy['specificationFields']
        _require(type(fields) is list and len(fields) <= 3
            and all(type(item) is str and item in {'actions', 'assertions', 'waits'} for item in fields)
            and len(set(fields)) == len(fields), 'ai_transfer_denied')
        if version == 2 and policy['diagnostics'] is not None:
            from .repair_diagnostics import diagnostic_fields
            selection = policy['diagnostics']
            _require(type(selection) is dict and set(selection) == {'policyDigest', 'eventFields'}, 'ai_transfer_denied')
            contracts.validate_digest(selection['policyDigest'])
            diagnostic_fields(selection['eventFields'], code='ai_transfer_denied')
        return copy.deepcopy(policy)
    except (contracts.ContractError, KeyError, TypeError, ValueError):
        raise RepairError('ai_transfer_denied') from None


def proposal_packet(project, frozen, specification, *, provider_id, external, policy=None, feedback=None,
                    diagnostics=None):
    """Prepare only explicitly eligible product text and authored QA fields.

    Resolved variables, raw recordings, fixture payloads, images, author names,
    and protected harnesses are never part of this proposal interface.
    """
    _require(type(frozen) is BlobSet and type(external) is bool, 'proposal_policy')
    try:
        project = contracts.validate_project_revision(project)
        contracts.validate_id(provider_id)
        fields = ('actions', 'assertions', 'waits')
        paths = tuple(project['editablePaths'])
        diagnostic_fields = None
        if external:
            _require(project['evidencePolicy']['aiEligible'] is True, 'ai_transfer_denied')
            policy = validate_transfer_policy(policy, project_digest=contracts.digest(project),
                                               provider_id=provider_id, source_paths=project['editablePaths'])
            version = policy['schemaVersion']
            paths = _paths(policy['sourcePaths'])
            fields = policy['specificationFields']
            if diagnostics is not None:
                selection = policy.get('diagnostics')
                _require(version == 2 and type(selection) is dict
                    and type(diagnostics) is dict and selection['policyDigest'] == diagnostics.get('policyDigest'),
                    'ai_transfer_denied')
                diagnostic_fields = selection['eventFields']
            elif version == 2:
                _require(policy['diagnostics'] is None, 'ai_transfer_denied')
        source = dict(frozen.entries)
        _require(type(specification) is dict and all(key in specification for key in fields), 'proposal_invalid')
        _require(feedback is None or type(feedback) is str and feedback in {
            'patch_invalid', 'build_failed', 'regression_failed', 'candidate_mismatch'}, 'proposal_invalid')
        packet = {'schemaVersion': 1, 'sourceFiles': {path: source[path].decode('utf-8') for path in paths},
            'specification': {key: copy.deepcopy(specification[key]) for key in fields},
            'editPolicy': {'paths': list(paths), 'exactUniqueReplacements': True,
                           'protectedInputs': 'immutable'}, 'previousFailure': feedback}
        if diagnostics is not None:
            from .repair_diagnostics import project_diagnostics
            _require(project['evidencePolicy']['logs'] is True, 'diagnostics_policy')
            packet['diagnostics'] = project_diagnostics(diagnostics, project, frozen.digest, specification,
                                                       fields=diagnostic_fields)
        _require(len(canonical(packet)) <= 512 * 1024, 'proposal_limit')
        return packet
    except (contracts.ContractError, KeyError, ValueError, UnicodeError, TypeError):
        raise RepairError('proposal_invalid') from None


class _JobCancellation:
    def __init__(self, journal, identifier, authorize):
        self.journal, self.identifier, self.authorize = journal, identifier, authorize
        self.local = journal.cancellation(identifier)

    def is_set(self):
        if self.local.is_set():
            return True
        try:
            allowed = self.authorize() is True
        except Exception:
            allowed = False
        if not allowed:
            self.journal.cancel(self.identifier)
        return not allowed

    def wait(self, timeout=None):
        import time
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            self.local.wait(.05 if deadline is None else max(0, min(.05, deadline-time.monotonic())))
        return self.is_set()


class ProjectRepair:
    """Frozen proposals and an optional locally qualified protected executor.

    Missing protected build, fixed signing, independent validation or exclusive
    mobile qualification blocks verify before the proposal adapter is called.
    Only the composed supervisor can publish verified; wire reports cannot.
    """
    reservation_bytes = 66 * 1024 * 1024

    def __init__(self, source, registry, engine, journal, agent, *, build_recipe_id,
                 validation_recipe_ids, transfer_policy=None, executor=None, diagnostic_policy=None):
        from .qualification import QualificationEngine, ScenarioRegistry
        from .repair_journal import RepairJournal
        _require(type(source) is RepairSource and type(registry) is ScenarioRegistry
            and type(engine) is QualificationEngine and engine.registry is registry
            and type(journal) is RepairJournal and callable(getattr(agent, 'propose_project', None)),
            'repair_configuration')
        contracts.validate_id(agent.provider_id)
        _require(type(agent.external) is bool, 'repair_configuration')
        recipes = {item['id']: item for item in source.project['recipes']}
        _require(build_recipe_id in recipes and recipes[build_recipe_id]['kind'] == 'build'
            and type(validation_recipe_ids) is tuple and validation_recipe_ids
            and len(set(validation_recipe_ids)) == len(validation_recipe_ids)
            and all(item in recipes and recipes[item]['kind'] == 'regression'
                    for item in validation_recipe_ids), 'repair_configuration')
        self.source, self.registry, self.engine, self.journal, self.agent = source, registry, engine, journal, agent
        self.build_recipe_id = build_recipe_id
        self.validation_recipe_ids = tuple(sorted(validation_recipe_ids))
        from .repair_verification import ProtectedRepairExecutor
        _require(executor is None or type(executor) is ProtectedRepairExecutor, 'repair_configuration')
        self.executor = executor
        if diagnostic_policy is not None:
            from .repair_diagnostics import validate_diagnostic_policy
            diagnostic_policy = validate_diagnostic_policy(diagnostic_policy, project=source.project)
        self._diagnostic_json = canonical(diagnostic_policy).decode('utf-8')
        self._transfer_json = canonical(transfer_policy).decode('utf-8')
        self._provider_json = canonical({'id': agent.provider_id, 'external': agent.external,
            'model': getattr(agent, 'model', None)}).decode('utf-8')
        self._execution_lock = threading.Lock()

    @property
    def definition_digest(self):
        return contracts.digest({'sourcePolicyDigest': self.source.policy_digest,
            'buildRecipeId': self.build_recipe_id, 'validationRecipeIds': self.validation_recipe_ids,
            'provider': json.loads(self._provider_json), 'transferPolicy': json.loads(self._transfer_json),
            'diagnosticPolicy': self.diagnostic_policy,
            'executor': self.executor.definition_digest if self.executor is not None else None})

    @property
    def diagnostic_policy(self):
        return json.loads(self._diagnostic_json)

    def availability(self):
        from .repair_execution import RepairExecutionError
        available, reason = False, 'protected_verification_unavailable'
        if self.executor is not None:
            try:
                self.executor.ready(project_digest=self.source.project_digest, build_recipe_id=self.build_recipe_id,
                                    validation_recipe_ids=self.validation_recipe_ids, _observe=True)
                available, reason = True, None
            except RepairExecutionError as error:
                reason = error.code
        return {'proposalAvailable': True, 'providerId': self.agent.provider_id,
            'providerKind': 'external-ai' if self.agent.external else 'local-test-adapter',
            'verificationAvailable': available, 'reason': reason}

    def create(self, approved, *, campaign_id, issue_id, owner_id, request_id, mode='propose', retain_until_ms=None):
        approved = self.registry.require(approved)
        contracts.validate_id(campaign_id)
        _require(type(mode) is str and mode in {'propose', 'verify'} and approved.project_digest == self.source.project_digest,
                 'repair_request')
        _require(tuple(sorted(approved.qualification['validationRecipeIds'])) == self.validation_recipe_ids,
                 'repair_policy_changed')
        request = {'schemaVersion': 1, 'issueId': issue_id, 'mode': mode, 'campaignId': campaign_id,
            'qualificationDigest': approved.qualification_digest, 'definitionDigest': self.definition_digest}
        return self.journal.create(project_id=self.source.project['id'], owner_id=owner_id,
            issue_id=issue_id, request_id=request_id, request_digest=contracts.digest(request),
            reservation_bytes=self.reservation_bytes, request=request, retain_until_ms=retain_until_ms)

    def execute(self, identifier, approved, *, authorize, diagnostic_reader=None):
        from .agents import AgentUnavailable
        from .qualification import QualificationError
        from .repair_journal import RepairJournalError, TERMINAL
        from .repair_execution import RepairExecutionError
        _require(callable(authorize), 'repair_configuration')
        with self._execution_lock:
            if not self.journal.claim(identifier):
                return self.journal.get(identifier)
            cancel = _JobCancellation(self.journal, identifier, authorize)
            attempts = []
            result_base = {'verified': False, 'afterEvidence': None}
            try:
                def boundary():
                    _require(not cancel.is_set(), 'cancelled')
                    self.registry.require(approved)
                    _require(request['definitionDigest'] == self.definition_digest, 'repair_policy_changed')
                    _require(json.loads(self._provider_json) == {'id': self.agent.provider_id,
                        'external': self.agent.external, 'model': getattr(self.agent, 'model', None)},
                        'repair_policy_changed')

                document = self.journal.get(identifier)
                request = document['request']
                _require(type(request) is dict and contracts.digest(request) == document['requestDigest']
                    and request['qualificationDigest'] == approved.qualification_digest
                    and document['projectId'] == self.source.project['id'], 'repair_request')
                boundary()
                try:
                    baseline = self.engine.require_reproduced(approved, request['campaignId'])
                except QualificationError:
                    raise RepairError('baseline_unqualified') from None
                build_id = approved.qualification['originalBuildId']
                frozen = self.source.freeze(build_id)
                original_build = next(item for item in self.source.project['builds'] if item['id'] == build_id)
                plan = {'schemaVersion': 1, 'projectDigest': approved.project_digest,
                    'recordingDigest': approved.recording_digest, 'specificationDigest': approved.specification_digest,
                    'qualificationDigest': approved.qualification_digest, 'runtimePolicyDigest': approved.runtime_policy_digest,
                    'baselineCampaignId': request['campaignId'], 'baselineDigest': contracts.digest(baseline),
                    'originalBuildId': build_id, 'originalSourceDigest': frozen.digest,
                    'originalBuildDigest': contracts.digest(original_build),
                    'originalArtifactDigest': original_build['artifactDigest'],
                    'attemptBudget': copy.deepcopy(approved.qualification['attemptBudget']),
                    'maxProposals': 1, 'definitionDigest': self.definition_digest,
                    'buildRecipeId': self.build_recipe_id, 'validationRecipeIds': list(self.validation_recipe_ids),
                    'verificationAvailable': self.availability()['verificationAvailable'],
                    'executorDigest': self.executor.definition_digest if self.executor is not None else None}
                plan['digest'] = contracts.digest(plan)
                provider = {'id': self.agent.provider_id,
                    'kind': 'external-ai' if self.agent.external else 'local-test-adapter'}
                self.journal.update(identifier, plan=plan, provider=provider)
                boundary()
                if request['mode'] == 'verify':
                    _require(self.executor is not None, 'protected_verification_unavailable')
                    self.executor.ready(project_digest=approved.project_digest, build_recipe_id=self.build_recipe_id,
                        validation_recipe_ids=self.validation_recipe_ids, runtime_policy_digest=approved.runtime_policy_digest,
                        application_id=approved.original['applicationId'])
                diagnostics = None
                if self.diagnostic_policy is not None:
                    from .repair_diagnostics import derive_app_log_diagnostics
                    _require(callable(diagnostic_reader), 'diagnostics_unavailable')
                    diagnostics = derive_app_log_diagnostics(self.source.project, approved.original,
                        frozen.digest, self.diagnostic_policy, read_object=diagnostic_reader)
                packet = proposal_packet(self.source.project, frozen, approved.specification,
                    provider_id=self.agent.provider_id, external=self.agent.external,
                    policy=json.loads(self._transfer_json), diagnostics=diagnostics)
                plan.pop('digest')
                plan['proposalPacketDigest'] = contracts.digest(packet)
                if diagnostics is not None:
                    plan.update(diagnosticEvidenceDigest=contracts.digest(packet['diagnostics']),
                        diagnosticPolicyDigest=diagnostics['policyDigest'],
                        diagnosticSourceDigests=diagnostics['sourceObjects'])
                plan['digest'] = contracts.digest(plan)
                self.journal.update(identifier, plan=plan)
                boundary()
                if diagnostics is not None:
                    self.journal.write_blobs(identifier, 'diagnostics',
                        BlobSet((('diagnostics.json', canonical(packet['diagnostics'])),)))
                self.source.require_original(frozen, build_id)
                attempts = [{'number': 1, 'status': 'requested'}]
                self.journal.update(identifier, phase='proposing', attempts=attempts)
                boundary()
                edits = self.agent.propose_project(packet, cancellation=cancel)
                boundary()
                self.source.require_original(frozen, build_id)
                candidate = self.source.apply(frozen, edits)
                original_entries = dict(frozen.entries)
                changed = sorted(path for path, raw in candidate.entries if original_entries[path] != raw)
                patch = ''.join(''.join(difflib.unified_diff(original_entries[path].decode('utf-8').splitlines(True),
                    dict(candidate.entries)[path].decode('utf-8').splitlines(True),
                    fromfile='a/' + path, tofile='b/' + path)) for path in changed)
                proposal = {'schemaVersion': 1, 'repairPlanDigest': plan['digest'], 'verified': False,
                    'candidateSourceDigest': candidate.digest, 'edits': edits, 'patch': patch}
                encoded = canonical(proposal)
                _require(len(encoded) <= 2 * 1024 * 1024, 'proposal_limit')
                boundary()
                self.journal.update(identifier, phase='candidate-ready')
                self.journal.write_blobs(identifier, 'candidate', candidate)
                self.journal.write_blobs(identifier, 'proposal', BlobSet((('proposal.json', encoded),)))
                boundary()
                self.source.require_original(frozen, build_id)
                self.engine.require_reproduced(approved, request['campaignId'])
                boundary()
                result_base = {
                    'verified': False, 'changedPaths': changed, 'candidateSourceDigest': candidate.digest,
                    'proposalDigest': hashlib.sha256(encoded).hexdigest(), 'repairPlanDigest': plan['digest'],
                    'beforeEvidence': {'campaignId': request['campaignId'], 'digest': plan['baselineDigest']},
                    'afterEvidence': None}
                attempts[0].update(status='proposal-ready', candidateSourceDigest=candidate.digest)
                if request['mode'] == 'propose':
                    return self.journal.finish(identifier, 'proposal-ready', attempts=attempts, result=result_base)
                attempts[0]['status'] = 'verifying'
                def progress(phase, data):
                    attempts[0].setdefault('execution', {}).update(copy.deepcopy(data))
                    self.source.require_original(frozen, build_id)
                    boundary()
                    self.journal.update(identifier, phase=phase, attempts=attempts)
                proof = self.executor.execute(candidate, approved, operation_id=identifier,
                    repair_plan_digest=plan['digest'], cancellation=cancel, boundary=boundary, progress=progress)
                self.source.require_original(frozen, build_id)
                self.engine.require_reproduced(approved, request['campaignId'])
                boundary()
                verified = self.executor.require_verified(proof, candidate.digest, plan['digest'], approved)
                _require(len(canonical(verified)) <= 96 * 1024, 'verification_evidence_limit')
                boundary()
                attempts[0]['status'] = 'verified'
                return self.journal.finish(identifier, 'verified', attempts=attempts,
                    result={**result_base, **verified, 'verified': True})
            except (RepairError, RepairExecutionError, RepairJournalError, QualificationError, AgentUnavailable,
                    contracts.ContractError, ArtifactError, OSError, ValueError, TypeError, KeyError) as error:
                current = self.journal.get(identifier)
                if current['status'] in TERMINAL:
                    return current
                reason = ('agent_unavailable' if isinstance(error, AgentUnavailable)
                          else getattr(error, 'code', 'repair_failed'))
                cancelled = cancel.is_set()
                if type(reason) is str and reason.endswith('_quarantined'):
                    status = 'quarantined'
                elif reason == 'cancelled' or cancelled:
                    self.journal.cancel(identifier)
                    status, reason = 'cancelled', 'cancelled'
                else:
                    status = 'blocked' if reason in {'baseline_unqualified', 'ai_transfer_denied',
                        'protected_verification_unavailable', 'source_provenance', 'build_unqualified',
                        'mobile_unqualified', 'signing_unqualified', 'validation_source_unavailable',
                        'build_unavailable', 'signing_unavailable', 'mobile_unavailable',
                        'diagnostics_unavailable'} else 'failed'
                for attempt in attempts:
                    if attempt['status'] in {'requested', 'verifying'}: attempt.update(status='failed', reason=reason)
                return self.journal.finish(identifier, status, reason=reason, attempts=attempts,
                                           result={**result_base, 'verified': False, 'afterEvidence': None})
            finally:
                if self.executor is not None: self.executor.discard(identifier)

    def proposal(self, identifier):
        from .execution.artifacts import read_regular
        from .execution.wire import decode_json
        document = self.journal.get(identifier)
        def current():
            return document.get('retainUntilMs') is None or document['retainUntilMs'] > int(time.time() * 1000)
        _require(document['status'] in {'proposal-ready', 'verified'} and not document.get('outputsExpired', False) and current()
                 and document['projectId'] == self.source.project['id'],
                 'proposal_unavailable')
        raw = read_regular(self.journal.root / identifier / 'proposal', 'proposal.json', maximum=2 * 1024 * 1024)
        _require(hashlib.sha256(raw).hexdigest() == document['result']['proposalDigest'], 'proposal_changed')
        _require(current(), 'proposal_unavailable')
        return decode_json(raw)

    def diagnostics(self, identifier):
        from .execution.artifacts import read_regular
        from .repair_diagnostics import MAX_DIAGNOSTIC_BYTES, diagnostic_json
        document = self.journal.get(identifier)
        _require(document['projectId'] == self.source.project['id'] and not document['outputsExpired']
            and (document['retainUntilMs'] is None or document['retainUntilMs'] > int(time.time() * 1000))
            and document['outputs'].get('diagnostics', {}).get('status') == 'complete'
            and document['plan'] is not None, 'diagnostics_unavailable')
        raw = read_regular(self.journal.root / identifier / 'diagnostics', 'diagnostics.json',
                           maximum=MAX_DIAGNOSTIC_BYTES)
        _require(hashlib.sha256(raw).hexdigest() == document['plan'].get('diagnosticEvidenceDigest'),
                 'diagnostics_binding')
        _require(document['retainUntilMs'] is None or document['retainUntilMs'] > int(time.time() * 1000),
                 'diagnostics_unavailable')
        return diagnostic_json(raw)


__all__ = ['RepairError', 'RepairSource', 'ProjectRepair', 'artifact_digest', 'proposal_packet']
