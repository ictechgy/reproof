"""Nonmutating checks of public coordinator documents, never deployment authority."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import contracts
from ..core import ContractError
from ..project_repair import RepairError
from ..storage import read_json
from .configuration import load_shared_configuration
from .issue_configuration import load_issue_configuration
from .recording_session import validate_collection_policy


NOT_CHECKED = ["durable-authorization", "worker-reachability", "device-readiness",
               "tls-key-validity", "listener-availability", "qualification",
               "secret-resolution", "native-tools", "repair-execution"]


def _require(condition):
    if not condition:
        raise ContractError("Public configuration references do not match")


def _issue_links(configuration, projects):
    for configured in configuration['projects']:
        project = projects.get((configured['projectId'], configured['projectDigest']))
        _require(project is not None)
        for key, field, allowed in (
            ('variables', 'variableId', {item['id'] for item in project['variables']}),
            ('observations', 'observationId', set(project['observations'])),
            ('fixtures', 'fixtureId', {item['id'] for item in project['fixtures']}),
        ):
            identifiers = [item[field] for item in configured[key]]
            _require(len(identifiers) == len(set(identifiers)) and set(identifiers) <= allowed)
        recipes = {item['id'] for item in project['recipes'] if item['kind'] == 'regression'}
        selected = configured['validationRecipeIds']
        _require(selected and len(selected) == len(set(selected)) and set(selected) <= recipes)
        if 'repair' in configured:
            repair = configured['repair']
            build_recipes = {item['id'] for item in project['recipes'] if item['kind'] == 'build'}
            _require(repair['buildRecipeId'] in build_recipes)
            editable = set(project['editablePaths'])
            protected = set(repair['protectedPaths']) | {
                item['productFile'] for item in project['recipes'] + project['fixtures']}
            _require(editable <= set(repair['sourcePaths']) and protected <= set(repair['sourcePaths'])
                     and not editable & protected)
            if 'diagnosticPolicy' in repair:
                from ..repair_diagnostics import validate_diagnostic_policy
                validate_diagnostic_policy(repair['diagnosticPolicy'], project=project)
            if repair['agent']['kind'] != 'local-patch':
                from ..project_repair import validate_transfer_policy
                _require(project['evidencePolicy']['aiEligible'] is True)
                validate_transfer_policy(repair.get('transferPolicy'), project_digest=contracts.digest(project),
                                         provider_id='claude', source_paths=project['editablePaths'])
        applications = {item['id'] for item in project['applications']}
        fixtures = {item['id']: item for item in project['fixtures']}
        for fixture in configured['fixtures']:
            checks = fixture['checkRecipeIds']
            _require(fixture['applicationId'] in applications and len(checks) == len(set(checks)))
            requirements = [(fixture['fixtureId'], 'prepare'), (fixture['cleanupRecipeId'], 'cleanup')]
            requirements.extend((item, 'check') for item in checks)
            _require(all(identifier in fixtures and fixtures[identifier]['operation'] == operation
                         for identifier, operation in requirements))
            base = fixtures[fixture['fixtureId']]
            _require(all(fixtures[identifier].get('exclusive') is True
                         and fixtures[identifier].get('endpointId') == fixture['endpointId']
                         and fixtures[identifier].get('capabilities') == base.get('capabilities')
                         for identifier, _operation in requirements))


def check_configuration(shared_path, *, issue_path=None, output=None):
    # Only explicitly selected public documents are read. No access store, key,
    # environment variable value, source tree, helper, or device is inspected.
    budget = [0]
    def public_file(path):
        source = Path(path)
        _require(source.is_file() and not source.is_symlink())
        size = source.stat().st_size
        budget[0] += size
        _require(size <= 1024 * 1024 and budget[0] <= 8 * 1024 * 1024)
        return source

    shared = load_shared_configuration(public_file(shared_path))
    if output is not None:
        try:
            # Python 3.13 non-strict resolve may leave a loop unresolved.
            # Validate symlink components explicitly on every supported version.
            for namespace in (Path(output), shared.state_root):
                for component in (namespace, *namespace.parents):
                    if component.is_symlink():
                        component.resolve(strict=True)
            destination = Path(output).resolve()
            state = shared.state_root.resolve()
        except RuntimeError:
            # pathlib reports symlink loops this way on Python 3.11/3.12.
            raise ContractError('Invalid configuration namespace') from None
        _require(destination != state and state not in destination.parents and destination not in state.parents)
    projects = {}
    for project_path, policy_path in shared.project_files:
        project = contracts.validate_project_revision(read_json(public_file(project_path)))
        validate_collection_policy(read_json(public_file(policy_path)))
        key = (project['id'], contracts.digest(project))
        _require(key not in projects)
        projects[key] = project
    if issue_path is not None:
        _issue_links(load_issue_configuration(public_file(issue_path)), projects)
    return {'projectRevisionCount': len(projects), 'issueConfigurationChecked': issue_path is not None,
            'outputNamespaceChecked': output is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='reproof live-check-config',
        description='Validate public configuration without opening state, sockets, devices or credentials')
    parser.add_argument('--shared-config', type=Path, required=True)
    parser.add_argument('--issue-config', type=Path)
    parser.add_argument('--output', type=Path, help='Check proposed output namespace; never create it')
    args = parser.parse_args(argv)
    report = {'schemaVersion': 1, 'kind': 'shared-coordinator-configuration-check',
              'executionAuthority': 'none', 'notChecked': NOT_CHECKED}
    try:
        report.update(check_configuration(args.shared_config, issue_path=args.issue_config, output=args.output))
        report['status'] = 'configuration-validated'
        code = 0
    except OSError:
        report.update(status='rejected', error={'code': 'public_configuration_io',
            'message': 'A selected public configuration file could not be read'})
        code = 2
    except (ValueError, TypeError, KeyError, RecursionError, RepairError):
        report.update(status='rejected', error={'code': 'public_configuration_invalid',
            'message': 'Check public configuration schemas, references and namespace separation'})
        code = 2
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        code = 130
    print(json.dumps(report, sort_keys=True, separators=(',', ':')))
    return code
