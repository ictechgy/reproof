"""Public offline iOS tool build, signing status and measured recovery commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading
import time

from . import contracts
from .execution.journal import TERMINAL
from .ios_signing_configuration import load_ios_signing_configuration
from .ios_signing_tools import IOSSigningBuildTools, IOSSigningToolsError, build_ios_signing_owner


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, 'Invalid iOS signing command arguments; use --help\n')


def _build_tools(arguments):
    if not 1 <= arguments.timeout_seconds <= 120:
        raise IOSSigningToolsError()
    cancellation = threading.Event()
    previous = {}
    try:
        for selected in (signal.SIGINT, signal.SIGTERM):
            previous[selected] = signal.signal(selected, lambda _number, _frame: cancellation.set())
        tools = IOSSigningBuildTools(arguments.clang, arguments.clang_sha256,
            arguments.sdk_root, arguments.sdk_settings_sha256)
        result = build_ios_signing_owner(arguments.output_new, tools, cancellation=cancellation,
            deadline_monotonic=time.monotonic() + arguments.timeout_seconds)
        return {'status':'built', 'manifestDigest':result.manifest_digest,
            'outputDigest':result.output_digest, 'ownerDefinitionDigest':result.tools.definition_digest}
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)


def main(argv=None):
    parser = _ArgumentParser(prog='reproof ios-signing',
        description='Build fixed iOS signing tools, inspect or recover an existing operation')
    commands = parser.add_subparsers(dest='action', required=True)
    build = commands.add_parser('build-tools')
    build.add_argument('--output-new', type=Path, required=True)
    build.add_argument('--clang', type=Path, required=True)
    build.add_argument('--clang-sha256', required=True)
    build.add_argument('--sdk-root', type=Path, required=True)
    build.add_argument('--sdk-settings-sha256', required=True)
    build.add_argument('--timeout-seconds', type=float, default=90)
    for name in ('status','recover'):
        child = commands.add_parser(name)
        child.add_argument('--config', type=Path, required=True,
            help='Public reference exported by the original signing owner')
        child.add_argument('--operation', required=True)
        if name == 'recover': child.add_argument('--request-digest', required=True)
    args = parser.parse_args(argv)
    report = {'schemaVersion':1, 'kind':
        'ios-signing-tools-v1' if args.action == 'build-tools' else 'ios-signing-recovery-v1'}
    operations = None
    result = 2
    try:
        if args.action == 'build-tools':
            report.update(_build_tools(args))
        else:
            contracts.validate_id(args.operation)
            if args.action == 'recover': contracts.validate_digest(args.request_digest)
            operations = load_ios_signing_configuration(args.config).open_existing()
            observed = operations.status(args.operation)
        if args.action == 'status':
            report.update(status='observed', operation=observed)
        elif args.action == 'recover':
            if observed['requestDigest'] != args.request_digest:
                raise ValueError('original request required')
            if observed['runState'] in TERMINAL:
                report.update(status='already-terminal', operationId=args.operation,
                    state=observed['runState'], reservedBytes=observed['reservedBytes'])
            else:
                with operations.recovery(args.operation,args.request_digest) as capability:
                    row = operations.run_store.finish_signing_recovery(capability, authority=operations)
                    report.update(status='recovered', operationId=args.operation, state=row['state'],
                        reservedBytes=row['reservedBytes'], evidenceDigest=capability.evidence_digest)
        result = 0
    except IOSSigningToolsError as error:
        report.update(status='rejected', error={'code':error.code,
            'message':'Fixed iOS signing tools could not be built or loaded'},
            cleanupConfirmed=error.cleanup_confirmed)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        report.update(status='rejected', error={'code':'ios_signing_operation_unavailable',
            'message':'Check the original public reference, journal, operation and request'})
    except KeyboardInterrupt:
        report.update(status='interrupted'); result = 130
    finally:
        if operations is not None: operations.close()
    print(json.dumps(report,sort_keys=True,separators=(',',':')))
    return result


if __name__ == '__main__': raise SystemExit(main())
