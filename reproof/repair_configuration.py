"""Inert public references for fixed protected service composition.

Parsing validates metadata and lexical namespace separation. It opens no
referenced tool, signing definition, VM image, device or observer. Successful
validation is not a live execution or qualification capability.
"""
from __future__ import annotations

import json

from . import contracts
from .contracts.versions import exact, require
from .execution.protocol import (validate_execution_route, validate_external_validation_plan,
                                 validate_signing_policy)
from .execution.wire import canonical
from .repair_signing_configuration import _path, _read_configuration


class ProtectedServiceConfigurationError(RuntimeError):
    def __init__(self):
        self.code = 'protected_service_configuration'
        super().__init__('Protected service configuration or runtime binding is invalid')


def _require(value):
    require(value, 'Invalid protected service configuration')


def _reference(value):
    exact(value, ('path', 'sha256'))
    path = _path(value['path'])
    _require(path.suffix == '.json')
    contracts.validate_digest(value['sha256'])
    return path


def _journal(value):
    exact(value, ('root', 'environmentDigest', 'diskBudgetBytes'))
    contracts.validate_digest(value['environmentDigest'])
    contracts.bounded_int(value['diskBudgetBytes'], 'journal disk budget', 1, 512 * 1024**3)
    return _path(value['root'])


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _profile(value):
    exact(value, ('id', 'projectId', 'projectDigest', 'applicationId', 'originalBuildId', 'platform', 'deviceId',
                  'runtimePolicyDigest', 'build', 'signing', 'mobile', 'validation'))
    for name in ('id', 'projectId', 'applicationId', 'originalBuildId', 'deviceId'):
        contracts.validate_id(value[name])
    for name in ('projectDigest', 'runtimePolicyDigest'):
        contracts.validate_digest(value[name])
    _require(value['platform'] in ('android', 'ios'))
    build, signing, mobile, validation = (value[name] for name in ('build', 'signing', 'mobile', 'validation'))
    _require(set(build) in ({'bundlePath', 'route', 'journal'}, {'hostPath', 'route', 'journal'}))
    exact(signing, ('toolsPath', 'toolsManifestSha256', 'definition', 'policy', 'journal', 'ownerRoot'))
    exact(mobile, ('definition', 'route', 'journal', 'ownerRoot'))
    exact(validation, ('plan', 'observers'))
    contracts.validate_digest(signing['toolsManifestSha256'])
    build_route = validate_execution_route(build['route'])
    mobile_route = validate_execution_route(mobile['route'])
    policy = validate_signing_policy(signing['policy'])
    plan = validate_external_validation_plan(validation['plan'])
    # 명시적 비격리 선택: host-build 경로만 hostPath를 사용한다.
    bundle_key = 'hostPath' if build_route['executionClass'] == 'host-build' else 'bundlePath'
    _require(bundle_key in build)
    _require(build_route['executionClass'] in ('build-guest', 'host-build')
        and mobile_route['executionClass'] == 'mobile-device'
        and build_route['projectDigest'] == mobile_route['projectDigest'] == plan['projectDigest'] == value['projectDigest']
        and build_route['validationPlanId'] == mobile_route['validationPlanId'] == plan['id']
        and mobile_route['platform'] == policy['platform'] == value['platform']
        and mobile_route['applicationId'] == policy['applicationId'] == value['applicationId']
        and mobile_route['signingPolicyId'] == policy['id'])
    mutable = [_journal(phase['journal']) for phase in (build, signing, mobile)]
    mutable += [_path(phase['ownerRoot']) for phase in (signing, mobile)]
    _require(build['journal']['environmentDigest'] == build_route['environmentDigest']
        and mobile['journal']['environmentDigest'] == mobile_route['environmentDigest'])
    inputs = [_path(build[bundle_key]), _path(signing['toolsPath']),
        _reference(signing['definition']), _reference(mobile['definition']), _reference(validation['observers'])]
    return mutable, inputs


