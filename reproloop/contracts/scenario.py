"""Executable specifications, frozen qualification, and trusted substitution binding."""
from __future__ import annotations
import copy
from dataclasses import dataclass
from .versions import bounded_int, bounded_list, bounded_number, bounded_text, digest, exact, json_scalar, require, unique_ids, validate_digest, validate_id, validate_version
from .evidence import validate_input, validate_provenance
from .observation import validate_coverage_requirement

def _predicate(value, depth=0):
    require(depth <= 4, 'Predicate nesting is too deep')
    exact(value, ('kind',), ('observationId', 'property', 'operator', 'value', 'children'))
    if value['kind'] in ('all', 'any', 'not'):
        children = bounded_list(value.get('children'), 'predicate children', 8, minimum=1)
        require(value['kind'] != 'not' and len(children) >= 2 or (value['kind'] == 'not' and len(children) == 1), 'Invalid Boolean predicate arity')
        require(set(value) == {'kind', 'children'}, 'Invalid Boolean predicate fields')
        for child in children:
            _predicate(child, depth + 1)
    else:
        require(value['kind'] == 'property', 'Invalid predicate kind')
        exact(value, ('kind', 'observationId', 'property', 'operator'), ('value',))
        validate_id(value['observationId'], 'predicate observation id')
        validate_id(value['property'], 'predicate property')
        require(value['operator'] in ('equals', 'not-equals', 'exists', 'absent', 'lt', 'lte', 'gt', 'gte'), 'Invalid predicate operator')
        if value['operator'] in ('equals', 'not-equals', 'lt', 'lte', 'gt', 'gte'):
            json_scalar(value.get('value'), 'predicate value')
            if value['operator'] in ('lt', 'lte', 'gt', 'gte'):
                bounded_number(value['value'], 'numeric predicate', -(2 ** 53 - 1), 2 ** 53 - 1)
        else:
            require('value' not in value, 'Presence predicate cannot have a value')

def _assertion(value):
    exact(value, ('id', 'role', 'predicate', 'coverage', 'windowMs', 'stabilityMs'))
    validate_id(value['id'], 'assertion id')
    require(value['role'] in ('defect', 'expected'), 'Invalid assertion role')
    _predicate(value['predicate'])
    coverage = validate_coverage_requirement(value['coverage'], relative=True)
    bounded_int(value['windowMs'], 'assertion window', 0, 60000)
    bounded_int(value['stabilityMs'], 'assertion stability', 0, min(5000, value['windowMs']))
    require(coverage['windowMs']['end'] <= value['windowMs'], 'Observation exceeds assertion window')
    require(coverage['class'] != 'snapshot' or value['stabilityMs'] == 0, 'A snapshot cannot prove stability')
    require(value['stabilityMs'] <= coverage['windowMs']['end'] - coverage['windowMs']['start'], 'Stability exceeds observation window')

def predicate_observations(predicate):
    """Return the observation IDs referenced by a previously validated predicate."""
    if predicate['kind'] == 'property':
        return {predicate['observationId']}
    return set().union(*(predicate_observations(child) for child in predicate['children']))

def validate_specification(value):
    required = ('schemaVersion', 'id', 'revision', 'originalRecordingDigest', 'actions', 'waits', 'bindings', 'fixtures', 'assertions', 'provenance')
    exact(value, required)
    validate_version(value['schemaVersion'])
    validate_id(value['id'], 'specification id')
    bounded_int(value['revision'], 'specification revision', 1, 1000000)
    validate_digest(value['originalRecordingDigest'])
    validate_provenance(value['provenance'])
    require(value['provenance']['kind'] == 'authored', 'Specification must be explicitly authored')
    actions = bounded_list(value['actions'], 'actions', 10000, minimum=1)
    for action in actions:
        exact(action, ('eventId', 'action', 'parameters'), ('target', 'geometry'))
        validate_id(action['eventId'], 'event id')
        validate_input({k: v for k, v in action.items() if k != 'eventId'})
    require(len({x['eventId'] for x in actions}) == len(actions), 'Duplicate action event id')
    waits = bounded_list(value['waits'], 'waits', 10000)
    action_ids = {item['eventId'] for item in actions}
    for wait in waits:
        exact(wait, ('afterEventId', 'durationMs'))
        validate_id(wait['afterEventId'], 'wait event id')
        require(wait['afterEventId'] in action_ids, 'Wait references unknown event')
        bounded_int(wait['durationMs'], 'wait duration', 100, 60000)
    require(len({wait['afterEventId'] for wait in waits}) == len(waits), 'Duplicate wait binding')
    require(sum((wait['durationMs'] for wait in waits)) + sum((action['parameters'].get('durationMs', 0) for action in actions)) <= 600000, 'Scenario duration budget exceeded')
    bindings = bounded_list(value['bindings'], 'bindings', 256)
    for binding in bindings:
        exact(binding, ('name', 'variableId'))
        validate_id(binding['name'], 'binding name')
        validate_id(binding['variableId'], 'variable id')
    require(len({binding['name'] for binding in bindings}) == len(bindings), 'Duplicate binding name')
    fixtures = bounded_list(value['fixtures'], 'fixture references', 128)
    for fixture in fixtures:
        validate_id(fixture, 'fixture recipe id')
    require(len(fixtures) == len(set(fixtures)), 'Duplicate fixture reference')
    assertions = bounded_list(value['assertions'], 'assertions', 128, minimum=2)
    for assertion in assertions:
        _assertion(assertion)
    unique_ids(assertions, 'assertion id')
    require([x['role'] for x in assertions].count('defect') == 1 and [x['role'] for x in assertions].count('expected') == 1, 'Specification needs one defect and one expected predicate')
    return copy.deepcopy(value)

