"""Offline iOS preparation status and recovery commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
import time

from . import contracts
from .execution.journal import TERMINAL
from .ios_mobile_configuration import load_ios_mobile_configuration


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Invalid iOS mobile command arguments; use --help\n')


def _parser():
    parser = _ArgumentParser(prog='reproloop ios-mobile',
        description='Inspect or recover an existing iOS IPA preparation journal')
    commands = parser.add_subparsers(dest='action', required=True)
    status = commands.add_parser('status')
    status.add_argument('--config', type=Path, required=True,
        help='Public reference exported by the original preparation owner')
    status.add_argument('--operation', required=True)
    recover = commands.add_parser('recover')
    recover.add_argument('--config', type=Path, required=True,
        help='Public reference exported by the original preparation owner')
    recover.add_argument('--operation', required=True)
    recover.add_argument('--request-digest', required=True)
    recover.add_argument('--timeout-seconds', type=float, default=30)
    return parser


def _handlers(cancellation):
    previous = {}
    for selected in (signal.SIGINT, signal.SIGTERM):
        previous[selected] = signal.signal(selected, lambda _number, _frame: cancellation.set())
    return previous


def main(argv=None):
    args = _parser().parse_args(argv)
    report = {'schemaVersion': 1, 'kind': 'ios-mobile-preparation-recovery-v1'}
    operations = None
    cancellation = threading.Event()
    previous = {}
    result = 2
    try:
        contracts.validate_id(args.operation)
        if args.action == 'recover':
            contracts.validate_digest(args.request_digest)
            contracts.bounded_number(args.timeout_seconds, 'recovery timeout', 1, 120)
        previous = _handlers(cancellation)
        configuration = load_ios_mobile_configuration(args.config)
        operations = configuration.open_existing()
        observed = operations.status(args.operation)
        if args.action == 'status':
            report.update(status='observed', operation=observed)
        elif observed['requestDigest'] != args.request_digest:
            raise ValueError('original request required')
        elif observed['runState'] in TERMINAL:
            report.update(status='already-terminal', operationId=args.operation,
                state=observed['runState'], reservedBytes=observed['reservedBytes'])
        elif observed.get('nativeOwnership') is not None:
            report.update(status='rejected', error={'code':'ios_mobile_native_recovery_reserved',
                'message':'Native-bound iOS preparation cannot use preparation recovery'})
            result = 2
        else:
            with operations.preparation_recovery(args.operation, args.request_digest,
                    cancellation=cancellation,
                    deadline_monotonic=time.monotonic() + args.timeout_seconds) as capability:
                row = operations.run_store.finish_ios_preparation_recovery(
                    capability, authority=operations)
                report.update(status='recovered', operationId=args.operation,
                    state=row['state'], reservedBytes=row['reservedBytes'])
        if report.get('status') != 'rejected':
            result = 0
    except KeyboardInterrupt:
        report.update(status='interrupted')
        result = 130
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        if cancellation.is_set():
            report.update(status='interrupted')
            result = 130
        else:
            report.update(status='rejected', error={'code':'ios_mobile_operation_unavailable',
                'message':'Check the original public reference, journal, operation and request'})
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)
        if operations is not None:
            try:
                closed = operations.close(deadline_monotonic=time.monotonic() + 3)
                if result == 0 and not closed:
                    report = {'schemaVersion': 1, 'kind': 'ios-mobile-preparation-recovery-v1',
                        'status': 'rejected',
                        'error': {'code': 'ios_mobile_operation_unavailable',
                                  'message': 'Check the original public reference, journal, operation and request'}}
                    result = 2
            except (OSError, RuntimeError, TypeError, ValueError):
                if result == 0:
                    report = {'schemaVersion': 1, 'kind': 'ios-mobile-preparation-recovery-v1',
                        'status': 'rejected',
                        'error': {'code': 'ios_mobile_operation_unavailable',
                                  'message': 'Check the original public reference, journal, operation and request'}}
                    result = 2
    print(json.dumps(report, sort_keys=True, separators=(',', ':')))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