class ProtectedServiceConfiguration:
    __slots__ = ('_document', 'definition_digest')

    def __init__(self, value):
        try:
            exact(value, ('schemaVersion', 'kind', 'profiles'))
            _require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
                and value['kind'] == 'reproof-protected-service'
                and type(value['profiles']) is list and 1 <= len(value['profiles']) <= 128)
            mutable, inputs = [], []
            profiles, projects = set(), set()
            for row in value['profiles']:
                owned, referenced = _profile(row)
                _require(row['id'] not in profiles and row['projectId'] not in projects)
                profiles.add(row['id']); projects.add(row['projectId'])
                mutable.extend(owned); inputs.extend(referenced)
            for index, path in enumerate(mutable):
                _require(not any(_overlap(path, other) for other in mutable[index+1:]))
                _require(not any(_overlap(path, other) for other in inputs))
            encoded = canonical(value)
            _require(len(encoded) <= 64 * 1024)
            self._document = encoded.decode('utf-8')
            self.definition_digest = contracts.digest(value)
        except (contracts.ContractError, OSError, TypeError, ValueError, KeyError, RuntimeError):
            raise ProtectedServiceConfigurationError() from None

    @property
    def document(self):
        return json.loads(self._document)

    def validate_issue_configuration(self, value):
        """Match an issue configuration before creating runtimes or native owners."""
        try:
            _require(type(value) is dict and type(value.get('schemaVersion')) is int
                and value['schemaVersion'] == 1 and value.get('kind') == 'reproof-issue-runtime'
                and type(value.get('projects')) is list and 1 <= len(value['projects']) <= 128)
            selected = {row['id']: row for row in self.document['profiles']}
            used, projects = set(), set()
            for project in value['projects']:
                contracts.validate_id(project['projectId'])
                _require(project['projectId'] not in projects)
                projects.add(project['projectId'])
                repair = project.get('repair', {})
                _require(type(repair) is dict)
                if 'protectedProfileId' not in repair:
                    continue
                identifier = repair['protectedProfileId']
                _require(type(identifier) is str and identifier in selected and identifier not in used)
                row = selected[identifier]
                recipes = project['validationRecipeIds']
                _require(type(recipes) in (tuple, list) and len(recipes) == len(set(recipes)))
                _require(row['projectId'] == project['projectId'] and row['projectDigest'] == project['projectDigest']
                    and row['runtimePolicyDigest'] == contracts.digest(project['runtimePolicy'])
                    and row['build']['route']['recipeId'] == repair['buildRecipeId']
                    and set(recipes) == {item['recipeId'] for item in row['validation']['plan']['checks']})
                used.add(identifier)
            _require(used == set(selected))
        except (contracts.ContractError, TypeError, ValueError, KeyError, RuntimeError):
            raise ProtectedServiceConfigurationError() from None

    def validate_runtime(self, bundle):
        """Recheck existing service metadata before measuring or attaching owners."""
        from .live.issue_configuration import IssueRuntimeBundle
        from .live.issue_workflow import IssueWorkflow, ProjectIssueRuntime
        try:
            _require(type(bundle) is IssueRuntimeBundle and type(bundle.workflow) is IssueWorkflow
                and not bundle.workflow._closed and bundle.workflow.repairs is None
                and bundle.protected_repairs is None)
            workflow = bundle.workflow
            for row in self.document['profiles']:
                runtime = workflow.runtimes.get(row['projectId'])
                _require(type(runtime) is ProjectIssueRuntime
                    and workflow.access.registration(row['projectId']) is runtime.registration
                    and runtime.registration.project_digest == row['projectDigest']
                    and runtime.service.lab is workflow.lab
                    and runtime.service.runner.lab is workflow.lab
                    and contracts.digest(runtime.runtime_policy) == row['runtimePolicyDigest']
                    and set(runtime.validation_recipe_ids) == {
                        item['recipeId'] for item in row['validation']['plan']['checks']})
                project = runtime.registration.project
                application = next((item for item in project['applications']
                    if item['id'] == row['applicationId']), None)
                device = workflow.lab.devices.get(row['deviceId'])
                _require(application is not None and type(device) is dict
                    and application['platform'] == device.get('platform') == row['platform']
                    and row['projectId'] in workflow.access.store.assignment_project_ids(row['deviceId']))
                capabilities = device.get('capabilities')
                _require(type(capabilities) is dict and type(capabilities.get('applicationIdentity')) is dict)
                identity = capabilities['applicationIdentity']
                original = next((item for item in project['builds']
                    if item['id'] == row['originalBuildId'] and item['applicationId'] == row['applicationId']
                    and item['artifactDigest'] == identity.get('artifactDigest')), None)
                _require(original is not None)
                workflow.lab._validate_release_selection(row['deviceId'], runtime.registration,
                    row['applicationId'], original['id'])
        except (contracts.ContractError, OSError, TypeError, ValueError, KeyError, RuntimeError, AttributeError):
            raise ProtectedServiceConfigurationError() from None


def load_protected_service_configuration(path):
    """Read exactly the administrator-selected public JSON; resolve no references."""
    try:
        return ProtectedServiceConfiguration(_read_configuration(path))
    except (contracts.ContractError, OSError, TypeError, ValueError, KeyError, RuntimeError):
        raise ProtectedServiceConfigurationError() from None


__all__ = ['ProtectedServiceConfiguration', 'ProtectedServiceConfigurationError',
           'load_protected_service_configuration']
