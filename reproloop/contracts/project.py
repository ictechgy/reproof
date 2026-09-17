"""Trusted administrator-owned project revision data contracts."""
from __future__ import annotations
import copy, re
from .versions import bounded_int, bounded_list, bounded_text, exact, require, safe_relative_path, unique_ids, validate_digest, validate_id, validate_version

def validate_application_identity(value):
    exact(value, ('id', 'platform', 'bundle'), ('versionPattern',))
    validate_id(value['id'], 'application id')
    require(value['platform'] in ('android', 'ios'), 'Invalid application platform')
    bundle = bounded_text(value['bundle'], 'application bundle', 180)
    require(re.fullmatch('[A-Za-z][A-Za-z0-9_.-]{0,179}', bundle) is not None, 'Invalid application bundle')
    if 'versionPattern' in value:
        bounded_text(value['versionPattern'], 'application version pattern', 120)
    return copy.deepcopy(value)

def validate_build_identity(value):
    exact(value, ('id', 'applicationId', 'revision', 'artifactDigest', 'provenance'), ('sourceDigest',))
    validate_id(value['id'], 'build id')
    validate_id(value['applicationId'], 'application id')
    bounded_text(value['revision'], 'build revision', 200)
    validate_digest(value['artifactDigest'], 'artifact digest')
    require(value['provenance'] in ('trusted-build', 'operator-attestation'), 'Invalid build provenance')
    if 'sourceDigest' in value:
        validate_digest(value['sourceDigest'], 'source digest')
    return copy.deepcopy(value)

def validate_variable(value):
    exact(value, ('id', 'type', 'secret'), ('default',))
    validate_id(value['id'], 'variable id')
    kind = value['type']
    require(kind in ('string', 'integer', 'boolean', 'secret-reference'), 'Invalid variable type')
    require(type(value['secret']) is bool, 'Invalid variable secret flag')
    require(kind != 'secret-reference' or value['secret'] is True, 'Secret reference must be marked secret')
    if 'default' in value:
        require(not value['secret'] and kind != 'secret-reference', 'Secret variables cannot have defaults')
        expected = {'string': str, 'integer': int, 'boolean': bool}[kind]
        require(type(value['default']) is expected, 'Variable default type mismatch')
        if expected is str:
            bounded_text(value['default'], 'variable default', 4096, empty=True)
        elif expected is int:
            bounded_int(value['default'], 'variable default', -2 ** 31, 2 ** 31 - 1)
    return copy.deepcopy(value)

def _capabilities(value):
    exact(value, ('remoteFencing', 'terminalStatus', 'idempotencyRetentionMs'))
    require(type(value['remoteFencing']) is bool, 'Invalid remote fencing capability')
    require(type(value['terminalStatus']) is bool, 'Invalid terminal status capability')
    bounded_int(value['idempotencyRetentionMs'], 'idempotency retention', 1, 30 * 86400000)

def validate_recipe(value):
    exact(value, ('id', 'kind', 'productFile', 'operation'), ('endpointId', 'exclusive', 'capabilities'))
    validate_id(value['id'], 'recipe id')
    require(value['kind'] in ('build', 'fixture', 'regression', 'cleanup'), 'Invalid recipe kind')
    safe_relative_path(value['productFile'], 'recipe product file')
    require(value['operation'] in ('prepare', 'check', 'cleanup', 'build', 'regression'), 'Invalid recipe operation')
    operations = {'build': ('build',), 'regression': ('regression',), 'fixture': ('prepare', 'check'), 'cleanup': ('cleanup',)}
    require(value['operation'] in operations[value['kind']], 'Recipe kind and operation mismatch')
    if 'endpointId' in value:
        validate_id(value['endpointId'], 'endpoint id')
    if 'exclusive' in value:
        require(type(value['exclusive']) is bool, 'Invalid recipe exclusivity')
    if value['kind'] in ('fixture', 'cleanup'):
        require('capabilities' in value, 'Fixture capabilities are required')
    if 'capabilities' in value:
        _capabilities(value['capabilities'])
    return copy.deepcopy(value)

def validate_fixture_recipe(value):
    result = validate_recipe(value)
    require(result['kind'] in ('fixture', 'cleanup'), 'Invalid fixture recipe kind')
    return result

def validate_project_revision(value):
    required = ('schemaVersion', 'id', 'revision', 'trustGroup', 'applications', 'builds', 'variables', 'fixtures', 'observations', 'evidencePolicy', 'editablePaths', 'recipes', 'executionClasses')
    exact(value, required)
    validate_version(value['schemaVersion'])
    validate_id(value['id'], 'project id')
    bounded_text(value['revision'], 'project revision', 120)
    validate_id(value['trustGroup'], 'trust group')
    apps = [validate_application_identity(x) for x in bounded_list(value['applications'], 'applications', 100, minimum=1)]
    unique_ids(apps, 'application id')
    app_ids = {x['id'] for x in apps}
    builds = [validate_build_identity(x) for x in bounded_list(value['builds'], 'builds', 1000, minimum=1)]
    unique_ids(builds, 'build id')
    require(all((x['applicationId'] in app_ids for x in builds)), 'Build references unknown application')
    variables = [validate_variable(x) for x in bounded_list(value['variables'], 'variables', 256)]
    unique_ids(variables, 'variable id')
    fixtures = [validate_fixture_recipe(x) for x in bounded_list(value['fixtures'], 'fixtures', 128)]
    unique_ids(fixtures, 'fixture id')
    recipes = [validate_recipe(x) for x in bounded_list(value['recipes'], 'recipes', 256)]
    unique_ids(recipes, 'recipe id')
    ids = [x['id'] for x in fixtures + recipes]
    require(len(ids) == len(set(ids)), 'Duplicate recipe id')
    observations = bounded_list(value['observations'], 'approved observations', 256, minimum=1)
    for item in observations:
        validate_id(item, 'observation id')
    require(len(observations) == len(set(observations)), 'Duplicate observation id')
    paths = bounded_list(value['editablePaths'], 'editable paths', 512, minimum=1)
    for path in paths:
        safe_relative_path(path, 'editable path')
    require(len(paths) == len(set(paths)), 'Duplicate editable path')
    require(not set(paths) & {recipe['productFile'] for recipe in recipes + fixtures}, 'Recipe inputs cannot be candidate-editable')
    classes = bounded_list(value['executionClasses'], 'execution classes', 4, minimum=1)
    for execution_class in classes:
        require(execution_class in ('build-guest', 'host-build', 'desktop-guest', 'mobile-device'), 'Invalid execution class')
    require(set(classes) <= {'build-guest', 'host-build', 'desktop-guest', 'mobile-device'} and len(classes) == len(set(classes)), 'Invalid execution class')
    policy = value['evidencePolicy']
    exact(policy, ('pixels', 'text', 'accessibility', 'logs', 'fixtures', 'unknownSensitive', 'aiEligible'))
    for key in ('pixels', 'text', 'accessibility', 'logs', 'fixtures', 'aiEligible'):
        require(type(policy[key]) is bool, f'Invalid evidence policy {key}')
    require(policy['unknownSensitive'] in ('deny', 'allow-redacted'), 'Invalid unknown-sensitive policy')
    return copy.deepcopy(value)
