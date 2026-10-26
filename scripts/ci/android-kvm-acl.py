#!/usr/bin/env python3
"""Narrow, explicitly authorized ephemeral-runner /dev/kvm ACL transaction.

Importable only: caller must enforce exact approved one-shot workflow guards.
No privilege change is attempted by import or tests. Never expands the ACL mask.
"""
from __future__ import annotations
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess

DEVICE = "/dev/kvm"
MAX_ACL = 16384
STATE = "kvm-acl-state.json"

class ACLStop(RuntimeError):
    """Fixed safe code, never subprocess output."""

DIAGNOSTIC_CODES = frozenset({
    "acl_command_canceled", "acl_command_completion_uncertain", "acl_command_failed",
    "acl_command_missing", "acl_privileged_command_nonzero", "acl_privileged_completion_uncertain",
    "acl_concurrent_change", "acl_grant_already_attempted", "acl_grant_failed_restore_uncertain",
    "acl_grant_failed_restored", "acl_grant_unconfirmed", "acl_incomplete", "acl_invalid",
    "acl_mask_cannot_grant_rw", "acl_mask_missing", "acl_mode_mismatch", "acl_restore_unconfirmed",
    "acl_state_current_mismatch", "acl_state_directory_rejected", "acl_state_rejected",
    "acl_unrelated_effective_change", "kvm_identity_changed", "kvm_identity_changed_after_restore",
    "kvm_kernel_identity_rejected", "kvm_not_character_device", "kvm_parent_rejected",
    "kvm_rw_not_available", "kvm_special_mode_rejected", "runner_is_device_owner", "runner_uid_rejected",
})
TRANSACTION_STATUSES = frozenset({"prepared", "inflight", "pending", "granted", "restored", "mutation_uncertain"})

def diagnostic(error):
    """Only fixed reviewed codes; never serialize exception text or arguments."""
    if (isinstance(error, ACLStop) and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in DIAGNOSTIC_CODES):
        return error.args[0]
    if isinstance(error, FileNotFoundError):
        return "acl_file_missing"
    if isinstance(error, PermissionError):
        return "acl_permission_denied"
    if isinstance(error, json.JSONDecodeError):
        return "acl_state_invalid_json"
    if isinstance(error, OSError):
        return "acl_io_failure"
    return "acl_unclassified_failure"

def transaction_status(directory, backend=None):
    """Read the owner-only validated snapshot, exposing only its state enum."""
    try:
        _, state = read_state(directory, backend or NativeBackend())
        return state["status"] if state["status"] in TRANSACTION_STATUSES else "unavailable"
    except BaseException:
        return "unavailable"

def check(condition, code):
    if not condition:
        raise ACLStop(code)

def permission_bits(value):
    check(re.fullmatch(r"[r-][w-][x-]", value) is not None, "acl_invalid")
    return (4 if value[0] == "r" else 0) | (2 if value[1] == "w" else 0) | (1 if value[2] == "x" else 0)

def parse_acl(text):
    check(type(text) is str and 0 < len(text.encode()) <= MAX_ACL, "acl_invalid")
    result = {}
    for line in text.splitlines():
        if not line:
            continue
        match = re.fullmatch(r"(user|group|mask|other):([0-9]*):([r-][w-][x-])", line)
        check(match is not None, "acl_invalid")
        kind, qualifier, rights = match.groups()
        check(kind in ("user", "group") or qualifier == "", "acl_invalid")
        check(not qualifier or str(int(qualifier)) == qualifier, "acl_invalid")
        key = kind + ":" + qualifier
        check(key not in result, "acl_invalid")
        permission_bits(rights)
        result[key] = rights
    check(all(key in result for key in ("user:", "group:", "other:")), "acl_incomplete")
    check("mask:" in result or set(result) == {"user:", "group:", "other:"}, "acl_mask_missing")
    return result

def encode_acl(acl):
    # Parse again before passing any restore bytes across the privilege boundary.
    text = "".join(f"{key}:{value}\n" for key, value in sorted(acl.items()))
    check(parse_acl(text) == acl, "acl_invalid")
    return text

