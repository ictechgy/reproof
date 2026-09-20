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
    close = commands.add_parser('close-run',
        help='Close a non-terminal run after device cleanup was resolved')
    close.add_argument('--config', type=Path,
        help='Public reference exported by the original preparation owner')
    close.add_argument('--owner', type=Path,
        help='Owner root when no exported reference exists')
    close.add_argument('--runs', type=Path,
        help='RunStore root when no exported reference exists')
    close.add_argument('--udid', help='Device UDID when no exported reference exists')
    close.add_argument('--disk-budget-bytes', type=int, default=512 * 1024 ** 3)
    close.add_argument('--operation', required=True)
    close.add_argument('--request-digest', required=True)
    close.add_argument('--device-clean', action='store_true',
        help='Attest that device-side cleanup was verified for a native-bound run')
    close.add_argument('--timeout-seconds', type=float, default=30)
    return parser


def _open_bootstrap(owner_path, runs_path, udid, disk_budget):
    """Reopen an owner without an exported reference, using its own intent."""
    from .ios_mobile_operation import IOSMobileDefinition, IOSMobileOperationStore
    from .execution.journal import RunStore
    owner_path, runs_path = Path(owner_path).resolve(), Path(runs_path).resolve()
    value = json.loads((owner_path / 'intent.json').read_bytes())
    fields = dict(value['definition'])
    fields.pop('scopeDigest', None); fields.pop('executionAuthority', None)
    fields['helper_bundles'] = tuple(tuple(item) for item in fields.get('helper_bundles', ()))
    definition = IOSMobileDefinition(udid=udid, **fields)
    store = RunStore(runs_path, environment_digest=value['environmentDigest'],
                     disk_limit=disk_budget, create=False)
    return IOSMobileOperationStore(store, definition, owner_path, create=False)


def _handlers(cancellation):
    previous = {}
    for selected in (signal.SIGINT, signal.SIGTERM):
        previous[selected] = signal.signal(selected, lambda _number, _frame: cancellation.set())
    return previous


def main(argv=None):
    args = _parser().parse_args(argv)
    report = {'schemaVersion': 1,
        'kind': 'ios-mobile-run-close-v1' if args.action == 'close-run'
        else 'ios-mobile-preparation-recovery-v1'}
    operations = None
    cancellation = threading.Event()
    previous = {}
    result = 2
    try:
        contracts.validate_id(args.operation)
        if args.action in ('recover', 'close-run'):
            contracts.validate_digest(args.request_digest)
            contracts.bounded_number(args.timeout_seconds, 'recovery timeout', 1, 120)
        if args.action == 'close-run':
            bootstrap = args.owner is not None or args.runs is not None or args.udid is not None
            if args.config is not None and bootstrap:
                raise ValueError('Choose either --config or --owner/--runs/--udid')
            if bootstrap and not (args.owner and args.runs and args.udid):
                raise ValueError('Bootstrap needs --owner, --runs and --udid together')
            if args.config is None and not bootstrap:
                raise ValueError('Either --config or --owner/--runs/--udid is required')
        previous = _handlers(cancellation)
        if args.action == 'close-run' and args.config is None:
            operations = _open_bootstrap(args.owner, args.runs, args.udid,
                args.disk_budget_bytes)
        else:
            configuration = load_ios_mobile_configuration(args.config)
            operations = configuration.open_existing()
        if args.action == 'close-run':
            from .ios_mobile_close import close_run
            row = operations.run_store.status(args.operation)
            if row['state'] in TERMINAL:
                report.update(status='already-terminal', operationId=args.operation,
                    state=row['state'], reservedBytes=row['reservedBytes'])
            elif row['requestDigest'] != args.request_digest:
                raise ValueError('original request required')
            else:
                row = close_run(operations, args.operation, args.request_digest,
                    device_clean_attested=args.device_clean, cancellation=cancellation,
                    deadline_monotonic=time.monotonic() + args.timeout_seconds)
                report.update(status='closed', operationId=args.operation,
                    state=row['state'], reservedBytes=row['reservedBytes'])
        else:
            observed = operations.status(args.operation)
        if args.action == 'status':
            report.update(status='observed', operation=observed)
        elif args.action == 'close-run':
            pass
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