def validate_attempt(value):
    exact(value, ('original', 'candidate', 'total'))
    bounded_int(value['original'], 'original attempts', 3, 20)
    bounded_int(value['candidate'], 'candidate attempts', 3, 20)
    bounded_int(value['total'], 'total attempts', 6, 40)
    require(value['total'] == value['original'] + value['candidate'], 'Attempt budget must be fixed before execution')

def validate_qualification(value):
    required = ('schemaVersion', 'projectDigest', 'projectRevision', 'recordingDigest', 'specificationDigest', 'originalBuildId', 'fixtureRules', 'observationRequirements', 'runtimePolicyDigest', 'validationRecipeIds', 'attemptBudget')
    exact(value, required)
    validate_version(value['schemaVersion'])
    for field in ('projectDigest', 'recordingDigest', 'specificationDigest', 'runtimePolicyDigest'):
        validate_digest(value[field], field)
    bounded_text(value['projectRevision'], 'project revision', 120)
    validate_id(value['originalBuildId'], 'build id')
    rules = bounded_list(value['fixtureRules'], 'fixture rules', 128)
    for rule in rules:
        exact(rule, ('fixtureId', 'equivalenceDigest'))
        validate_id(rule['fixtureId'], 'fixture id')
        validate_digest(rule['equivalenceDigest'])
    require(len({rule['fixtureId'] for rule in rules}) == len(rules), 'Duplicate fixture rule')
    requirements = bounded_list(value['observationRequirements'], 'observation requirements', 128, minimum=1)
    for requirement in requirements:
        exact(requirement, ('assertionId', 'observationId', 'coverage'))
        validate_id(requirement['assertionId'], 'assertion id')
        validate_id(requirement['observationId'], 'observation id')
        validate_coverage_requirement(requirement['coverage'], relative=True)
    require(len({(item['assertionId'], item['observationId']) for item in requirements}) == len(requirements), 'Duplicate observation requirement')
    recipes = bounded_list(value['validationRecipeIds'], 'validation recipes', 128, minimum=1)
    for recipe in recipes:
        validate_id(recipe, 'validation recipe id')
    require(len(recipes) == len(set(recipes)), 'Duplicate validation recipe')
    validate_attempt(value['attemptBudget'])
    return copy.deepcopy(value)

def validate_candidate_run(value):
    exact(value, ('schemaVersion', 'qualificationDigest', 'sourceBuildId', 'candidateBuildDigest', 'originalRecordingDigest', 'specificationDigest'))
    validate_version(value['schemaVersion'])
    validate_digest(value['qualificationDigest'])
    validate_id(value['sourceBuildId'], 'candidate build id')
    validate_digest(value['candidateBuildDigest'])
    validate_digest(value['originalRecordingDigest'])
    validate_digest(value['specificationDigest'])
    return copy.deepcopy(value)

@dataclass(frozen=True, slots=True)
class TrustedSubstitutionApproval:
    qualification_digest: str
    recording_digest: str
    specification_digest: str
    candidate_build_id: str
    candidate_build_digest: str
    _issuer: object
_LOCAL_ISSUER = object()