def effective(acl):
    mask = permission_bits(acl.get("mask:", acl["group:"]))
    return {key: permission_bits(value) if key in ("user:", "other:", "mask:")
            else permission_bits(value) & mask for key, value in acl.items()}

def identity(info):
    check(stat.S_ISCHR(info.st_mode), "kvm_not_character_device")
    check(info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) == 0, "kvm_special_mode_rejected")
    return {key: getattr(info, "st_" + key) for key in ("dev", "ino", "rdev", "uid", "gid", "mode")}

def canonical_identity():
    """Read-only fixed device identity bound to the kernel's KVM misc device."""
    check(Path("/dev").resolve() == Path("/dev") and stat.S_ISDIR(os.lstat("/dev").st_mode),
          "kvm_parent_rejected")
    with open("/sys/class/misc/kvm/dev", "r", encoding="ascii") as stream:
        kernel_device = stream.read(129)
    check(len(kernel_device) <= 128 and re.fullmatch(r"[0-9]+:[0-9]+\n?", kernel_device) is not None,
          "kvm_kernel_identity_rejected")
    major, minor = (int(item) for item in kernel_device.strip().split(":"))
    result = identity(os.lstat(DEVICE))
    check(os.major(result["rdev"]) == major and os.minor(result["rdev"]) == minor,
          "kvm_kernel_identity_rejected")
    return result

def validate_mode(acl, device):
    expected = (permission_bits(acl["user:"]) << 6) | (permission_bits(acl.get("mask:", acl["group:"])) << 3) | permission_bits(acl["other:"])
    check(stat.S_IMODE(device["mode"]) & 0o777 == expected, "acl_mode_mismatch")

def expected_grant(original, uid):
    mask = original.get("mask:", original["group:"])
    check(permission_bits(mask) & 6 == 6, "acl_mask_cannot_grant_rw")
    granted = dict(original)
    granted["mask:"] = mask
    granted[f"user:{uid}"] = "rw-"
    original_effective, granted_effective = effective(original), effective(granted)
    check(all(granted_effective[key] == rights for key, rights in original_effective.items()
              if key != f"user:{uid}"), "acl_unrelated_effective_change")
    return granted

class NativeBackend:
    def uid(self):
        check(os.getuid() == os.geteuid(), "runner_uid_rejected")
        return os.getuid()

    def device_identity(self):
        result = canonical_identity()
        if not hasattr(self, "_expected"):
            self._expected = result
        return result

    def held_identity(self):
        return identity(os.fstat(self._fd)) if getattr(self, "_fd", None) is not None else None

    def close(self):
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    @contextmanager
    def _pinned(self):
        # Keep this verified inode alive across mutation, rollback, and readback.
        if getattr(self, "_fd", None) is None:
            fd = os.open(DEVICE, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                check(identity(os.fstat(fd)) == getattr(self, "_expected", None), "kvm_identity_changed")
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        check(identity(os.fstat(self._fd)) == self._expected, "kvm_identity_changed")
        yield f"/proc/{os.getpid()}/fd/{self._fd}"

    def _run(self, argv, data=None, *, mutation=False):
        if not mutation:
            try:
                result = subprocess.run(argv, input=data, stdin=subprocess.DEVNULL if data is None else None,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
            except FileNotFoundError:
                raise ACLStop("acl_command_missing") from None
            except (OSError, subprocess.SubprocessError):
                raise ACLStop("acl_command_failed") from None
            check(result.returncode == 0 and len(result.stdout) <= MAX_ACL, "acl_command_failed")
            return result.stdout
        # Only the root timeout wrapper may stop setfacl. Python must not kill
        # sudo and race a surviving privileged child against restoration.
        canceled = []
        previous = {}
        process = None
        self._mutation_quiescent = True
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda number, frame: canceled.append(number))
            self._mutation_quiescent = False
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            process.communicate(input=data)  # wrapper bounds child; no Python timeout/kill
            self._mutation_quiescent = type(process.returncode) is int and 0 <= process.returncode < 128
            check(self._mutation_quiescent, "acl_privileged_completion_uncertain")
            check(process.returncode == 0, "acl_privileged_command_nonzero")
            check(not canceled, "acl_command_canceled")
            return b""
        except OSError as error:
            if process is None:
                self._mutation_quiescent = True
            code = "acl_command_missing" if isinstance(error, FileNotFoundError) else "acl_command_failed"
            raise ACLStop(code) from None
        finally:
            # Unexpected exceptions still cannot release the held FD or restore
            # until the privileged command has completed. No automatic retry.
            try:
                if process is not None and process.returncode is None:
                    process.wait()
                    self._mutation_quiescent = type(process.returncode) is int and 0 <= process.returncode < 128
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)

    def read_acl(self):
        with self._pinned() as target:
            try:
                return self._run(["getfacl", "--omit-header", "--numeric", "--no-effective", "--absolute-names", target]).decode("ascii")
            except UnicodeError:
                raise ACLStop("acl_invalid") from None

    def grant(self, uid, added_mask=None):
        check(type(uid) is int and uid > 0, "runner_uid_rejected")
        entry = f"u:{uid}:rw"
        if added_mask is not None:
            permission_bits(added_mask)
            entry += f",m::{added_mask}"
        with self._pinned() as target:
            self._run(["sudo", "-n", "timeout", "--foreground", "--signal=TERM", "--kill-after=1", "10",
                       "setfacl", "--no-mask", "-m", entry, target], mutation=True)

    def restore(self, acl):
        data = encode_acl(acl).encode("ascii")
        with self._pinned() as target:
            self._run(["sudo", "-n", "timeout", "--foreground", "--signal=TERM", "--kill-after=1", "10",
                       "setfacl", "--no-mask", "--set-file=-", target], data, mutation=True)

    def accessible(self):
        return os.access(DEVICE, os.R_OK | os.W_OK, effective_ids=True)


