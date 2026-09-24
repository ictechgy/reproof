"""Fixed-budget run sequence and runtime policy contracts."""
from __future__ import annotations
import copy
from .versions import bounded_int, bounded_list, exact, require, validate_id, validate_version
from .scenario import validate_attempt

def validate_attempt_budget(value):
    validate_attempt(value)
    return copy.deepcopy(value)

def validate_execution_policy(value):
    exact(value, ('schemaVersion', 'class', 'network', 'candidateCanEdit', 'enforcedControls', 'attestations'))
    validate_version(value['schemaVersion'])
    require(value['class'] in ('build-guest', 'host-build', 'desktop-guest', 'mobile-device'), 'Invalid execution class')
    require(value['network'] in ('none', 'loopback', 'declared'), 'Invalid execution network')
    require(type(value['candidateCanEdit']) is bool, 'Invalid candidate edit control')
    enforced = bounded_list(value['enforcedControls'], 'enforced controls', 64, minimum=1)
    attest = bounded_list(value['attestations'], 'attestations', 64)
    for item in enforced + attest:
        validate_id(item, 'control id')
    require(len(enforced) == len(set(enforced)) and len(attest) == len(set(attest)), 'Duplicate runtime control')
    require(not set(enforced) & set(attest), 'Attestation cannot satisfy enforcement')
    return copy.deepcopy(value)

def validate_run(value):
    exact(value, ('schemaVersion', 'attempt', 'phase', 'defect', 'expected', 'coverage', 'valid', 'regression', 'cleanup'))
    validate_version(value['schemaVersion'])
    bounded_int(value['attempt'], 'attempt', 1, 40)
    require(value['phase'] in ('original', 'candidate'), 'Invalid run phase')
    require(all((type(value[key]) is bool or value[key] is None for key in ('defect', 'expected'))), 'Invalid run predicates')
    require(value['coverage'] in ('complete', 'unknown'), 'Invalid run coverage')
    require(type(value['valid']) is bool, 'Invalid run validity')
    require(value['regression'] in ('pass', 'fail', 'not-applicable', 'unknown') and value['cleanup'] in ('complete', 'failed', 'unknown'), 'Invalid run gates')
    return copy.deepcopy(value)

def validate_run_sequence(budget, runs):
    fixed = validate_attempt_budget(budget)
    bounded_list(runs, 'runs', fixed['total'], minimum=fixed['total'])
    validated = [validate_run(x) for x in runs]
    require([x['attempt'] for x in validated] == list(range(1, fixed['total'] + 1)), 'Run attempts must be complete and ordered')
    phases = ['original'] * fixed['original'] + ['candidate'] * fixed['candidate']
    require([x['phase'] for x in validated] == phases, 'Run phases do not match frozen budget')
    require(all((x['valid'] and x['coverage'] == 'complete' and (x['cleanup'] == 'complete') for x in validated)), 'Invalid run cannot be discarded')
    require(all((x['regression'] == ('not-applicable' if x['phase'] == 'original' else 'pass') for x in validated)), 'Run validation gate failed')
    from .scenario import classify_predicates
    require(all((classify_predicates(x['defect'], x['expected'], phase=x['phase']) == 'match' for x in validated)), 'Predicate pair mismatch')
    return copy.deepcopy(validated)
