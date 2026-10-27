"""Local explicit source snapshots. Never executes or transmits selected inputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .core import ContractError
from .execution.artifacts import ArtifactError, open_regular
from .execution.wire import ProtocolError

SELECTION_LIMIT = 24 * 1024 * 1024


def load_selection(path):
    try:
        path = Path(path).absolute()
        descriptor = open_regular(path.parent, path.name)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_nlink != 1 or before.st_size > SELECTION_LIMIT:
                raise ValueError()
            raw = stream.read(SELECTION_LIMIT + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or len(raw) > SELECTION_LIMIT
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)):
                raise ValueError()
        from .source_snapshot_json import bounded_json
        value = bounded_json(raw, max_items=32768, max_depth=2)
        if (type(value) is not dict or set(value) != {'schemaVersion', 'kind', 'paths'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or value['kind'] != 'reproof-source-selection'
                or type(value['paths']) is not list or not 1 <= len(value['paths']) <= 32768):
            raise ValueError()
        return value['paths']
    except (ArtifactError, ProtocolError, OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ContractError('Explicit source selection is unavailable or invalid') from None


def main(argv=None):
    from .source_snapshot import SnapshotBudget, SnapshotError, capture, verify, materialize

    parser = argparse.ArgumentParser(prog='reproof source-snapshot',
        description='Capture, verify or materialize explicit local source bytes; no build, upload or provenance claim')
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ('capture', 'verify', 'materialize'):
        command = commands.add_parser(operation)
        if operation == 'capture':
            command.add_argument('--source', required=True, type=Path)
            command.add_argument('--selection', required=True, type=Path,
                                 help='Explicit reproof-source-selection JSON; no repository discovery')
        else:
            command.add_argument('--snapshot', required=True, type=Path)
        if operation != 'verify':
            command.add_argument('--output', required=True, type=Path, help='New destination only')
        command.add_argument('--max-files', type=int, default=900)
        command.add_argument('--max-bytes', type=int, default=60 * 1024 * 1024)
        command.add_argument('--max-file-bytes', type=int, default=8 * 1024 * 1024)
        command.add_argument('--max-manifest-bytes', type=int, default=SELECTION_LIMIT)
    args = parser.parse_args(argv)
    try:
        budgets = SnapshotBudget(max_files=args.max_files, max_bytes=args.max_bytes,
                                 max_file_bytes=args.max_file_bytes,
                                 max_manifest_bytes=args.max_manifest_bytes)
        if args.operation == 'capture':
            result = capture(args.source, load_selection(args.selection), args.output, budgets=budgets)
        elif args.operation == 'verify':
            result = verify(args.snapshot, budgets=budgets)
        else:
            result = materialize(args.snapshot, args.output, budgets=budgets)
        print(json.dumps({'status': 'complete', 'operation': args.operation, **result}, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({'status': 'cancelled', 'operation': args.operation}))
        return 130
    except SnapshotError as error:
        status = 'cancelled' if error.code == 'snapshot_cancelled' else 'rejected'
        print(json.dumps({'status': status, 'operation': args.operation,
                          'code': error.code, 'cleanupIncomplete': error.cleanup_incomplete}))
        return 130 if status == 'cancelled' else 2
    except (ContractError, OSError, ValueError, TypeError):
        print(json.dumps({'status': 'rejected', 'operation': args.operation,
                          'message': 'Local source operation did not complete; no execution or transfer was authorized'}))
        return 2