def state_directory(directory, uid):
    directory = Path(directory)
    check(directory.is_absolute() and directory.resolve() == directory and not directory.is_symlink(), "acl_state_directory_rejected")
    info = directory.stat()
    check(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700,
          "acl_state_directory_rejected")
    return directory

def write_state(directory, state, *, initial=False):
    target = directory / STATE
    temporary = directory / (STATE + ".tmp")
    path = target if initial else temporary
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not initial:
            os.replace(temporary, target)
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary.exists() and not initial:
            temporary.unlink()

def read_state(directory, backend):
    uid = backend.uid()
    directory = state_directory(directory, uid)
    path = directory / STATE
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
              and info.st_size <= MAX_ACL * 2, "acl_state_rejected")
        state = json.load(stream)
    check(type(state) is dict and set(state) == {"schema", "uid", "device", "identity", "original", "granted", "status"},
          "acl_state_rejected")
    check(type(state["schema"]) is int and state["schema"] == 1 and type(state["uid"]) is int
          and state["uid"] == uid and uid > 0 and state["device"] == DEVICE
          and state["status"] in ("prepared", "pending", "granted", "restored", "inflight", "mutation_uncertain"), "acl_state_rejected")
    device = state["identity"]
    check(type(device) is dict and set(device) == {"dev", "ino", "rdev", "uid", "gid", "mode"}
          and all(type(value) is int and 0 <= value <= (1 << 64) - 1 for value in device.values())
          and stat.S_ISCHR(device["mode"]) and device["uid"] != uid
          and device["mode"] & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) == 0, "acl_state_rejected")
    check(type(state["original"]) is dict and type(state["granted"]) is dict, "acl_state_rejected")
    parse_acl(encode_acl(state["original"]))
    validate_mode(state["original"], device)
    check(state["granted"] == expected_grant(state["original"], uid), "acl_state_rejected")
    return directory, state

def assert_identity(backend, original):
    check(backend.device_identity() == original, "kvm_identity_changed")

def mutate_with_durable_marker(directory, state, backend, action):
    # Uncatchable parent death must not allow fallback to race a surviving sudo
    # child. Only this originating flow may clear inflight after confirmed reap.
    state["status"] = "inflight"
    write_state(directory, state)
    try:
        action()
    finally:
        quiescent = getattr(backend, "_mutation_quiescent", True) is True
        state["status"] = "pending" if quiescent else "mutation_uncertain"
        write_state(directory, state)
    check(quiescent, "acl_command_completion_uncertain")


