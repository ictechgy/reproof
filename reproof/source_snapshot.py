"""Local disk-backed source captures, not provenance or execution capabilities.

Only explicitly selected inert regular files are copied. Existing BlobSet,
execution, legacy instrumentation and AI-transfer contracts remain unchanged.
Explicit opt-in consumers may bind these local bytes through their own contracts.
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


@dataclass(slots=True)
class _FilePin:
    fd: int
    stamp: tuple | None = None


@dataclass(slots=True)
class _DirectoryPin:
    path: str
    fd: int
    stamp: tuple


class _OwnedWriter:
    """Seal a completed writer while a separate live descriptor pins its inode."""
    def __init__(self, owner, path, stream, pin):
        self.owner, self.path, self.stream, self.pin = owner, path, stream, pin

    def __enter__(self):
        return self.stream

    def __exit__(self, kind, value, traceback):
        try:
            self.stream.close()
        finally:
            try:
                self.owner._seal_file(self.path, self.pin)
            finally:
                self.owner.writer_active = False
        return False


class _Output:
    """Fresh output with bounded *live* cleanup ownership, not an inode ledger.

    At most 32 recently written files and the active directory prefix are pinned.
    Evicted files and closed directories are never reacquired for mutation or
    cleanup. Missing proof means preserved output and cleanupIncomplete, even on
    an otherwise ordinary failure. This is not a same-UID adversarial sandbox.
    """
    FILE_PIN_LIMIT = 32

    def __init__(self, path, *, source_root=None, parent_identity=None):
        self.path = Path(path).absolute()
        _require(self.path.name not in ('', '.', '..'), 'snapshot_output_invalid')
        self.parent = open_directory(self.path.parent)
        self.fd = None
        self.identity = None
        self.root_stamp = None
        self.directories = []
        self.file_pins = {}
        self.created_directories = set()
        self.created_files = set()
        self.retired_directories = set()
        self.retired_files = set()
        self.writer_active = False
        created_root = False
        try:
            if parent_identity is not None:
                _require(type(parent_identity) is tuple and len(parent_identity) == 2
                         and all(type(value) is int and value >= 0 for value in parent_identity)
                         and _identity(os.fstat(self.parent)) == parent_identity,
                         'snapshot_output_parent_changed')
            if source_root is not None:
                _require_separate(source_root, self.parent)
            os.mkdir(self.path.name, mode=0o700, dir_fd=self.parent)
            created_root = True
            info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
            self.identity = _identity(info)
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self.parent)
            live = os.fstat(self.fd)
            _require(_identity(live) == self.identity and live.st_nlink > 0,
                     'snapshot_output_changed')
            self.root_stamp = _stamp(live)
        except BaseException as error:
            if self.fd is not None:
                os.close(self.fd)
            os.close(self.parent)
            if created_root:
                raise SnapshotError('snapshot_cleanup_incomplete') from error
            raise

    def current(self):
        live = os.fstat(self.fd)
        info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
        _require(stat.S_ISDIR(live.st_mode) and live.st_nlink > 0
                 and _stamp(live) == self.root_stamp and _stamp(info) == self.root_stamp,
                 'snapshot_output_changed')
        current = open_directory(self.path.parent)
        try:
            _require(_identity(os.fstat(current)) == _identity(os.fstat(self.parent)),
                     'snapshot_output_changed')
        finally:
            os.close(current)

    def _check_chain(self):
        self.current()
        parent = self.fd
        for entry in self.directories:
            live = os.fstat(entry.fd)
            bound = os.stat(entry.path.rsplit('/', 1)[-1], dir_fd=parent, follow_symlinks=False)
            _require(stat.S_ISDIR(live.st_mode) and live.st_nlink > 0
                     and _stamp(live) == entry.stamp and _stamp(bound) == entry.stamp,
                     'snapshot_output_changed')
            parent = entry.fd

    def _parent_pin(self, path):
        parent_path = path.rpartition('/')[0]
        if not parent_path:
            return self.fd
        for entry in self.directories:
            if entry.path == parent_path:
                return entry.fd
        raise SnapshotError('snapshot_output_unpinned')

    def _refresh_parent(self, path):
        parent_path = path.rpartition('/')[0]
        if not parent_path:
            self.root_stamp = _stamp(os.fstat(self.fd))
            return
        for entry in self.directories:
            if entry.path == parent_path:
                entry.stamp = _stamp(os.fstat(entry.fd))
                return
        raise SnapshotError('snapshot_output_unpinned')

    def _retire_file(self, path):
        pin = self.file_pins.pop(path)
        self.retired_files.add(path)
        os.close(pin.fd)

    def _retire_directory(self):
        entry = self.directories.pop()
        for path in list(self.file_pins):
            if path.startswith(entry.path + '/'):
                self._retire_file(path)
        self.retired_directories.add(entry.path)
        os.close(entry.fd)

    def _file_current(self, path, pin, *, sealed=True):
        self._check_chain()
        _require(self.file_pins.get(path) is pin, 'snapshot_output_unpinned')
        parent = self._parent_pin(path)
        live = os.fstat(pin.fd)
        bound = os.stat(path.rsplit('/', 1)[-1], dir_fd=parent, follow_symlinks=False)
        _require(stat.S_ISREG(live.st_mode) and live.st_nlink == 1
                 and _stamp(bound) == _stamp(live), 'snapshot_output_changed')
        if sealed:
            _require(pin.stamp is not None and _stamp(live) == pin.stamp,
                     'snapshot_output_changed')
        return live

    def _seal_file(self, path, pin):
        pin.stamp = _stamp(self._file_current(path, pin, sealed=False))

    def validate_pins(self):
        self._check_chain()
        for path, pin in self.file_pins.items():
            self._file_current(path, pin)

    def create(self, relative):
        _require(not self.writer_active and relative not in self.created_files,
                 'snapshot_output_changed')
        self._check_chain()
        parts = relative.split('/')[:-1]
        prefixes = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
        _require(len(prefixes) <= MAX_DEPTH, 'snapshot_output_changed')
        common = 0
        while (common < len(prefixes) and common < len(self.directories)
               and self.directories[common].path == prefixes[common]):
            common += 1
        while len(self.directories) > common:
            self._retire_directory()
        for path in prefixes[common:]:
            self._check_chain()
            _require(path not in self.created_directories, 'snapshot_output_unpinned')
            parent = self._parent_pin(path)
            name = path.rsplit('/', 1)[-1]
            os.mkdir(name, mode=0o700, dir_fd=parent)
            self.created_directories.add(path)
            child = None
            try:
                self._refresh_parent(path)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                live = os.fstat(child)
                bound = os.stat(name, dir_fd=parent, follow_symlinks=False)
                _require(stat.S_ISDIR(live.st_mode) and live.st_nlink > 0
                         and _stamp(live) == _stamp(bound), 'snapshot_output_changed')
                self.directories.append(_DirectoryPin(path, child, _stamp(live)))
                child = None
            except BaseException:
                self.retired_directories.add(path)
                raise
            finally:
                if child is not None:
                    os.close(child)
        self._check_chain()
        while len(self.file_pins) >= self.FILE_PIN_LIMIT:
            self._retire_file(next(iter(self.file_pins)))
        parent = self._parent_pin(relative)
        name = relative.rsplit('/', 1)[-1]
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        self.created_files.add(relative)
        pin_fd = None
        try:
            self._refresh_parent(relative)
            pin_fd = os.dup(fd)
            pin = _FilePin(pin_fd)
            self.file_pins[relative] = pin
            self._file_current(relative, pin, sealed=False)
            stream = os.fdopen(fd, 'wb')
            self.writer_active = True
            return _OwnedWriter(self, relative, stream, pin)
        except BaseException:
            self.file_pins.pop(relative, None)
            self.retired_files.add(relative)
            if pin_fd is not None:
                os.close(pin_fd)
            os.close(fd)
            raise

    def rollback(self):
        clean = not (self.retired_files or self.retired_directories or self.writer_active)
        for path, pin in reversed(list(self.file_pins.items())):
            try:
                self._file_current(path, pin)
                parent = self._parent_pin(path)
                os.unlink(path.rsplit('/', 1)[-1], dir_fd=parent)
                self._refresh_parent(path)
                # Keep the inode alive until after the bound name is removed.
                os.close(pin.fd)
                del self.file_pins[path]
            except (OSError, ArtifactError, SnapshotError):
                clean = False
        while self.directories:
            entry = self.directories[-1]
            try:
                self._check_chain()
                parent = self._parent_pin(entry.path)
                os.rmdir(entry.path.rsplit('/', 1)[-1], dir_fd=parent)
                self._refresh_parent(entry.path)
                self.directories.pop()
                os.close(entry.fd)
            except (OSError, ArtifactError, SnapshotError):
                clean = False
                break
        try:
            self.current()
            if clean and not self.file_pins and not self.directories:
                os.rmdir(self.path.name, dir_fd=self.parent)
            else:
                clean = False
        except (OSError, ArtifactError, SnapshotError):
            clean = False
        return clean

    def close(self):
        for pin in self.file_pins.values():
            os.close(pin.fd)
        self.file_pins.clear()
        for entry in reversed(self.directories):
            os.close(entry.fd)
        self.directories.clear()
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


def capture(source, paths, output, *, budgets=None, cancel=None, output_parent_identity=None):
    budget = _budget(budgets)
    names = _paths(paths, budget)
    _overlap(source, output)
    root = destination = None
    try:
        _cancel(cancel)
        root = _Root(source)
        destination = _Output(output, source_root=root, parent_identity=output_parent_identity)
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
    # Only live retained pins prove cleanup ownership. Retired entries below
    # are checked as content, never promoted from stored inode numbers.
    destination.validate_pins()
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
    destination.validate_pins()


def _consume(snapshot, output, budget, cancel, output_parent_identity=None):
    root = destination = None
    try:
        _cancel(cancel)
        root = _Root(snapshot)
        document, manifest_stamp = _load(root, budget)
        names = [item['path'] for item in document['files']]
        _tree(root.fd, names, cancel)
        if output is not None:
            _overlap(snapshot, output)
            destination = _Output(output, source_root=root, parent_identity=output_parent_identity)
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


def materialize(snapshot, output, *, budgets=None, cancel=None, output_parent_identity=None):
    return _consume(snapshot, output, _budget(budgets), cancel, output_parent_identity)


MAX_CONSUMER_FILES = 128
MAX_CHANGE_FILES = 16
MAX_CONSUMER_BYTES = 16 * 1024 * 1024


def _expected_document(document, expected):
    _require(type(expected) is dict and set(expected) == {'digest', 'files', 'bytes'}
             and type(expected['digest']) is str and len(expected['digest']) == 64
             and all(c in '0123456789abcdef' for c in expected['digest'])
             and type(expected['files']) is int and 1 <= expected['files'] <= MAX_FILES
             and type(expected['bytes']) is int and 0 <= expected['bytes'] <= MAX_BYTES,
             'snapshot_reference_invalid')
    expected = {key: expected[key] for key in ('digest', 'files', 'bytes')}
    summary = _summary(document)
    _require(all(expected[key] == summary[key] for key in ('digest', 'files', 'bytes')),
             'snapshot_reference_mismatch')


def _finish_input(root, document, manifest_stamp, cancel):
    _tree(root.fd, [item['path'] for item in document['files']], cancel)
    _require(_stamp(os.stat('manifest.json', dir_fd=root.fd, follow_symlinks=False)) == manifest_stamp,
             'snapshot_manifest_changed')
    root.current()
    _cancel(cancel)


def _consumer_limits(max_files, max_bytes):
    _require(type(max_files) is int and 1 <= max_files <= MAX_CONSUMER_FILES
             and type(max_bytes) is int and 1 <= max_bytes <= MAX_CONSUMER_BYTES,
             'snapshot_consumer_limit')


def _read_consumer(snapshot, paths, *, expected, budgets, cancel, max_bytes, visitor=None):
    import io
    budget = _budget(budgets)
    root = None
    try:
        _cancel(cancel)
        root = _Root(snapshot)
        document, manifest_stamp = _load(root, budget)
        if expected is not None:
            _expected_document(document, expected)
        _tree(root.fd, [item['path'] for item in document['files']], cancel)
        selected = set(paths or ())
        declared = {item['path']: item for item in document['files']}
        _require(selected <= declared.keys(), 'snapshot_consumer_selection_invalid')
        if visitor is None:
            _require(sum(declared[name]['size'] for name in selected) <= max_bytes,
                     'snapshot_consumer_limit')
        result, remaining = {}, budget.max_bytes
        for item in document['files']:
            _cancel(cancel)
            if item['path'] in selected:
                with io.BytesIO() as sink:
                    _stream(root.fd, 'data/' + item['path'], budget, remaining,
                            sink=sink, expected=item, cancel=cancel)
                    if visitor is None:
                        result[item['path']] = sink.getvalue()
                    else:
                        visitor(item['path'], sink.getvalue())
            else:
                _stream(root.fd, 'data/' + item['path'], budget, remaining,
                        expected=item, cancel=cancel)
            remaining -= item['size']
        _finish_input(root, document, manifest_stamp, cancel)
        return document if paths is None or visitor is not None else result
    except BaseException as error:
        _fail(error)
    finally:
        if root is not None:
            root.close()


def inspect_snapshot(snapshot, *, expected=None, budgets=None, cancel=None):
    """Verify actual contents and return bounded manifest metadata, not authority."""
    return _read_consumer(snapshot, None, expected=expected, budgets=budgets,
                          cancel=cancel, max_bytes=MAX_CONSUMER_BYTES)


def read_snapshot_files(snapshot, paths, *, expected, budgets=None, cancel=None,
                        max_files=64, max_bytes=MAX_CONSUMER_BYTES):
    """Read limited metadata files while verifying all actual snapshot contents."""
    _consumer_limits(max_files, max_bytes)
    _require(type(paths) in (list, tuple) and 1 <= len(paths) <= max_files,
             'snapshot_consumer_limit')
    names = tuple(paths[:max_files + 1])
    _require(1 <= len(names) <= max_files, 'snapshot_consumer_limit')
    names = _paths(names, _budget(budgets))
    _require(expected is not None, 'snapshot_reference_invalid')
    return _read_consumer(snapshot, names, expected=expected, budgets=budgets,
                          cancel=cancel, max_bytes=max_bytes)


def scan_snapshot_files(snapshot, paths, visitor, *, expected, budgets=None, cancel=None):
    """Visit selected files one at a time with a trusted, non-retaining callback."""
    _require(callable(visitor) and expected is not None, 'snapshot_consumer_invalid')
    names = _paths(paths, _budget(budgets))
    return _read_consumer(snapshot, names, expected=expected, budgets=budgets,
                          cancel=cancel, max_bytes=MAX_CONSUMER_BYTES, visitor=visitor)


def snapshot_manifest_reference(document, *, budgets=None):
    """Validate metadata only; this does not verify actual bytes or provenance."""
    budget = _budget(budgets)
    _require(type(document) is dict and len(document) == 4
             and set(document) == {'schemaVersion', 'kind', 'files', 'digest'}
             and type(document['schemaVersion']) is int and document['schemaVersion'] == 1
             and document['kind'] == _KIND and type(document['files']) is list
             and 1 <= len(document['files']) <= budget.max_files, 'snapshot_manifest_invalid')
    selected = document['files'][:budget.max_files + 1]
    _require(1 <= len(selected) <= budget.max_files, 'snapshot_manifest_invalid')
    files = []
    for item in selected:
        _require(type(item) is dict and len(item) == 3 and set(item) == {'path', 'size', 'sha256'}
                 and type(item['size']) is int and 0 <= item['size'] <= budget.max_file_bytes
                 and type(item['sha256']) is str and len(item['sha256']) == 64
                 and all(c in '0123456789abcdef' for c in item['sha256']),
                 'snapshot_manifest_invalid')
        files.append({'path': item['path'], 'size': item['size'], 'sha256': item['sha256']})
    names = [item['path'] for item in files]
    _require(tuple(names) == _paths(names, budget)
             and sum(item['size'] for item in files) <= budget.max_bytes,
             'snapshot_manifest_invalid')
    canonical = _manifest(files, budget)
    _require(document['digest'] == canonical['digest'], 'snapshot_manifest_invalid')
    summary = _summary(canonical)
    return {key: summary[key] for key in ('digest', 'files', 'bytes')}


def derive_snapshot(snapshot, output, changes, *, expected, budgets=None, cancel=None,
                    output_parent_identity=None):
    """Derive fresh content from bounded explicit bytes, never historical provenance.

    Callers using output_parent_identity must keep that parent descriptor alive.
    """
    from itertools import islice
    budget = _budget(budgets)
    _require(type(changes) is dict and 1 <= len(changes) <= MAX_CHANGE_FILES,
             'snapshot_changes_invalid')
    try:
        changes = dict(islice(changes.items(), MAX_CHANGE_FILES + 1))
    except RuntimeError:
        raise SnapshotError('snapshot_changes_invalid') from None
    _require(1 <= len(changes) <= MAX_CHANGE_FILES
             and all(type(raw) is bytes and len(raw) <= budget.max_file_bytes for raw in changes.values())
             and sum(map(len, changes.values())) <= MAX_CONSUMER_BYTES,
             'snapshot_changes_invalid')
    _paths(tuple(changes), budget)
    _require(expected is not None, 'snapshot_reference_invalid')
    _overlap(snapshot, output)
    root = destination = None
    try:
        _cancel(cancel)
        root = _Root(snapshot)
        document, manifest_stamp = _load(root, budget)
        _expected_document(document, expected)
        original = {item['path']: item for item in document['files']}
        _tree(root.fd, list(original), cancel)
        names = _paths(tuple(set(original) | set(changes)), budget)
        _require(sum(len(changes[name]) if name in changes else original[name]['size']
                     for name in names) <= budget.max_bytes, 'snapshot_bytes_limit')
        prospective = [{'path': name, 'size': len(changes[name]),
                        'sha256': hashlib.sha256(changes[name]).hexdigest()}
                       if name in changes else original[name] for name in names]
        derived = _manifest(prospective, budget)
        destination = _Output(output, source_root=root, parent_identity=output_parent_identity)
        remaining = budget.max_bytes
        for name in names:
            _cancel(cancel)
            if name in changes:
                if name in original:
                    _stream(root.fd, 'data/' + name, budget, remaining,
                            expected=original[name], cancel=cancel)
                    remaining -= original[name]['size']
                with destination.create('data/' + name) as sink:
                    raw = memoryview(changes[name])
                    for start in range(0, len(raw), CHUNK_BYTES):
                        _cancel(cancel)
                        sink.write(raw[start:start + CHUNK_BYTES])
            else:
                with destination.create('data/' + name) as sink:
                    _stream(root.fd, 'data/' + name, budget, remaining,
                            sink=sink, expected=original[name], cancel=cancel)
                remaining -= original[name]['size']
        _finish_input(root, document, manifest_stamp, cancel)
        with destination.create('manifest.json') as sink:
            for raw in _chunks(derived):
                _cancel(cancel)
                sink.write(raw)
        _validate_output(destination, derived, budget, cancel, snapshot=True)
        _cancel(cancel)
        return _summary(derived)
    except BaseException as error:
        if destination is not None and not destination.rollback():
            raise SnapshotError('snapshot_cleanup_incomplete') from error
        _fail(error)
    finally:
        if destination is not None:
            destination.close()
        if root is not None:
            root.close()
