"""Bounded repair jobs, terminal cancellation and conservative restart recovery."""
from __future__ import annotations

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