def managed_backend(function):
    @wraps(function)
    def wrapped(directory, backend=None):
        owned = backend is None
        backend = backend or NativeBackend()
        try:
            return function(directory, backend)
        finally:
            if owned:
                backend.close()
    return wrapped


@managed_backend
def prepare(directory, backend=None):
    backend = backend or NativeBackend()
    uid = backend.uid()
    check(type(uid) is int and uid > 0, "runner_uid_rejected")
    directory = state_directory(directory, uid)
    device = backend.device_identity()
    check(uid != device["uid"], "runner_is_device_owner")
    original = parse_acl(backend.read_acl())
    validate_mode(original, device)
    granted = expected_grant(original, uid)
    assert_identity(backend, device)
    state = {"schema": 1, "device": DEVICE, "uid": uid, "identity": device,
             "original": original, "granted": granted, "status": "prepared"}
    write_state(directory, state, initial=True)
    return {"prepared": True, "aclChanged": False}

@managed_backend
def restore(directory, backend=None):
    backend = backend or NativeBackend()
    directory, state = read_state(directory, backend)
    check(state["status"] not in ("inflight", "mutation_uncertain"), "acl_command_completion_uncertain")
    held = backend.held_identity() if hasattr(backend, "held_identity") else None
    path_matches = False
    try:
        path_matches = backend.device_identity() == state["identity"]
    except (OSError, ACLStop):
        pass
    if held is not None:
        check(held == state["identity"], "kvm_identity_changed")
    else:
        check(path_matches, "kvm_identity_changed")
    current = parse_acl(backend.read_acl())
    if state["status"] in ("prepared", "restored"):
        check(current == state["original"], "acl_state_current_mismatch")
    check(current in (state["original"], state["granted"]), "acl_concurrent_change")
    if current != state["original"]:
        mutate_with_durable_marker(directory, state, backend, lambda: backend.restore(state["original"]))
    if held is not None:
        check(backend.held_identity() == state["identity"], "kvm_identity_changed")
    else:
        assert_identity(backend, state["identity"])
    check(parse_acl(backend.read_acl()) == state["original"], "acl_restore_unconfirmed")
    state["status"] = "restored"
    write_state(directory, state)
    # Restore the originally held inode even if renamed, but never claim overall
    # success or modify the replacement now occupying the canonical path.
    check(path_matches, "kvm_identity_changed_after_restore")
    assert_identity(backend, state["identity"])
    return {"restored": True, "originalAclVerified": True, "deviceIdentityVerified": True}

@managed_backend
def grant(directory, backend=None):
    backend = backend or NativeBackend()
    directory, state = read_state(directory, backend)
    check(state["status"] == "prepared", "acl_grant_already_attempted")
    assert_identity(backend, state["identity"])
    check(parse_acl(backend.read_acl()) == state["original"], "acl_concurrent_change")
    # Durable rollback intention must exist BEFORE the privileged mutation.
    state["status"] = "pending"
    write_state(directory, state)
    try:
        added_mask = state["granted"]["mask:"] if "mask:" not in state["original"] else None
        mutate_with_durable_marker(directory, state, backend, lambda: backend.grant(state["uid"], added_mask))
        assert_identity(backend, state["identity"])
        actual = parse_acl(backend.read_acl())
        check(actual == state["granted"], "acl_grant_unconfirmed")
        check(backend.accessible(), "kvm_rw_not_available")
        state["status"] = "granted"
        write_state(directory, state)
    except BaseException:
        if getattr(backend, "_mutation_quiescent", True) is not True:
            state["status"] = "mutation_uncertain"
            write_state(directory, state)
            raise ACLStop("acl_command_completion_uncertain") from None
        # Restoration failure remains visible; never report the grant as clean.
        try:
            restore(directory, backend)
        except BaseException:
            raise ACLStop("acl_grant_failed_restore_uncertain") from None
        raise ACLStop("acl_grant_failed_restored") from None
    return {"granted": True, "otherEffectiveRightsUnchanged": True, "groupClassModeUnchanged": True}

@contextmanager
def temporary_grant(directory, backend=None):
    owned = backend is None
    backend = backend or NativeBackend()
    try:
        prepare(directory, backend)
        grant(directory, backend)
        yield
    finally:
        try:
            restore(directory, backend)
        finally:
            if owned:
                backend.close()
