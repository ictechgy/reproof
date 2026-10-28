"""Bounded repair jobs, terminal cancellation and conservative restart recovery."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

from . import contracts
from .execution.artifacts import ArtifactError, BlobSet, open_directory
from .execution.wire import canonical, safe_transfer_path


TERMINAL = frozenset({'verified', 'proposal-ready', 'failed', 'blocked', 'cancelled', 'quarantined', 'interrupted'})
_EXECUTING = frozenset({'building', 'signing', 'installing', 'validating', 'replaying', 'finalizing'})
_PHASES = frozenset({'created', 'freezing', 'proposing', 'candidate-ready', 'finished'}) | _EXECUTING


class RepairJournalError(RuntimeError):
    def __init__(self, code='repair_storage'):
        super().__init__('Repair job storage is unavailable or closed')
        self.code = code


def _require(condition, code='repair_storage'):
    if not condition:
        raise RepairJournalError(code)


class RepairCancellation:
    def __init__(self, journal, identifier):
        self.journal, self.identifier = journal, identifier

    def is_set(self):
        with self.journal._lock:
            if self.journal._db is None:
                return True
            value = self.journal.get(self.identifier)
            return value['cancelRequested'] or value['status'] in {'cancelled', 'quarantined', 'interrupted'}

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            with self.journal._changed:
                self.journal._changed.wait(.05 if deadline is None else max(0, min(.05, deadline-time.monotonic())))
        return self.is_set()


class _SnapshotOutput:
    """Process-local pinned publication context; reference metadata is not proof."""
    def __init__(self, journal, identifier, name, root_fd, parent_fd):
        self.journal, self.identifier, self.name = journal, identifier, name
        self.root_fd, self.parent_fd = root_fd, parent_fd
        self.path = journal.root / identifier / name
        info = os.fstat(parent_fd)
        self.parent_identity = (info.st_dev, info.st_ino)
        self.completed = False
        self.closed = False
        self.sidecar = None
        self.candidate_fd = None
        self.completing = False
        self.sidecar_fds = []

    def current(self):
        _require(not self.closed and self.journal._db is not None)
        root = open_directory(self.journal.root)
        try:
            live, pinned = os.fstat(root), os.fstat(self.root_fd)
            _require((live.st_dev, live.st_ino) == (pinned.st_dev, pinned.st_ino)
                     and pinned.st_nlink > 0, 'repair_output_changed')
            info = os.stat(self.identifier, dir_fd=root, follow_symlinks=False)
            parent = os.fstat(self.parent_fd)
            _require(stat.S_ISDIR(info.st_mode) and parent.st_nlink > 0
                     and (info.st_dev, info.st_ino) == self.parent_identity
                     and (parent.st_dev, parent.st_ino) == self.parent_identity,
                     'repair_output_changed')
        finally:
            os.close(root)

    def _check_sidecar(self):
        if self.sidecar is None:
            return
        directory, fd, stamp = self.sidecar
        entry = os.stat('proposal', dir_fd=self.parent_fd, follow_symlinks=False)
        live = os.fstat(directory)
        _require(live.st_nlink > 0 and stat.S_ISDIR(entry.st_mode)
                 and (entry.st_dev, entry.st_ino) == (live.st_dev, live.st_ino), 'repair_output_changed')
        entry = os.stat('proposal.json', dir_fd=directory, follow_symlinks=False)
        live = os.fstat(fd)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
        _require(live.st_nlink == 1 and identity(live) == stamp and identity(entry) == stamp,
                 'repair_output_changed')

    def write_sidecar(self, name, raw):
        """Write only the bounded proposal sidecar under the retained job pin."""
        _require(name == 'proposal' and type(raw) is bytes and 0 < len(raw) <= 2 * 1024 ** 2,
                 'repair_request')
        with self.journal._lock:
            self.current()
            _require(not self.completed and not self.completing and self.sidecar is None, 'repair_terminal')
            document = self.journal.get(self.identifier)
            _require(document['status'] not in TERMINAL and not document['cancelRequested'], 'repair_terminal')
            _require(name not in document['outputs']
                     and document['usedBytes'] + len(raw) <= document['reservedBytes'], 'repair_disk_limit')
            document['usedBytes'] += len(raw)
            document['outputs'][name] = {'digest': BlobSet((('proposal.json', raw),)).digest, 'bytes': len(raw),
                'status': 'reserved', 'paths': ['proposal.json'], 'cleanupBlocked': True}
            self.journal._put(document)
            os.mkdir(name, 0o700, dir_fd=self.parent_fd)
            directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
            self.sidecar_fds.append(directory)
            fd = os.open('proposal.json', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=directory)
            self.sidecar_fds.append(fd)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view[:65536])
                _require(written > 0)
                view = view[written:]
            os.fsync(fd); os.fsync(directory); os.fsync(self.parent_fd)
            info = os.fstat(fd)
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
            self.sidecar = (directory, fd, stamp)
            self.current()
            self._check_sidecar()
            document['outputs'][name]['status'] = 'complete'
            self.journal._put(document)
            return self.path.parent / name

    def _check_candidate(self):
        _require(self.candidate_fd is not None, 'repair_output_changed')
        entry = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        live = os.fstat(self.candidate_fd)
        _require(stat.S_ISDIR(entry.st_mode) and live.st_nlink > 0
                 and (entry.st_dev, entry.st_ino) == (live.st_dev, live.st_ino), 'repair_output_changed')

    def complete(self, reference, *, cancel=None):
        from .source_snapshot import SnapshotBudget, inspect_snapshot
        _require(cancel is None or callable(getattr(cancel, 'is_set', None)), 'repair_request')
        _require(type(reference) is dict
                 and set(reference) == {'schemaVersion', 'kind', 'digest', 'files', 'bytes'},
                 'repair_snapshot_reference')
        reference = dict(reference)
        _require(type(reference['schemaVersion']) is int and reference['schemaVersion'] == 1
                 and reference['kind'] == 'reproof-local-source-snapshot'
                 and type(reference['digest']) is str and len(reference['digest']) == 64
                 and all(c in '0123456789abcdef' for c in reference['digest'])
                 and type(reference['files']) is int and 1 <= reference['files'] <= 32768
                 and type(reference['bytes']) is int and 0 <= reference['bytes'] <= 256 * 1024 ** 2,
                 'repair_snapshot_reference')
        with self.journal._lock:
            self.current()
            document = self.journal.get(self.identifier)
            _require(not self.completed and not self.completing and document['status'] not in TERMINAL
                     and not document['cancelRequested'], 'repair_terminal')
            output = document['outputs'][self.name]
            _require(output['status'] == 'reserved' and reference['bytes'] <= output['bytes'],
                     'repair_disk_limit')
            self.candidate_fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=self.parent_fd)
            self.completing = True
            self._check_candidate()
            self._check_sidecar()
            manifest = os.stat('manifest.json', dir_fd=self.candidate_fd, follow_symlinks=False)
            _require(stat.S_ISREG(manifest.st_mode) and manifest.st_nlink == 1
                     and reference['bytes'] + manifest.st_size <= output['bytes'], 'repair_disk_limit')
            budget = SnapshotBudget(max_files=32768, max_bytes=min(256 * 1024 ** 2, output['bytes']))
        # Cancellation must be able to take the journal lock during the scan.
        journal_cancel = self.journal.cancellation(self.identifier)
        class CombinedCancellation:
            def is_set(self):
                return journal_cancel.is_set() or (cancel is not None and cancel.is_set())
        combined_cancel = CombinedCancellation()
        inspect_snapshot(self.path, expected={key: reference[key] for key in ('digest', 'files', 'bytes')},
                         budgets=budget, cancel=combined_cancel)
        _require(not combined_cancel.is_set(), 'repair_cancelled')
        with self.journal._lock:
            self.current()
            self._check_candidate()
            self._check_sidecar()
            document = self.journal.get(self.identifier)
            _require(document['status'] not in TERMINAL and not document['cancelRequested'], 'repair_terminal')
            document['outputs'][self.name].update(status='complete', reference=reference)
            self.journal._put(document)
            self.completed = True


class RepairJournal:
    """One writer; declared data reservations are retained with every result.

    disk_limit bounds blob payloads. The fixed SQLite/WAL allowance is 8 MiB;
    a composing service must include it in its overall storage reservation.
    Completed jobs are retained; no automatic eviction or authority reissue.
    """
    def __init__(self, root, *, disk_limit=512 * 1024 * 1024, max_jobs=128):
        _require(type(disk_limit) is int and 0 < disk_limit <= 512 * 1024 ** 3)
        _require(type(max_jobs) is int and 1 <= max_jobs <= 1024)
        self.root = Path(root).absolute()
        self.disk_limit, self.max_jobs = disk_limit, max_jobs
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._db = None; self._fd = None; self._directory = None
        try:
            parent = open_directory(self.root.parent)
            try:
                safe_transfer_path(self.root.name)
                try:
                    os.mkdir(self.root.name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                self._directory = os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            finally:
                os.close(parent)
            info = os.fstat(self._directory)
            _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
            self._fd = os.open('.writer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._directory)
            info = os.fstat(self._fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in ('jobs.sqlite3', 'jobs.sqlite3-wal', 'jobs.sqlite3-shm'):
                try:
                    info = os.stat(name, dir_fd=self._directory, follow_symlinks=False)
                    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1)
                except FileNotFoundError:
                    pass
            fd = os.open('jobs.sqlite3', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self._directory)
            os.close(fd)
            self._db = sqlite3.connect(self.root / 'jobs.sqlite3', isolation_level=None, check_same_thread=False)
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA busy_timeout=5000')
            self._db.execute('PRAGMA journal_size_limit=2097152')
            self._db.execute('PRAGMA wal_autocheckpoint=32')
            _require(self._db.execute('PRAGMA max_page_count=1024').fetchone()[0] <= 1024)
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,
                    reserved_bytes INTEGER NOT NULL,body TEXT NOT NULL,
                    UNIQUE(project_id,owner_id,request_id));
            ''')
            version = self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
            if version is None:
                self._db.execute("INSERT INTO metadata VALUES('version',1)")
            else:
                _require(version[0] == 1)
            with self._lock:
                for document in self.list():
                    if document['status'] not in TERMINAL:
                        document.update(status='quarantined' if document['phase'] in _EXECUTING else 'interrupted',
                                        reason='process_restarted', phase='finished')
                        self._put(document)
        except (OSError, sqlite3.Error, ArtifactError, contracts.ContractError):
            self.close()
            raise RepairJournalError() from None
        except Exception:
            self.close(); raise

    def _put(self, document, *, release_reservation=False):
        raw = canonical(document)
        _require(len(raw) <= 256 * 1024)
        try:
            if release_reservation:
                _require(document['status'] in TERMINAL and document['outputsExpired']
                    and document['usedBytes'] == document['reservedBytes'] == 0
                    and all(item['status'] == 'expired' for item in document['outputs'].values()))
                self._db.execute('UPDATE jobs SET body=?,reserved_bytes=0 WHERE id=?',
                                 (raw.decode('utf-8'), document['id']))
            else:
                self._db.execute('UPDATE jobs SET body=? WHERE id=?', (raw.decode('utf-8'), document['id']))
        except sqlite3.Error:
            raise RepairJournalError() from None
        self._changed.notify_all()

    def create(self, *, project_id, owner_id, issue_id, request_id, request_digest, reservation_bytes,
               request=None, retain_until_ms=None):
        try:
            for identifier in (project_id, owner_id, issue_id, request_id): contracts.validate_id(identifier)
            contracts.validate_digest(request_digest)
        except contracts.ContractError:
            raise RepairJournalError('repair_request') from None
        _require(type(reservation_bytes) is int and 0 < reservation_bytes <= self.disk_limit, 'repair_disk_limit')
        _require(request is None or type(request) is dict and len(canonical(request)) <= 16 * 1024,
                 'repair_request')
        _require(retain_until_ms is None or type(retain_until_ms) is int and 0 < retain_until_ms < 2 ** 53,
                 'repair_request')
        with self._lock:
            existing = self._db.execute('SELECT body FROM jobs WHERE project_id=? AND owner_id=? AND request_id=?',
                                       (project_id, owner_id, request_id)).fetchone()
            if existing:
                value = json.loads(existing[0])
                _require(value['requestDigest'] == request_digest and value['issueId'] == issue_id,
                         'repair_request_conflict')
                return value
            count, reserved = self._db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_bytes),0) FROM jobs').fetchone()
            _require(count < self.max_jobs and reserved + reservation_bytes <= self.disk_limit, 'repair_disk_limit')
            identifier = 'repair_' + uuid.uuid4().hex
            now = int(time.time() * 1000)
            document = {'schemaVersion': 1, 'id': identifier, 'projectId': project_id, 'ownerId': owner_id,
                'issueId': issue_id, 'requestId': request_id, 'requestDigest': request_digest,
                'status': 'created', 'phase': 'created', 'reason': None, 'createdAtMs': now, 'updatedAtMs': now,
                'cancelRequested': False, 'reservedBytes': reservation_bytes, 'usedBytes': 0,
                'retainUntilMs': retain_until_ms, 'outputsExpired': False,
                'request': request, 'plan': None, 'provider': None, 'attempts': [], 'result': None, 'outputs': {}}
            created = inserted = False
            try:
                os.mkdir(identifier, 0o700, dir_fd=self._directory)
                created = True
                self._db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',
                    (identifier, project_id, owner_id, request_id, request_digest, reservation_bytes,
                     canonical(document).decode('utf-8')))
                inserted = True
                os.fsync(self._directory)
            except (OSError, sqlite3.Error):
                if inserted:
                    document.update(status='quarantined', phase='finished', reason='repair_storage')
                    self._put(document)
                elif created:
                    # This directory was just created and has no job or data.
                    # Never remove a nonempty or preexisting directory here.
                    try:
                        os.rmdir(identifier, dir_fd=self._directory)
                        os.fsync(self._directory)
                    except OSError:
                        # Stop this writer if its unindexed directory cannot
                        # be removed; repeated failed inserts cannot add more.
                        self.close()
                raise RepairJournalError() from None
            return document

    def claim(self, identifier):
        with self._lock:
            document = self.get(identifier)
            if document['status'] != 'created':
                return False
            document.update(status='running', phase='freezing', updatedAtMs=int(time.time() * 1000))
            self._put(document)
            return True

    def get(self, identifier):
        try:
            contracts.validate_id(identifier)
        except contracts.ContractError:
            raise RepairJournalError('repair_not_found') from None
        with self._lock:
            _require(self._db is not None)
            row = self._db.execute('SELECT body FROM jobs WHERE id=?', (identifier,)).fetchone()
            _require(row is not None, 'repair_not_found')
            return json.loads(row[0])

    def list(self, *, project_id=None, owner_id=None):
        with self._lock:
            _require(self._db is not None)
            rows = [json.loads(row[0]) for row in self._db.execute('SELECT body FROM jobs ORDER BY rowid')]
            return [row for row in rows if (project_id is None or row['projectId'] == project_id)
                    and (owner_id is None or row['ownerId'] == owner_id)]

    def update(self, identifier, **changes):
        _require(set(changes) <= {'status', 'phase', 'reason', 'plan', 'provider', 'attempts', 'result'})
        _require('status' not in changes or changes['status'] in {'created', 'running'})
        _require('phase' not in changes or changes['phase'] in _PHASES)
        with self._lock:
            document = self.get(identifier)
            _require(document['status'] not in TERMINAL, 'repair_terminal')
            _require(not document['cancelRequested'], 'repair_cancelled')
            document.update(changes, updatedAtMs=int(time.time() * 1000))
            self._put(document)
            return document

    def finish(self, identifier, status, *, reason=None, result=None, attempts=None):
        _require(status in TERMINAL)
        with self._lock:
            document = self.get(identifier)
            _require(document['status'] not in TERMINAL, 'repair_terminal')
            _require(not document['cancelRequested'] or status in {'cancelled', 'quarantined'}, 'repair_cancelled')
            document.update(status=status, phase='finished', reason=reason, result=result,
                            updatedAtMs=int(time.time() * 1000))
            if attempts is not None:
                document['attempts'] = attempts
            self._put(document)
            return document

    def cancel(self, identifier):
        with self._lock:
            document = self.get(identifier)
            if document['status'] not in TERMINAL:
                document.update(cancelRequested=True, updatedAtMs=int(time.time() * 1000))
                self._put(document)
            return document

    def cancellation(self, identifier):
        self.get(identifier)
        return RepairCancellation(self, identifier)

    def write_blobs(self, identifier, name, blobs):
        try:
            contracts.validate_id(name)
        except contracts.ContractError:
            raise RepairJournalError() from None
        _require(type(blobs) is BlobSet)
        size = sum(len(data) for _, data in blobs.entries)
        with self._lock:
            document = self.get(identifier)
            _require(document['status'] not in TERMINAL and not document['cancelRequested'], 'repair_terminal')
            _require(name not in document['outputs'] and document['usedBytes'] + size <= document['reservedBytes'],
                     'repair_disk_limit')
            document['usedBytes'] += size
            document['outputs'][name] = {'digest': blobs.digest, 'bytes': size, 'status': 'reserved',
                                          'paths': [path for path, _ in blobs.entries]}
            self._put(document)
            try:
                blobs.write_new(self.root / identifier / name)
                directory = open_directory(self.root / identifier / name)
                try:
                    for path, _ in blobs.entries:
                        parts = path.split('/')
                        current = os.dup(directory)
                        try:
                            for part in parts[:-1]:
                                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                                os.fsync(current); os.close(current); current = child
                            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
                            try: os.fsync(fd)
                            finally: os.close(fd)
                            os.fsync(current)
                        finally: os.close(current)
                    os.fsync(directory)
                finally: os.close(directory)
                job_directory = open_directory(self.root / identifier)
                try: os.fsync(job_directory)
                finally: os.close(job_directory)
            except (ArtifactError, OSError):
                document.update(status='quarantined', phase='finished', reason='output_publication_failed')
                self._put(document)
                raise RepairJournalError() from None
            document['outputs'][name]['status'] = 'complete'
            self._put(document)
        return self.root / identifier / name

    @contextmanager
    def begin_snapshot_output(self, identifier, name, *, reservation_bytes):
        """Reserve before mutation and pin the fresh output's parent through exit.

        Caller must include content, manifest and its bounded publication overhead
        in the reservation, and pass parent_identity to the snapshot writer.
        All outputs of a snapshot job are retained, including uncertain residue.
        """
        try:
            contracts.validate_id(name)
        except contracts.ContractError:
            raise RepairJournalError('repair_request') from None
        _require(type(reservation_bytes) is int and 0 < reservation_bytes <= 512 * 1024 ** 2,
                 'repair_disk_limit')
        context = None
        root_fd = parent_fd = None
        registered = False
        try:
            with self._lock:
                document = self.get(identifier)
                _require(document['status'] not in TERMINAL and not document['cancelRequested'], 'repair_terminal')
                _require(name not in document['outputs']
                         and document['usedBytes'] + reservation_bytes <= document['reservedBytes'],
                         'repair_disk_limit')
                root_fd = os.dup(self._directory)
                parent_fd = os.open(identifier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=root_fd)
                context = _SnapshotOutput(self, identifier, name, root_fd, parent_fd)
                context.current()
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise RepairJournalError('repair_output_exists')
                document['usedBytes'] += reservation_bytes
                document['outputs'][name] = {'kind': 'source-snapshot-v1', 'status': 'reserved',
                    'bytes': reservation_bytes, 'reference': None, 'cleanupBlocked': True}
                document['snapshotOutputsRetained'] = True
                self._put(document)
                registered = True
            yield context
            with self._lock:
                context.current()
                context._check_sidecar()
                context._check_candidate()
                document = self.get(identifier)
                _require(context.completed and not document['cancelRequested']
                         and document['status'] not in TERMINAL, 'repair_terminal')
        except BaseException:
            if registered:
                with self._lock:
                    if self._db is not None:
                        document = self.get(identifier)
                        for output in document['outputs'].values():
                            output.update(status='cleanup-blocked', cleanupBlocked=True)
                        self._put(document)
            raise
        finally:
            if context is not None:
                context.closed = True
                if context.candidate_fd is not None:
                    os.close(context.candidate_fd)
                for fd in reversed(context.sidecar_fds):
                    os.close(fd)
            if parent_fd is not None:
                os.close(parent_fd)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _discard_output(parent, name, paths):
        """Remove only declared private files, with every directory pinned."""
        tree = {}
        for path in paths:
            safe_transfer_path(path)
            parts = path.split('/')
            node = tree
            for part in parts[:-1]: node = node.setdefault(part, {})
            node[parts[-1]] = None
        def remove(fd, expected):
            actual = set(os.listdir(fd))
            _require(actual <= set(expected))
            for part in sorted(actual):
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                _require(info.st_uid == os.getuid())
                if expected[part] is None:
                    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                    os.unlink(part, dir_fd=fd)
                else:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try: remove(child, expected[part])
                    finally: os.close(child)
                    os.rmdir(part, dir_fd=fd)
            os.fsync(fd)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return
        try: remove(fd, tree)
        finally: os.close(fd)
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)

    def apply_retention(self, *, now_ms):
        _require(type(now_ms) is int and now_ms >= 0)
        with self._lock:
            for document in self.list():
                expiry = document.get('retainUntilMs')
                if expiry is None or expiry > now_ms:
                    continue
                if document['status'] not in TERMINAL:
                    self.cancel(document['id'])
                    continue
                document['outputsExpired'] = True
                if document.get('snapshotOutputsRetained') or any(
                        output.get('kind') == 'source-snapshot-v1' for output in document['outputs'].values()):
                    # No process-lifetime ownership proof survives publication or
                    # restart. Never run legacy deletion on this job or siblings.
                    for output in document['outputs'].values():
                        if output['status'] != 'expired':
                            output.update(status='cleanup-blocked', cleanupBlocked=True)
                    self._put(document)
                    continue
                self._put(document)
                directory = os.open(document['id'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=self._directory)
                try:
                    for name, output in document['outputs'].items():
                        if output['status'] == 'expired': continue
                        try:
                            contracts.validate_id(name)
                            _require(type(output.get('paths')) is list)
                            self._discard_output(directory, name, output['paths'])
                            output['status'] = 'expired'
                            document['usedBytes'] -= output['bytes']
                        except (OSError, RepairJournalError, contracts.ContractError):
                            output['status'] = 'cleanup-blocked'
                        self._put(document)
                finally:
                    os.close(directory)
                if document['usedBytes'] == 0 and all(item['status'] == 'expired' for item in document['outputs'].values()):
                    # Only confirmed deletion releases the data reservation.
                    # Request tombstones and the bounded job index remain.
                    document['reservedBytes'] = 0
                    self._put(document, release_reservation=True)

    def expire(self, identifier, *, now_ms):
        _require(type(now_ms) is int and now_ms > 0)
        with self._lock:
            document = self.get(identifier)
            previous = document.get('retainUntilMs')
            document['retainUntilMs'] = min(previous, now_ms) if previous is not None else now_ms
            self._put(document)
            self.apply_retention(now_ms=now_ms)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close(); self._db = None
            if self._fd is not None:
                os.close(self._fd); self._fd = None
            if self._directory is not None:
                os.close(self._directory); self._directory = None
            self._changed.notify_all()


__all__ = ['RepairJournal', 'RepairJournalError', 'RepairCancellation', 'TERMINAL']
