"""Durable, single-VM admission. Unknown shutdown never releases an overlay."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import threading
import uuid

from reproof.core import ContractError
from reproof.contracts.versions import bounded_int, digest, exact, require, validate_digest, validate_id
from reproof.storage import Lease
from .artifacts import ArtifactError, open_directory, read_regular
from .wire import ProtocolError, canonical, decode_json

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MAX_RUNS = 512
OWNED_OVERLAY_FILES = frozenset({"disk.img", "auxiliary.bin", "termination.json"})


class RunDenied(RuntimeError):
    pass


def _directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise RunDenied("Private execution directory required")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RunStore:
    def __init__(self, root, *, environment_digest, disk_limit, create=True):
        validate_digest(environment_digest)
        bounded_int(disk_limit, "execution disk budget", 1, 512 * 1024 ** 3)
        require(type(create) is bool, "Invalid execution journal open mode")
        self.root = Path(root).absolute()
        self.environment_digest = environment_digest
        self.disk_limit = disk_limit
        self._mutex = threading.RLock()
        self._machine_ownership = threading.local()
        try:
            if not create:
                for path in (self.root, self.root / "runs", self.root / "cancellations"):
                    descriptor = open_directory(path)
                    try:
                        info = os.fstat(descriptor)
                        if info.st_mode & 0o077 or info.st_uid != os.getuid():
                            raise RunDenied("Private execution directory required")
                    finally:
                        os.close(descriptor)
                # Older journals have no archive directory; it appears on the
                # first archival under the control lock, never on open.
                try:
                    os.lstat(self.root / "archive")
                except FileNotFoundError:
                    pass
                else:
                    descriptor = open_directory(self.root / "archive")
                    try:
                        info = os.fstat(descriptor)
                        if info.st_mode & 0o077 or info.st_uid != os.getuid():
                            raise RunDenied("Private execution directory required")
                    finally:
                        os.close(descriptor)
                self._load()
                return
            _directory(self.root)
            _directory(self.root / "runs")
            _directory(self.root / "cancellations")
            _directory(self.root / "archive")
            with self._control():
                if not (self.root / "state.json").exists():
                    self._write({"schemaVersion": 1, "environmentDigest": environment_digest, "runs": {}})
                self._load()
        except (OSError, ArtifactError, ProtocolError, ContractError):
            raise RunDenied("Execution journal unavailable") from None

    @contextmanager
    def machine_lease(self, machine_digest, *, kind='vm'):
        """Bind a machine identity to one canonical journal across directories.

        The kernel lease prevents overlapping executors. Its durable authority
        marker prevents a new journal root from bypassing old uncertain work.
        ``kind``은 회수 도메인이다 — 'host' scope는 VM 종료 기록 복구가
        구조적으로 거절된다.
        """
        require(kind in ('vm', 'host'), 'Invalid machine scope kind')
        validate_digest(machine_digest)
        authority_root = digest({"stateRoot": str(self.root.resolve()),
                                 "environmentDigest": self.environment_digest})
        lease_name = ('protected-macos-vm-' if kind == 'vm'
                      else 'protected-host-build-') + machine_digest
        try:
            with Lease(lease_name, authority_root=authority_root) as lease:
                self._bind_scope(kind, machine_digest)
                lease.mark_authority("shared")
                self._machine_ownership.active = (os.getpid(), machine_digest, lease)
                try:
                    yield
                finally:
                    del self._machine_ownership.active
        except (ContractError, OSError):
            raise RunDenied("Machine ownership unavailable") from None

    @contextmanager
    def repair_scope_lease(self, kind, scope_digest):
        """Keep signing/device uncertainty bound to its canonical local journal.

        The registered provider supplies the stable physical scope identity.
        This lease does not replace its native device ownership or sanitation.
        Mobile journals must never use VM termination-record recovery.
        """
        require(kind in ('signing', 'mobile-device'), 'Invalid repair scope')
        validate_digest(scope_digest)
        authority_root = digest({'stateRoot': str(self.root.resolve()),
                                 'environmentDigest': self.environment_digest})
        try:
            with Lease('protected-repair-' + kind + '-' + scope_digest,
                       authority_root=authority_root) as lease:
                self._bind_scope(kind, scope_digest)
                lease.mark_authority('shared')
                yield
        except (ContractError, OSError):
            raise RunDenied('Repair scope ownership unavailable') from None

    def _bind_scope(self, kind, scope_digest):
        """Persist the recovery domain before effects can be admitted.

        A different device, signer or VM cannot reinterpret this journal after
        restart. Historical uncertain runs without a domain remain unknown;
        merely selecting a new configuration cannot migrate their ownership.
        """
        expected = {'kind': kind, 'scopeDigest': scope_digest}
        with self._control():
            value = self._load()
            previous = value.get('scope')
            if previous is None:
                if any(row['state'] not in TERMINAL for row in value['runs'].values()):
                    raise RunDenied('Unscoped uncertain work requires explicit recovery')
                value['scope'] = expected
                self._write(value)
            elif previous != expected:
                raise RunDenied('Execution journal belongs to another physical scope')

    @contextmanager
    def _control(self):
        with self._mutex:
            fd = os.open(self.root / ".control-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _load(self):
        try:
            value = decode_json(read_regular(self.root, "state.json", maximum=256 * 1024))
            exact(value, ("schemaVersion", "environmentDigest", "runs"), ('scope', 'archive'))
            require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
                    and value["environmentDigest"] == self.environment_digest, "Journal mismatch")
            require(type(value["runs"]) is dict and len(value["runs"]) <= MAX_RUNS, "Journal limit")
            if 'scope' in value:
                exact(value['scope'], ('kind', 'scopeDigest'))
                require(type(value['scope']['kind']) is str
                        and value['scope']['kind'] in {'vm', 'host', 'signing', 'mobile-device'},
                        'Journal scope')
                validate_digest(value['scope']['scopeDigest'])
            for operation_id, record in value["runs"].items():
                validate_id(operation_id)
                exact(record, ("requestDigest", "state", "reservedBytes"))
                validate_digest(record["requestDigest"])
                require(record["state"] in TERMINAL | {"admitted", "quarantined"}, "Journal state")
                bounded_int(record["reservedBytes"], "reserved bytes", 0, 512 * 1024 ** 3)
                require(record["state"] not in TERMINAL or record["reservedBytes"] == 0,
                        "Journal accounting mismatch")
            archived = value.get('archive')
            if archived is None:
                official = 0
            else:
                exact(archived, ('count', 'digest'))
                bounded_int(archived['count'], 'archived records', 1, 2 ** 31 - 1)
                validate_digest(archived['digest'])
                official = archived['count']
            names = self._archive_names()
            pending = names & set(value['runs'])
            # A committed archive file whose record still sits in runs is a
            # crash orphan; every other unaccounted name is foreign. Removal of
            # a committed record lowers the count and is refused here.
            require(len(names) - len(pending) == official
                    and all(value['runs'][name]['state'] in TERMINAL for name in pending),
                    'Journal archive mismatch')
            return value
        except (ArtifactError, ProtocolError, ContractError, TypeError):
            raise RunDenied("Execution journal rejected") from None

    def _write(self, value):
        raw = canonical(value)
        if len(raw) > 256 * 1024:
            raise RunDenied("Execution journal limit exceeded")
        temporary = self.root / (".state-" + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / "state.json")
            _sync_directory(self.root)
        except OSError:
            raise RunDenied("Execution journal write failed") from None

    def _archive_root(self, *, create=False):
        """Open the terminal-record archive; absent means nothing was moved yet."""
        path = self.root / "archive"
        if create:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            except OSError:
                raise RunDenied("Execution archive unavailable") from None
        else:
            try:
                os.lstat(path)
            except FileNotFoundError:
                return None
            except OSError:
                raise RunDenied("Execution archive unavailable") from None
        try:
            descriptor = open_directory(path)
        except ArtifactError:
            raise RunDenied("Execution archive unavailable") from None
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(descriptor)
            raise RunDenied("Private execution archive required")
        return descriptor

    def _archive_names(self):
        """Operation ids with a committed archive record; dotfiles are internal."""
        descriptor = self._archive_root()
        if descriptor is None:
            return frozenset()
        try:
            names = set()
            for name in os.listdir(descriptor):
                if name.startswith('.'):
                    continue
                validate_id(name)
                names.add(name)
            return frozenset(names)
        finally:
            os.close(descriptor)

    @staticmethod
    def _archive_document(descriptor, operation_id):
        """Read one committed terminal record; reject anything but exact bytes."""
        try:
            fd = os.open(operation_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=descriptor)
        except FileNotFoundError:
            raise
        except OSError:
            raise RunDenied("Execution archive unavailable") from None
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size > 4096):
                raise RunDenied("Execution archive rejected")
            raw = stream.read(4097)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise RunDenied("Execution archive changed")
            try:
                document = decode_json(raw)
                exact(document, ("schemaVersion", "operationId", "requestDigest",
                                 "state", "reservedBytes"))
                require(document["schemaVersion"] == 1
                        and document["operationId"] == operation_id
                        and document["state"] in TERMINAL
                        and document["reservedBytes"] == 0, "Invalid archived record")
                validate_digest(document["requestDigest"])
            except (ContractError, ProtocolError, TypeError):
                raise RunDenied("Execution archive rejected") from None
            return document

    def _archived_record(self, operation_id):
        """Committed terminal record, or None when no archive entry exists."""
        descriptor = self._archive_root()
        if descriptor is None:
            return None
        try:
            try:
                return self._archive_document(descriptor, operation_id)
            except FileNotFoundError:
                return None
        finally:
            os.close(descriptor)

    def _is_archived(self, operation_id):
        """Whether any archive entry permanently claims this operation identity."""
        descriptor = self._archive_root()
        if descriptor is None:
            return False
        try:
            try:
                os.stat(operation_id, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError:
                raise RunDenied("Execution archive unavailable") from None
            return True
        finally:
            os.close(descriptor)

    def _archivable_ids(self, runs):
        """Terminal, zero-reservation records with no remaining run directory."""
        eligible = []
        try:
            descriptor = open_directory(self.root / "runs")
        except ArtifactError:
            raise RunDenied("Execution journal unavailable") from None
        try:
            for operation_id, record in runs.items():
                if record["state"] not in TERMINAL or record["reservedBytes"] != 0:
                    continue
                try:
                    os.stat(operation_id, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    eligible.append(operation_id)
                except OSError:
                    continue
        finally:
            os.close(descriptor)
        return eligible

    def _archive_terminal(self, value):
        """Move clean terminal records to durable per-run archive files.

        The caller holds _control and ``value`` is its loaded state. Every
        record commits to archive/<id> before state.json drops it, so a crash
        leaves an adoptable orphan, never a lost identity. Existing entries
        must replay the exact recorded set; unknown or altered content refuses.
        """
        eligible = self._archivable_ids(value["runs"])
        if not eligible:
            return
        descriptor = self._archive_root(create=True)
        try:
            records = {}
            for name in os.listdir(descriptor):
                if name.startswith("."):
                    continue
                try:
                    validate_id(name)
                except ContractError:
                    raise RunDenied("Execution archive rejected") from None
                try:
                    records[name] = self._archive_document(descriptor, name)
                except FileNotFoundError:
                    raise RunDenied("Execution archive changed during merge") from None
            previous = value.get("archive")
            official = {name: document for name, document in records.items()
                        if name not in value["runs"]}
            expected = digest([official[name] for name in sorted(official)])
            if ((previous is None) != (not official)
                    or previous is not None and (previous["count"] != len(official)
                                                 or previous["digest"] != expected)):
                raise RunDenied("Execution archive mismatch")
            for name, document in records.items():
                record = value["runs"].get(name)
                if record is not None and (name not in eligible
                        or document["requestDigest"] != record["requestDigest"]
                        or document["state"] != record["state"]):
                    raise RunDenied("Execution archive mismatch")
            for operation_id in eligible:
                if operation_id in records:
                    continue
                record = value["runs"][operation_id]
                document = {"schemaVersion": 1, "operationId": operation_id,
                            "requestDigest": record["requestDigest"],
                            "state": record["state"], "reservedBytes": 0}
                temporary = "." + uuid.uuid4().hex
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(canonical(document))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, operation_id,
                               src_dir_fd=descriptor, dst_dir_fd=descriptor)
                except OSError:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except OSError:
                        pass
                    raise RunDenied("Execution archive write failed") from None
                records[operation_id] = document
            os.fsync(descriptor)
            merged = [records[name] for name in sorted(records)]
            value["archive"] = {"count": len(merged), "digest": digest(merged)}
            for operation_id in eligible:
                del value["runs"][operation_id]
            self._write(value)
        finally:
            os.close(descriptor)

    def status(self, operation_id):
        validate_id(operation_id)
        value = self._load()["runs"].get(operation_id)
        archived = self._archived_record(operation_id)
        if value is None:
            if archived is None:
                raise RunDenied("Unknown execution operation")
            return {"requestDigest": archived["requestDigest"], "state": archived["state"],
                    "reservedBytes": archived["reservedBytes"]}
        if archived is not None and (value["state"] not in TERMINAL
                or archived["requestDigest"] != value["requestDigest"]
                or archived["state"] != value["state"]):
            raise RunDenied("Execution archive mismatch")
        return dict(value)

    def require_available(self):
        """Read-only preflight; admission still rechecks under the kernel lock."""
        with self._control():
            runs = self._load()['runs']
            if any(item['state'] not in TERMINAL for item in runs.values()):
                raise RunDenied('Execution scope is busy or quarantined')
            if (len(runs) >= MAX_RUNS
                    and len(runs) - len(self._archivable_ids(runs)) >= MAX_RUNS):
                raise RunDenied('Execution scope is busy or quarantined')

    def require_scope_available(self, kind, scope_digest):
        """Read-only availability hint; real admission still takes its lease.

        A UI poll must not contend with the operation it is observing. Check
        durable quarantine and canonical-root metadata without claiming the
        execution lock or writing its cutover marker.
        """
        require(kind in {'vm', 'host', 'signing', 'mobile-device'}, 'Invalid execution scope')
        validate_digest(scope_digest)
        self.require_available()
        with self._control():
            scope = self._load().get('scope')
            if scope is not None and scope != {'kind': kind, 'scopeDigest': scope_digest}:
                raise RunDenied('Execution journal belongs to another physical scope')
        authority_root = digest({'stateRoot': str(self.root.resolve()), 'environmentDigest': self.environment_digest})
        name = ('protected-macos-vm-' if kind == 'vm'
                else 'protected-host-build-' if kind == 'host'
                else 'protected-repair-'+kind+'-') + scope_digest
        try:
            Lease(name, authority_root=authority_root).check_authority()
        except (ContractError, OSError):
            raise RunDenied('Execution scope authority is unavailable') from None

    def cancel(self, operation_id, request_digest):
        validate_id(operation_id)
        validate_digest(request_digest)
        with self._control():
            record = self.status(operation_id)
            if record["requestDigest"] != request_digest:
                raise RunDenied("Cancellation binding mismatch")
            if record["state"] in TERMINAL:
                return
            path = self.root / "cancellations" / operation_id
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                if read_regular(path.parent, path.name, maximum=64) != request_digest.encode():
                    raise RunDenied("Cancellation record rejected") from None
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(request_digest.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_directory(path.parent)

    def reconcile(self, operation_id, request_digest):
        """Recover only from the native owner's private, durable stop record.

        The calling thread must hold this store's machine_lease. A candidate has no directory share
        or file descriptor to this record; peer JSON and bare status flags are
        never accepted as recovery inputs.
        """
        validate_id(operation_id)
        validate_digest(request_digest)
        fd = os.open(self.root / ".vm-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunDenied("Execution environment busy") from None
            with self._control():
                record = self.status(operation_id)
                if record["requestDigest"] != request_digest:
                    raise RunDenied("Recovery binding mismatch")
                # A terminal record has no remaining reservation to release,
                # including historical journals without a recovery domain.
                if record["state"] in TERMINAL:
                    return record
                scope = self._load().get('scope')
                if scope is None or scope['kind'] != 'vm':
                    raise RunDenied('VM termination cannot recover this execution scope')
                owned = getattr(self._machine_ownership, 'active', None)
                if (owned is None or owned[0] != os.getpid() or owned[1] != scope['scopeDigest']
                        or owned[2].file is None or owned[2].file.closed):
                    raise RunDenied('VM recovery requires current machine ownership')
                proof = decode_json(read_regular(self.root / "runs" / operation_id, "termination.json", maximum=1024))
                exact(proof, ("schemaVersion", "operationId", "requestDigest", "state"))
                require(type(proof["schemaVersion"]) is int and proof["schemaVersion"] == 1
                        and proof["operationId"] == operation_id and proof["requestDigest"] == request_digest
                        and proof["state"] in ("stopped", "not-started"), "Native termination proof missing")
            run = OwnedRun(self, operation_id, request_digest)
            run.finish("failed", stopped=True)
            return self.status(operation_id)
        except (OSError, ContractError, ArtifactError, ProtocolError):
            raise RunDenied("Native termination recovery unavailable") from None
        finally:
            os.close(fd)

    def finish_mobile_recovery(self, capability, *, authority):
        """Consume a live cleanup capability after exact Android staging disposal.

        Implementation lives in journal_recovery so the generic journal
        carries no platform-specific recovery code or imports.
        """
        from . import journal_recovery
        return journal_recovery.finish_mobile_recovery(self, capability, authority=authority)

    def finish_ios_native_recovery(self, capability, *, authority):
        """Consume measured native recovery and exact staged-file disposal."""
        from . import journal_recovery
        return journal_recovery.finish_ios_native_recovery(self, capability, authority=authority)

    def finish_ios_preparation_recovery(self, capability, *, authority):
        """Release only freshly disposed preparation files, never device ownership."""
        from . import journal_recovery
        return journal_recovery.finish_ios_preparation_recovery(self, capability, authority=authority)

    def consume_ios_native_disposal(self, native_owner, cleanup_token, sanitation, evidence_digest,
                                    *, cancellation, deadline_monotonic):
        """Remove the iOS file hold; run accounting and device release remain separate."""
        from . import journal_recovery
        return journal_recovery.consume_ios_native_disposal(
            self, native_owner, cleanup_token, sanitation, evidence_digest,
            cancellation=cancellation, deadline_monotonic=deadline_monotonic)

    def finish_signing_recovery(self, capability, *, authority):
        """Consume a live native signing cleanup capability under its locks.

        Recovery can only fail or cancel an interrupted operation. The signing
        owner has already proved all native phases stopped and removed their
        exact private files; no VM termination record is accepted here.
        """
        from . import journal_recovery
        return journal_recovery.finish_signing_recovery(self, capability, authority=authority)

    @contextmanager
    def admit(self, operation_id, request_digest, *, disk_bytes):
        validate_id(operation_id)
        validate_digest(request_digest)
        bounded_int(disk_bytes, "overlay reservation", 1, 512 * 1024 ** 3)
        fd = os.open(self.root / ".vm-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        run = None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunDenied("Execution environment busy") from None
            with self._control():
                value = self._load()
                if any(item["state"] == "admitted" for item in value["runs"].values()):
                    for item in value["runs"].values():
                        if item["state"] == "admitted":
                            item["state"] = "quarantined"
                    self._write(value)
                if operation_id in value["runs"] or self._is_archived(operation_id):
                    raise RunDenied("Execution admission refused")
                if len(value["runs"]) >= MAX_RUNS:
                    self._archive_terminal(value)
                if (len(value["runs"]) >= MAX_RUNS
                        or any(item["state"] == "quarantined" for item in value["runs"].values())):
                    raise RunDenied("Execution admission refused")
                reserved = sum(item["reservedBytes"] for item in value["runs"].values())
                free = os.statvfs(self.root).f_bavail * os.statvfs(self.root).f_frsize
                if reserved + disk_bytes > self.disk_limit or disk_bytes > free:
                    raise RunDenied("Execution disk budget exhausted")
                value["runs"][operation_id] = {"requestDigest": request_digest,
                                               "state": "admitted", "reservedBytes": disk_bytes}
                self._write(value)
                run = OwnedRun(self, operation_id, request_digest)
                run.directory.mkdir(mode=0o700, exist_ok=False)
                _sync_directory(run.directory.parent)
                # admit이 만든 inode를 기록한다 — 같은 UID의 후보가 run 디렉터리나
                # 그 상위를 symlink로 교체해도 정리 경로가 다른 inode를 지우지 않는다.
                info = run.directory.stat()
                run.directory_identity = (info.st_dev, info.st_ino)
            yield run
        finally:
            try:
                if run is not None and not run.finished:
                    run.finish("failed", stopped=False)
            finally:
                os.close(fd)


class OwnedRun:
    def __init__(self, store, operation_id, request_digest):
        self.store, self.operation_id, self.request_digest = store, operation_id, request_digest
        self.directory = store.root / "runs" / operation_id
        self.finished = False

    def cancelled(self):
        path = self.store.root / "cancellations" / self.operation_id
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        # The cancel API checks binding before publication. Any record, including
        # an unreadable or malformed one, is a reason to stop this unique run ID.
        return True

    def is_set(self):
        """Cancellation interface for the byte channel, including another process."""
        return self.cancelled()

    def finish(self, outcome, *, stopped):
        if self.finished:
            raise RunDenied("Execution already finalized")
        if outcome not in TERMINAL:
            raise RunDenied("Invalid execution outcome")
        with self.store._control():
            value = self.store._load()
            record = value["runs"][self.operation_id]
            cleaned = False
            if stopped is True:
                parent = directory = None
                try:
                    # fd-기준 정리: run 디렉터리(또는 runs/ 상위)가 symlink로
                    # 교체된 경우 admit 시점 inode와 달라 정리를 거절한다 —
                    # 경로 기반 unlink/rmdir은 대상 디렉터리를 지울 수 있다.
                    parent = open_directory(self.store.root / "runs")
                    directory = os.open(self.operation_id,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    info = os.fstat(directory)
                    expected = getattr(self, "directory_identity", None)
                    if (info.st_uid != os.getuid() or info.st_mode & 0o077
                            or (expected is not None and (info.st_dev, info.st_ino) != expected)):
                        raise OSError()
                    names = os.listdir(directory)
                    if not set(names) <= OWNED_OVERLAY_FILES:
                        raise OSError()
                    for name in names:
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            raise OSError()
                    for name in names:
                        os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                    os.rmdir(self.operation_id, dir_fd=parent)
                    os.fsync(parent)
                    cleaned = True
                except (OSError, ArtifactError):
                    pass
                finally:
                    if directory is not None:
                        os.close(directory)
                    if parent is not None:
                        os.close(parent)
            record["state"] = (("cancelled" if self.cancelled() else outcome)
                               if cleaned else "quarantined")
            if cleaned:
                record["reservedBytes"] = 0
            self.store._write(value)
            self.finished = True
