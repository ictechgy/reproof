"""Shared issue commands; credentials arrive from a prompt or bounded stdin."""
import getpass
from pathlib import Path
import sys
import time
import uuid

from ..issue_package import _json
from .client import IssueClient
from .model import check, public_id


def add_issue_parser(commands):
    parent = commands.add_parser('live-issues', help='Authenticated project recording, packages and fixed-budget replay')
    operations = parent.add_subparsers(dest='operation', required=True)
    leaves = []
    for name in ('projects', 'list'):
        leaves.append(operations.add_parser(name))
    for name in ('show', 'stop', 'cancel', 'input', 'save', 'approve', 'replay', 'export'):
        child = operations.add_parser(name); child.add_argument('id'); leaves.append(child)
        if name in {'input', 'save'}: child.add_argument('--file', type=Path, required=True)
        if name == 'approve':
            child.add_argument('--revision', type=int, required=True)
            child.add_argument('--specification-digest', required=True)
            child.add_argument('--bind-imported', action='store_true')
        if name == 'replay':
            child.add_argument('--device', required=True); child.add_argument('--client-id', default=None)
            child.add_argument('--specification-digest', required=True)
        if name == 'export': child.add_argument('--output', type=Path, required=True)
        if name in {'stop', 'cancel', 'replay'}:
            child.add_argument('--wait', action='store_true'); child.add_argument('--wait-timeout', type=int, default=180)
    start = operations.add_parser('start'); leaves.append(start)
    for name in ('project', 'application', 'build', 'device'): start.add_argument('--' + name, required=True)
    start.add_argument('--preparation', action='append', default=[]); start.add_argument('--unprepared', action='store_true')
    start.add_argument('--client-id', default=None); start.add_argument('--wait', action='store_true')
    start.add_argument('--wait-timeout', type=int, default=180)
    imp = operations.add_parser('import'); leaves.append(imp)
    imp.add_argument('--project', required=True); imp.add_argument('--file', type=Path, required=True)
    for name in ('repairs', 'repair-propose', 'repair-verify', 'repair-show', 'repair-cancel', 'repair-patch', 'repair-diagnostics'):
        child = operations.add_parser(name); child.add_argument('id'); leaves.append(child)
        if name in {'repair-propose', 'repair-verify'}:
            child.add_argument('--specification-digest', required=True)
            child.add_argument('--request-id', default=None)
        if name in {'repair-propose', 'repair-verify', 'repair-cancel'}:
            child.add_argument('--wait', action='store_true'); child.add_argument('--wait-timeout', type=int, default=360)
        if name in {'repair-patch', 'repair-diagnostics'}:
            child.add_argument('--output', type=Path, required=True,
                               help='New directory for review files; existing output is preserved')
    for child in leaves:
        child.add_argument('--server', default='http://127.0.0.1:8765')
        child.add_argument('--credential-stdin', action='store_true', help='Read one personal credential from stdin, without echo')


def run_issue_command(args):
    if getattr(args, 'wait', False):
        check(1 <= args.wait_timeout <= 1800, 'invalid_timeout', 'Issue wait must be 1–1800 seconds', 400)
    if args.credential_stdin:
        credential = sys.stdin.read(514).rstrip('\n')
    else:
        check(sys.stdin.isatty(), 'credential_required', 'Use --credential-stdin for noninteractive shared access', 400)
        credential = getpass.getpass('Personal access credential: ')
    try: client = IssueClient(args.server, credential)
    finally: credential = ''
    if hasattr(args, 'id'): public_id(args.id)
    path = '/api/release/issues/' + args.id if hasattr(args, 'id') else None
    if args.operation in {'projects', 'list'}:
        result = client.call('/api/release/projects' if args.operation == 'projects' else '/api/release/issues')
    elif args.operation == 'show': result = client.call(path)
    elif args.operation in {'stop', 'cancel'}: result = client.call(path + '/' + args.operation, {})
    elif args.operation in {'input', 'save'}:
        check(args.file.is_file() and not args.file.is_symlink(), 'invalid_file', 'A local JSON request file is required', 400)
        with args.file.open('rb') as stream: raw = stream.read(5 * 1024 * 1024 + 1)
        check(len(raw) <= 5 * 1024 * 1024, 'request_limit', 'Request file is too large', 413)
        result = client.call(path + ('/specifications' if args.operation == 'save' else '/input'), _json(raw))
    elif args.operation == 'approve': result = client.call(path + '/approve', {
        'revision': args.revision, 'specificationDigest': args.specification_digest, 'bindImported': args.bind_imported})
    elif args.operation == 'replay': result = client.call(path + '/replay', {
        'deviceId': args.device, 'clientId': args.client_id or 'cli_' + uuid.uuid4().hex,
        'specificationDigest': args.specification_digest})
    elif args.operation == 'export': result = client.export_package(args.id, args.output)
    elif args.operation == 'import': result = client.import_package(args.project, args.file)
    elif args.operation == 'start': result = client.call('/api/release/issues', {
        'projectId': args.project, 'applicationId': args.application, 'buildId': args.build,
        'deviceId': args.device, 'clientId': args.client_id or 'cli_' + uuid.uuid4().hex,
        'preparationIds': args.preparation, 'unprepared': args.unprepared})
    elif args.operation == 'repairs': result = client.call(path + '/repairs')
    elif args.operation in {'repair-propose', 'repair-verify'}:
        result = client.call(path + '/repairs', {'requestId': args.request_id or 'cli_' + uuid.uuid4().hex,
            'specificationDigest': args.specification_digest,
            'mode': 'propose' if args.operation == 'repair-propose' else 'verify'})
    elif args.operation in {'repair-show', 'repair-cancel', 'repair-patch', 'repair-diagnostics'}:
        path = '/api/release/repairs/' + args.id
        if args.operation == 'repair-show': result = client.call(path)
        elif args.operation == 'repair-cancel': result = client.call(path + '/cancel', {})
        else:
            from ..execution.artifacts import ArtifactError, BlobSet
            from ..execution.wire import canonical
            from .model import LiveError
            diagnostic = args.operation == 'repair-diagnostics'
            document = client.call(path + ('/diagnostics' if diagnostic else '/proposal'))
            raw = canonical(document)
            try:
                entries = (('diagnostics.json', raw),) if diagnostic else (
                    ('proposal.json', raw), ('change.diff', document['patch'].encode('utf-8')))
                output = BlobSet(entries)
                output.write_new(args.output.absolute())
            except (ArtifactError, OSError, KeyError, TypeError):
                raise LiveError('repair_output', 'Valid review data and a new output directory are required', 409) from None
            result = {'repairId': args.id, 'outputDirectory': str(args.output.absolute()),
                      'outputDigest': output.digest, 'verified': False}
    if getattr(args, 'wait', False):
        repair = args.operation.startswith('repair-')
        key, field = ('repair', 'status') if repair else ('issue', 'state')
        active = {'created', 'running'} if repair else {'preparing', 'finalizing', 'replaying', 'cancelling'}
        identifier = result[key]['id']; deadline = time.monotonic() + args.wait_timeout
        while result[key][field] in active:
            check(time.monotonic() < deadline, 'wait_timeout', 'Client wait expired; the operation continues on the server', 408)
            time.sleep(.2); result = client.call('/api/release/' + ('repairs/' if repair else 'issues/') + identifier)
    return result
