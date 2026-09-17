"""Public configuration preflight without execution or qualification authority."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys
import time
import uuid

from .live.issue_configuration import load_issue_configuration
from .repair_configuration import load_protected_service_configuration


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Invalid protected service command arguments; use --help\n')


def _recovery_command(arguments):
    from . import contracts
    from .live.client import IssueClient
    from .live.model import check
    ios = arguments.action.startswith('ios-')
    if ios:
        arguments.action = 'android-' + arguments.action[4:]
    report = {'schemaVersion': 1, 'kind': 'protected-ios-recovery' if ios else 'protected-android-recovery'}
    code = 2
    try:
        for name in ('profile', 'operation', 'id'):
            if hasattr(arguments, name): contracts.validate_id(getattr(arguments, name))
        if arguments.action == 'android-recover':
            contracts.validate_digest(arguments.request_digest)
            contracts.validate_id(arguments.request_id)
            check(10 <= arguments.timeout_seconds <= 600, 'invalid_timeout', 'Invalid recovery timeout', 400)
            check(1 <= arguments.wait_timeout <= 1800, 'invalid_timeout', 'Invalid recovery wait', 400)
        if arguments.credential_stdin:
            credential = sys.stdin.read(514).rstrip('\n')
        else:
            check(sys.stdin.isatty(), 'credential_required', 'Use --credential-stdin for noninteractive access', 400)
            credential = getpass.getpass('Personal access credential: ')
        try:
            client = IssueClient(arguments.server, credential)
        finally:
            credential = ''
        base = '/api/protected-recovery'
        if arguments.action == 'android-profiles':
            report.update(client.call(base+'/profiles'))
        elif arguments.action == 'android-operations':
            report.update(client.call(base+'/profiles/'+arguments.profile+'/operations'))
        elif arguments.action == 'android-status':
            report.update(client.call(base+'/profiles/'+arguments.profile+'/operations/'+arguments.operation))
        elif arguments.action in ('recovery-job', 'recovery-cancel'):
            path = base+'/jobs/'+arguments.id
            report.update(client.call(path+'/cancel', {}) if arguments.action == 'recovery-cancel' else client.call(path))
        elif arguments.action == 'android-recover':
            path = base+'/profiles/'+arguments.profile+'/operations/'+arguments.operation+'/recover'
            report.update(client.call(path, {'requestDigest': arguments.request_digest,
                'requestId': arguments.request_id, 'timeoutSeconds': arguments.timeout_seconds}))
            if arguments.wait:
                deadline = time.monotonic()+arguments.wait_timeout
                while report['job']['state'] not in ('succeeded', 'failed', 'cancelled'):
                    check(time.monotonic() < deadline, 'recovery_wait_timeout', 'Recovery wait elapsed', 408)
                    time.sleep(min(.2, max(0, deadline-time.monotonic())))
                    report.update(client.call(base+'/jobs/'+report['job']['id']))
        report['status'] = 'observed'
        if arguments.action == 'android-recover':
            report['status'] = report['job']['state'] if arguments.wait else 'accepted'
            code = 0 if not arguments.wait or report['job']['state'] == 'succeeded' else 2
        else:
            code = 0
    except KeyboardInterrupt:
        report['status'] = 'interrupted'; code = 130
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, contracts.ContractError):
        report.update(status='rejected', error={'code': 'protected_recovery_unavailable',
            'message': 'Check service access, the registered profile and original operation'})
    print(json.dumps(report, sort_keys=True, separators=(',', ':')))
    return code


def main(argv=None):
    parser = _ArgumentParser(prog='reproloop protected-service',
        description='Validate protected service references or operate authenticated mobile recovery')
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('check-config')
    check.add_argument('--config', type=Path, required=True)
    check.add_argument('--issue-config', type=Path, required=True)
    for name in ('android-profiles', 'android-operations', 'android-status', 'android-recover',
                 'ios-profiles', 'ios-operations', 'ios-status', 'ios-recover', 'recovery-job', 'recovery-cancel'):
        child = commands.add_parser(name)
        child.add_argument('--server', default='http://127.0.0.1:8765')
        child.add_argument('--credential-stdin', action='store_true', help='Read a personal credential without echo')
        if name in ('android-operations', 'android-status', 'android-recover', 'ios-operations', 'ios-status', 'ios-recover'):
            child.add_argument('--profile', required=True)
        if name in ('android-status', 'android-recover', 'ios-status', 'ios-recover'):
            child.add_argument('--operation', required=True)
        if name in ('recovery-job', 'recovery-cancel'):
            child.add_argument('--id', required=True)
        if name in ('android-recover', 'ios-recover'):
            child.add_argument('--request-digest', required=True)
            child.add_argument('--request-id', default='cli_'+uuid.uuid4().hex)
            child.add_argument('--timeout-seconds', type=int, default=120)
            child.add_argument('--wait', action='store_true')
            child.add_argument('--wait-timeout', type=int, default=180)
    arguments = parser.parse_args(argv)
    if arguments.action != 'check-config':
        return _recovery_command(arguments)
    report = {'schemaVersion':1, 'kind':'protected-service-configuration-check',
              'executionAuthority':'none'}
    code = 2
    try:
        configuration = load_protected_service_configuration(arguments.config)
        issue = load_issue_configuration(arguments.issue_config)
        configuration.validate_issue_configuration(issue)
        report.update(status='configuration-validated', configurationDigest=configuration.definition_digest,
                      profileIds=[row['id'] for row in configuration.document['profiles']])
        code = 0
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        report.update(status='rejected', error={'code':'protected_service_configuration',
            'message':'Check the selected public service and issue configurations'})
    except KeyboardInterrupt:
        report.update(status='interrupted'); code = 130
    print(json.dumps(report, sort_keys=True, separators=(',', ':')))
    return code


if __name__ == '__main__': raise SystemExit(main())