def issue_substitution_approval(*, qualification_digest, recording_digest, specification_digest, candidate_build_id, candidate_build_digest):
    validate_digest(qualification_digest)
    validate_digest(recording_digest)
    validate_digest(specification_digest)
    validate_id(candidate_build_id, 'candidate build id')
    validate_digest(candidate_build_digest)
    return TrustedSubstitutionApproval(qualification_digest, recording_digest, specification_digest, candidate_build_id, candidate_build_digest, _LOCAL_ISSUER)

def check_candidate_substitution(candidate, approval):
    run = validate_candidate_run(candidate)
    require(type(approval) is TrustedSubstitutionApproval and approval._issuer is _LOCAL_ISSUER, 'Trusted local approval required')
    require((run['qualificationDigest'], run['originalRecordingDigest'], run['specificationDigest'], run['sourceBuildId'], run['candidateBuildDigest']) == (approval.qualification_digest, approval.recording_digest, approval.specification_digest, approval.candidate_build_id, approval.candidate_build_digest), 'Candidate substitution binding mismatch')
    return copy.deepcopy(run)

def validate_qualification_bindings(qualification, project, evidence, specification):
    from .project import validate_project_revision
    from .evidence import validate_original_evidence
    q = validate_qualification(qualification)
    p = validate_project_revision(project)
    e = validate_original_evidence(evidence)
    s = validate_specification(specification)
    require(e['projectId'] == p['id'] and e['projectRevision'] == p['revision'], 'Original project identity mismatch')
    require(q['projectDigest'] == digest(p) and q['projectRevision'] == p['revision'], 'Qualification project mismatch')
    require(q['recordingDigest'] == digest(e) and q['specificationDigest'] == digest(s), 'Qualification evidence mismatch')
    require(s['originalRecordingDigest'] == digest(e), 'Specification recording mismatch')
    builds = {x['id']: x for x in p['builds']}
    require(q['originalBuildId'] in builds and e['buildId'] == q['originalBuildId'], 'Original build mismatch')
    require(e['applicationId'] == builds[q['originalBuildId']]['applicationId'], 'Original application mismatch')
    variables = {x['id']: x for x in p['variables']}
    require(all((x['variableId'] in variables for x in s['bindings'])), 'Unknown scenario variable')
    application_ids = {item['id'] for item in p['applications']}
    all_inputs = s['actions'] + [event['input'] for event in e['events']]
    for action in all_inputs:
        if action['action'] == 'text':
            variable = variables.get(action['parameters']['variableId'])
            require(variable is not None and variable['type'] in ('string', 'secret-reference'), 'Unknown text variable')
        if action['action'] in ('launch', 'terminate'):
            require(action['parameters']['applicationId'] in application_ids, 'Unknown application operation')
    original_events = {event['id']: index for index, event in enumerate(e['events'])}
    mapped = [action['eventId'] for action in s['actions']]
    require(set(mapped) <= set(original_events), 'Unrecorded action mapping')
    require([original_events[item] for item in mapped] == sorted((original_events[item] for item in mapped)), 'Action mapping changes original order')
    fixture_ids = {x['id'] for x in p['fixtures']}
    require(set(s['fixtures']) <= fixture_ids and {x['fixtureId'] for x in q['fixtureRules']} == set(s['fixtures']), 'Qualification fixture mismatch')
    require(all((receipt['recipeId'] in fixture_ids for receipt in e['preparation'])), 'Unregistered original preparation')
    required_observations = {(assertion['id'], observation): assertion['coverage'] for assertion in s['assertions'] for observation in predicate_observations(assertion['predicate'])}
    require({observation for _, observation in required_observations} <= set(p['observations']), 'Unknown predicate observation')
    actual_requirements = {(item['assertionId'], item['observationId']): item['coverage'] for item in q['observationRequirements']}
    require(actual_requirements == required_observations, 'Qualification observation mismatch')
    recipe_ids = {x['id'] for x in p['recipes'] if x['kind'] == 'regression'}
    require(set(q['validationRecipeIds']) <= recipe_ids, 'Unknown regression recipe')
    require('mobile-device' in p['executionClasses'], 'Unsupported runtime class')
    return copy.deepcopy(q)

def classify_predicates(defect, expected, *, phase):
    require(phase in ('original', 'candidate'), 'Invalid qualification phase')
    if type(defect) is not bool or type(expected) is not bool:
        return 'unknown'
    wanted = (True, False) if phase == 'original' else (False, True)
    return 'match' if (defect, expected) == wanted else 'mismatch'
