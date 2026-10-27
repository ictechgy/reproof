"""Local disk-backed source captures, not provenance or execution capabilities.

Only explicitly selected inert regular files are copied. Existing BlobSet,
execution, instrumentation and AI-transfer contracts are deliberately unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from .core import ContractError
from .execution.artifacts import ArtifactError, open_directory
from .source_input_policy import inert_input_path
from .source_snapshot_json import bounded_json

CHUNK_BYTES = 64 * 1024
MAX_FILES = 32768
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 24 * 1024 * 1024
MAX_DIRECTORIES = 65536
MAX_DEPTH = 32
MAX_PARENT_DEPTH = 128
_KIND = 'reproof-local-source-snapshot'
_ENCODER = json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class SnapshotError(RuntimeError):
    """Static diagnostics intentionally exclude source paths and file contents."""
    def __init__(self, code='snapshot_rejected'):
        self.code = code
        self.cleanup_incomplete = code == 'snapshot_cleanup_incomplete'
        super().__init__(code)


def _require(condition, code='snapshot_rejected'):
    if not condition:
        raise SnapshotError(code)


@dataclass(frozen=True, slots=True)
class SnapshotBudget:
    max_files: int = 900
    max_bytes: int = 60 * 1024 * 1024
    max_file_bytes: int = MAX_FILE_BYTES
    max_manifest_bytes: int = MAX_MANIFEST_BYTES

    def __post_init__(self):
        for value, maximum in ((self.max_files, MAX_FILES), (self.max_bytes, MAX_BYTES),
                               (self.max_file_bytes, MAX_FILE_BYTES),
                               (self.max_manifest_bytes, MAX_MANIFEST_BYTES)):
            _require(type(value) is int and 1 <= value <= maximum, 'snapshot_budget_invalid')


def _budget(value):
    _require(value is None or type(value) is SnapshotBudget, 'snapshot_budget_invalid')
    return SnapshotBudget() if value is None else value


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SnapshotError('snapshot_cancelled')


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (_identity(info), info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _paths(paths, budget):
    _require(type(paths) in (list, tuple) and 1 <= len(paths) <= budget.max_files,
             'snapshot_selection_invalid')
    # Slice before tuple conversion so even concurrent list growth cannot make
    # the defensive copy exceed the declared count budget.
    paths = tuple(paths[:budget.max_files + 1])
    _require(1 <= len(paths) <= budget.max_files, 'snapshot_selection_invalid')
    normalized = set()
    directories = set()
    spelling = {}
    for path in paths:
        try:
            inert_input_path(path)
        except (ContractError, TypeError, ValueError, UnicodeError):
            raise SnapshotError('snapshot_selection_invalid') from None
        parts = path.split('/')
        _require(len(parts) <= MAX_DEPTH, 'snapshot_selection_invalid')
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            key = prefix.casefold()
            previous = spelling.setdefault(key, prefix)
            _require(previous == prefix and len(spelling) <= MAX_DIRECTORIES + budget.max_files,
                     'snapshot_selection_invalid')
            if index < len(parts):
                directories.add(key)
                _require(len(directories) <= MAX_DIRECTORIES, 'snapshot_selection_invalid')
        _require(key not in normalized, 'snapshot_selection_invalid')
        normalized.add(key)
    _require(not normalized.intersection(directories) and len(directories) <= MAX_DIRECTORIES,
             'snapshot_selection_invalid')
    return tuple(sorted(paths))


class _Root:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.fd = open_directory(self.path)
        self.identity = _identity(os.fstat(self.fd))

    def current(self):
        # Re-open every component without following symlinks, not Path.resolve().
        other = open_directory(self.path)
        try:
            _require(_identity(os.fstat(other)) == self.identity, 'snapshot_root_changed')
        finally:
            os.close(other)

    def close(self):
        os.close(self.fd)


def _parent(root_fd, relative):
    current = os.dup(root_fd)
    try:
        for part in relative.split('/')[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _stream(root_fd, relative, budget, remaining, *, sink=None, expected=None, cancel=None):
    parent = _parent(root_fd, relative)
    fd = None
    try:
        name = relative.split('/')[-1]
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'snapshot_file_invalid')
        _require(before.st_size <= min(budget.max_file_bytes, remaining), 'snapshot_byte_limit')
        sha = hashlib.sha256()
        count = 0
        while True:
            _cancel(cancel)
            raw = os.read(fd, min(CHUNK_BYTES, min(budget.max_file_bytes, remaining) - count + 1))
            if not raw:
                break
            count += len(raw)
            _require(count <= min(budget.max_file_bytes, remaining), 'snapshot_byte_limit')
            sha.update(raw)
            if sink is not None:
                sink.write(raw)
        after = os.fstat(fd)
        current_parent = _parent(root_fd, relative)
        try:
            _require(_identity(os.fstat(current_parent)) == _identity(os.fstat(parent)),
                     'snapshot_file_changed')
            bound = os.stat(name, dir_fd=current_parent, follow_symlinks=False)
        finally:
            os.close(current_parent)
        _require(_stamp(before) == _stamp(after) and _identity(bound) == _identity(before)
                 and count == before.st_size, 'snapshot_file_changed')
        result = {'path': relative, 'size': count, 'sha256': sha.hexdigest()}
        if expected is not None:
            _require(count == expected['size'] and result['sha256'] == expected['sha256'],
                     'snapshot_content_changed')
        return result
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _require_separate(root, output_parent_fd):
    """Reject aliases into the input using the very parent fd used for mkdir.

    Only the fixed parent component is traversed. No user-supplied traversal,
    symlink resolution, path normalization, or case assumptions are involved.
    """
    current = os.dup(output_parent_fd)
    try:
        for _ in range(MAX_PARENT_DEPTH):
            identity = _identity(os.fstat(current))
            _require(identity != root.identity, 'snapshot_output_overlaps_input')
            parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            try:
                parent_identity = _identity(os.fstat(parent))
            except BaseException:
                os.close(parent)
                raise
            if parent_identity == identity:
                os.close(parent)
                return
            os.close(current)
            current = parent
        raise SnapshotError('snapshot_parent_limit')
    finally:
        os.close(current)


class _Output:
    """Fresh output with a pinned root and an inode ledger for failure cleanup.

    Detected replacements/foreign entries are preserved. This is not a sandbox
    against a malicious process with the same UID racing every filesystem call.
    """
    def __init__(self, path, *, source_root=None):
        self.path = Path(path).absolute()
        _require(self.path.name not in ('', '.', '..'), 'snapshot_output_invalid')
        self.parent = open_directory(self.path.parent)
        self.fd = None
        self.ledger = []
        self.created = {}
        self.identity = None
        created_root = False
        try:
            if source_root is not None:
                _require_separate(source_root, self.parent)
            os.mkdir(self.path.name, mode=0o700, dir_fd=self.parent)
            created_root = True
            info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
            self.identity = _identity(info)
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self.parent)
            _require(_identity(os.fstat(self.fd)) == self.identity, 'snapshot_output_changed')
        except BaseException as error:
            if self.fd is not None:
                os.close(self.fd)
            os.close(self.parent)
            if created_root:
                raise SnapshotError('snapshot_cleanup_incomplete') from error
            raise

    def current(self):
        info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
        _require(stat.S_ISDIR(info.st_mode) and _identity(info) == self.identity,
                 'snapshot_output_changed')
        current = open_directory(self.path.parent)
        try:
            _require(_identity(os.fstat(current)) == _identity(os.fstat(self.parent)),
                     'snapshot_output_changed')
        finally:
            os.close(current)

    def create(self, relative):
        current = os.dup(self.fd)
        prefix = []
        try:
            for part in relative.split('/')[:-1]:
                prefix.append(part)
                path = '/'.join(prefix)
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    info = os.stat(part, dir_fd=current, follow_symlinks=False)
                    self.ledger.append((path, _identity(info), True))
                    self.created[path] = (_identity(info), True)
                except FileExistsError:
                    info = os.stat(part, dir_fd=current, follow_symlinks=False)
                    _require(self.created.get(path) == (_identity(info), True),
                             'snapshot_output_changed')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                try:
                    _require(_identity(os.fstat(child)) == _identity(info), 'snapshot_output_changed')
                except BaseException:
                    os.close(child)
                    raise
                os.close(current)
                current = child
            name = relative.split('/')[-1]
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=current)
            try:
                self.ledger.append((relative, _identity(os.fstat(fd)), False))
                return os.fdopen(fd, 'wb')
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(current)

    def rollback(self):
        clean = True
        for path, identity, directory in reversed(self.ledger):
            parent = None
            try:
                parent = _parent(self.fd, path)
                name = path.split('/')[-1]
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _identity(info) != identity:
                    clean = False
                    continue
                (os.rmdir if directory else os.unlink)(name, dir_fd=parent)
            except FileNotFoundError:
                clean = False
            except (OSError, SnapshotError):
                clean = False
            finally:
                if parent is not None:
                    os.close(parent)
        try:
            self.current()
            if clean:
                os.rmdir(self.path.name, dir_fd=self.parent)
        except (OSError, ArtifactError, SnapshotError):
            clean = False
        return clean

    def close(self):
        os.close(self.fd)
        os.close(self.parent)


def _chunks(value):
    for text in _ENCODER.iterencode(value):
        yield text.encode('utf-8')


def _manifest(files, budget):
    body = {'schemaVersion': 1, 'kind': _KIND, 'files': files}
    sha = hashlib.sha256()
    for raw in _chunks(body):
        sha.update(raw)
    result = dict(body, digest=sha.hexdigest())
    _require(sum(map(len, _chunks(result))) <= budget.max_manifest_bytes, 'snapshot_manifest_limit')
    return result


def _summary(manifest):
    return {'digest': manifest['digest'], 'files': len(manifest['files']),
            'bytes': sum(item['size'] for item in manifest['files']),
            'provenance': 'unverified-local-content'}


def _overlap(source, output):
    source, output = Path(source).absolute(), Path(output).absolute()
    _require(not output.is_relative_to(source) and not source.is_relative_to(output),
             'snapshot_output_overlaps_input')


def _fail(error):
    if isinstance(error, (SnapshotError, KeyboardInterrupt, SystemExit)):
        raise error
    raise SnapshotError('snapshot_io_rejected') from None


def capture(source, paths, output, *, budgets=None, cancel=None):
    budget = _budget(budgets)
    names = _paths(paths, budget)
    _overlap(source, output)
    root = destination = None
    try:
        _cancel(cancel)
        root = _Root(source)
        destination = _Output(output, source_root=root)
        files, remaining = [], budget.max_bytes
        for name in names:
            _cancel(cancel)
            with destination.create('data/' + name) as sink:
                item = _stream(root.fd, name, budget, remaining, sink=sink, cancel=cancel)
            files.append(item)
            remaining -= item['size']
        manifest = _manifest(files, budget)
        with destination.create('manifest.json') as sink:
            for raw in _chunks(manifest):
                _cancel(cancel)
                sink.write(raw)
        root.current()
        _validate_output(destination, manifest, budget, cancel, snapshot=True)
        _cancel(cancel)
        return _summary(manifest)
    except BaseException as error:
        if destination is not None and not destination.rollback():
            raise SnapshotError('snapshot_cleanup_incomplete') from error
        _fail(error)
    finally:
        if destination is not None:
            destination.close()
        if root is not None:
            root.close()


def _load(root, budget):
    parent = _parent(root.fd, 'manifest.json')
    fd = None
    try:
        fd = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_size <= budget.max_manifest_bytes, 'snapshot_manifest_invalid')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(budget.max_manifest_bytes + 1)
            after = os.fstat(stream.fileno())
        _require(_stamp(before) == _stamp(after) and len(raw) == before.st_size
                 and _identity(os.stat('manifest.json', dir_fd=parent, follow_symlinks=False)) == _identity(before),
                 'snapshot_manifest_changed')
        document = bounded_json(raw, max_items=budget.max_files, max_depth=3)
        _require(type(document) is dict and set(document) == {'schemaVersion', 'kind', 'files', 'digest'}
                 and type(document['schemaVersion']) is int and document['schemaVersion'] == 1
                 and document['kind'] == _KIND and type(document['files']) is list,
                 'snapshot_manifest_invalid')
        files = document['files']
        _require(1 <= len(files) <= budget.max_files, 'snapshot_manifest_invalid')
        for item in files:
            _require(type(item) is dict and set(item) == {'path', 'size', 'sha256'}
                     and type(item['size']) is int and 0 <= item['size'] <= budget.max_file_bytes
                     and type(item['sha256']) is str and len(item['sha256']) == 64
                     and all(c in '0123456789abcdef' for c in item['sha256']),
                     'snapshot_manifest_invalid')
        names = [item['path'] for item in files]
        _require(tuple(names) == _paths(names, budget)
                 and sum(item['size'] for item in files) <= budget.max_bytes,
                 'snapshot_manifest_invalid')
        expected = _manifest(files, budget)
        _require(document == expected, 'snapshot_manifest_invalid')
        actual_raw_hash = hashlib.sha256(raw).digest()
        canonical_hash = hashlib.sha256()
        for chunk in _chunks(expected):
            canonical_hash.update(chunk)
        _require(canonical_hash.digest() == actual_raw_hash, 'snapshot_manifest_invalid')
        return document, _stamp(before)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise SnapshotError('snapshot_manifest_invalid') from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _exact_tree(root_fd, names, cancel):
    expected = set(names)
    directories = set()
    for name in names:
        parts = name.split('/')
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    seen = set()
    def visit(fd, prefix):
        _cancel(cancel)
        with os.scandir(fd) as iterator:
            for entry in iterator:
                _cancel(cancel)
                path = prefix + entry.name
                _require(path in expected or path in directories, 'snapshot_tree_changed')
                info = entry.stat(follow_symlinks=False)
                if path in directories:
                    _require(stat.S_ISDIR(info.st_mode), 'snapshot_tree_changed')
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        _require(_identity(os.fstat(child)) == _identity(info), 'snapshot_tree_changed')
                        visit(child, path + '/')
                    finally:
                        os.close(child)
                else:
                    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'snapshot_tree_changed')
                    seen.add(path)
    visit(root_fd, '')
    _require(seen == expected, 'snapshot_tree_changed')


def _tree(root_fd, names, cancel):
    _exact_tree(root_fd, ['manifest.json', *('data/' + name for name in names)], cancel)


def _validate_output(destination, document, budget, cancel, *, snapshot):
    """Recheck every owned output inode and actual file bytes before success."""
    for path, identity, directory in destination.ledger:
        _cancel(cancel)
        parent = _parent(destination.fd, path)
        try:
            info = os.stat(path.split('/')[-1], dir_fd=parent, follow_symlinks=False)
            _require(_identity(info) == identity
                     and (stat.S_ISDIR(info.st_mode) if directory else
                          stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
                     'snapshot_output_changed')
        finally:
            os.close(parent)
    names = [item['path'] for item in document['files']]
    if snapshot:
        actual, manifest_stamp = _load(destination, budget)
        _require(actual == document, 'snapshot_output_changed')
        _tree(destination.fd, names, cancel)
    else:
        _exact_tree(destination.fd, names, cancel)
    remaining = budget.max_bytes
    for item in document['files']:
        _stream(destination.fd, ('data/' if snapshot else '') + item['path'],
                budget, remaining, expected=item, cancel=cancel)
        remaining -= item['size']
    if snapshot:
        _tree(destination.fd, names, cancel)
        _require(_stamp(os.stat('manifest.json', dir_fd=destination.fd, follow_symlinks=False)) == manifest_stamp,
                 'snapshot_manifest_changed')
    else:
        _exact_tree(destination.fd, names, cancel)
    destination.current()


def _consume(snapshot, output, budget, cancel):
    root = destination = None
    try:
        _cancel(cancel)
        root = _Root(snapshot)
        document, manifest_stamp = _load(root, budget)
        names = [item['path'] for item in document['files']]
        _tree(root.fd, names, cancel)
        if output is not None:
            _overlap(snapshot, output)
            destination = _Output(output, source_root=root)
        remaining = budget.max_bytes
        for item in document['files']:
            _cancel(cancel)
            if destination is None:
                _stream(root.fd, 'data/' + item['path'], budget, remaining,
                        expected=item, cancel=cancel)
            else:
                with destination.create(item['path']) as sink:
                    _stream(root.fd, 'data/' + item['path'], budget, remaining,
                            sink=sink, expected=item, cancel=cancel)
            remaining -= item['size']
        _tree(root.fd, names, cancel)
        _require(_stamp(os.stat('manifest.json', dir_fd=root.fd, follow_symlinks=False)) == manifest_stamp,
                 'snapshot_manifest_changed')
        root.current()
        if destination is not None:
            _validate_output(destination, document, budget, cancel, snapshot=False)
        _cancel(cancel)
        return _summary(document)
    except BaseException as error:
        if destination is not None and not destination.rollback():
            raise SnapshotError('snapshot_cleanup_incomplete') from error
        _fail(error)
    finally:
        if destination is not None:
            destination.close()
        if root is not None:
            root.close()


def verify(snapshot, *, budgets=None, cancel=None):
    return _consume(snapshot, None, _budget(budgets), cancel)


def materialize(snapshot, output, *, budgets=None, cancel=None):
    return _consume(snapshot, output, _budget(budgets), cancel)
