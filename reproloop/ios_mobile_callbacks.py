"""Bounded, process-local callback ownership for protected iOS mobile work.

The native owner is created on the caller's thread, while
``repair_callbacks.invoke_fixed`` invokes each trusted adapter phase on a new
worker thread.  ``IOSNativeCallbackCoordinator`` is the capability that
bridges that narrow boundary.  It retains the original owner and its open
descriptors; it never reconstructs authority from the phase JSON records.

This module deliberately does not load or call callback names from documents.
The trusted adapter enters :meth:`IOSNativeCallbackCoordinator.callback` and
performs its already-issued native work inside the yielded token scope.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import threading
import weakref

from . import contracts
from .ios_mobile_native import IOSMobileNativeOwner
from .ios_mobile_operation import IOSMobileOperationError, _require
from .repair_android_operation import (
    _open_child_directory,
    _read_json_at,
    _replace_at,
    _write_new_at,
)


_PHASES = frozenset(("install", "replay", "cleanup"))
_PHASE_DIRECTORY = "phases"
_MAX_REPLAYS = 3

# A coordinator is an in-process capability.  Keeping the live issuer here
# prevents a shallow copy of a coordinator from becoming a second owner of the
# same native owner.  Weak keys ensure a completed native owner is not kept
# alive by this registry.
_LIVE_COORDINATORS = weakref.WeakKeyDictionary()


def _live_coordinator(owner):
    reference = _LIVE_COORDINATORS.get(owner)
    return reference() if reference is not None else None


def _reject():
    raise IOSMobileOperationError() from None


def _digest_or_none(value):
    if value is None:
        return None
    try:
        contracts.validate_digest(value)
    except (contracts.ContractError, TypeError, ValueError):
        _reject()
    return value


class IOSNativeCallbackToken:
    """Ephemeral proof that one coordinator phase owns the current thread."""

    __slots__ = (
        "_coordinator",
        "_operation",
        "_phase",
        "_iteration",
        "_sequence",
        "_attempt",
        "_issuer",
        "_outcome",
    )

    def __init__(self, coordinator, operation, phase, iteration, sequence, attempt):
        self._coordinator = coordinator
        self._operation = operation
        self._phase = phase
        self._iteration = iteration
        self._sequence = sequence
        self._attempt = attempt
        self._issuer = coordinator._token_issuer
        self._outcome = None

    def __repr__(self):
        return "<IOSNativeCallbackToken>"

    def __copy__(self):
        _reject()

    def __deepcopy__(self, memo):
        _reject()

    @property
    def operation(self):
        self.require()
        return self._operation

    @property
    def owner(self):
        self.require()
        return self._coordinator.owner

    @property
    def phase(self):
        self.require()
        return self._phase

    @property
    def iteration(self):
        self.require()
        return self._iteration

    @property
    def sequence(self):
        self.require()
        return self._sequence

    @property
    def attempt(self):
        self.require()
        return self._attempt

    def require(self):
        """Re-check the token and current owner before a native operation."""
        self._coordinator._require_token(self)
        return self

    def complete(self, outcome_digest=None):
        """Mark the phase successful; the context manager journals it on exit."""
        self._coordinator._set_outcome(self, True, outcome_digest)
        return self

    def fail(self, outcome_digest=None):
        """Mark the phase failed while retaining cleanup as the next phase."""
        self._coordinator._set_outcome(self, False, outcome_digest)
        return self


class IOSNativeCallbackCoordinator:
    """Serialize the fixed install/replay/cleanup callback sequence.

    A coordinator is issued only while ``owner`` is live on its creator
    thread.  It is intentionally not serializable and does not trust any
    digest or phase record as a substitute for the live owner capability.
    """

    __slots__ = (
        "_owner",
        "_store",
        "_operation",
        "_record",
        "_context_digest",
        "_creator_pid",
        "_creator_thread_id",
        "_creator_thread",
        "_issuer",
        "_token_issuer",
        "_active_token",
        "_stage",
        "_next_replay",
        "_cleanup_failed",
        "_failed_phase",
        "_early_cleanup",
        "_done",
        "_uncertain",
        "_closed",
        "__weakref__",
    )

    def __init__(self, owner):
        try:
            _require(type(owner) is IOSMobileNativeOwner)
            creator = threading.current_thread()
            creator_id = threading.get_ident()
            store = owner.operations
            operation = owner.operation
            with store._changed:
                # Construction itself must happen on the original owner
                # thread, before any invoke_fixed worker is admitted.
                owner._check()
                _require(owner._thread == creator_id
                    and owner._thread_object is creator
                    and owner._pid == os.getpid()
                    and owner.operations._native_owners.get(id(owner)) is owner)
                _require(operation.store is store)
                existing = _live_coordinator(owner)
                _require(existing is None)
                record = json.loads(owner._record)
                _require(type(record) is dict and record.get("bindingDigest") == owner.binding_digest)
                self._owner = owner
                self._store = store
                self._operation = operation
                self._record = owner._record
                self._context_digest = operation.context.digest
                self._creator_pid = os.getpid()
                self._creator_thread_id = creator_id
                self._creator_thread = creator
                self._issuer = object()
                self._token_issuer = object()
                self._active_token = None
                self._stage = "install"
                self._next_replay = 1
                self._cleanup_failed = False
                self._failed_phase = None
                self._early_cleanup = False
                self._done = False
                self._uncertain = False
                self._closed = False
                _LIVE_COORDINATORS[owner] = weakref.ref(self)
        except IOSMobileOperationError:
            raise
        except (BaseException,):
            raise IOSMobileOperationError() from None

    def __repr__(self):
        return "<IOSNativeCallbackCoordinator>"

    def __copy__(self):
        _reject()

    def __deepcopy__(self, memo):
        _reject()

    @property
    def owner(self):
        self._require_capability()
        return self._owner

    @property
    def operation(self):
        self._require_capability()
        return self._operation

    @property
    def store(self):
        self._require_capability()
        return self._store

    @property
    def uncertain(self):
        # This property is diagnostic only; it never grants a callback.
        with self._store._changed:
            self._require_capability(active=False)
            return self._uncertain

    def request_cleanup(self, operation):
        """End replay admission after an enclosing validation/cancellation failure."""
        with self._store._changed:
            self._require_capability()
            _require(operation is self._operation and not self._done and not self._uncertain
                and self._active_token is None and self._resources_idle_locked()
                and self._stage in ('replay','cleanup'))
            if self._stage == 'replay':
                self._require_history('replay',self._next_replay)
                self._early_cleanup=True
                self._stage='cleanup'

    def close(self):
        """Revoke an unused coordinator capability."""
        try:
            with self._store._changed:
                self._require_capability(active=False)
                _require(self._active_token is None)
                self._closed = True
                if _live_coordinator(self._owner) is self:
                    del _LIVE_COORDINATORS[self._owner]
                self._store._changed.notify_all()
        except IOSMobileOperationError:
            raise
        except (BaseException,):
            raise IOSMobileOperationError() from None

    def _require_capability(self, *, active=True):
        _require(type(self) is IOSNativeCallbackCoordinator
            and self._issuer is not None
            and not self._closed
            and _live_coordinator(self._owner) is self
            and self._owner.operations is self._store
            and self._owner.operation is self._operation
            and self._owner._record == self._record
            and self._owner._pid == os.getpid()
            and (not active or self._owner._active)
            and self._store._native_owners.get(id(self._owner)) is self._owner
            and self._operation.store is self._store)

    def _require_creator_bound(self):
        owner = self._owner
        _require(owner._pid == self._creator_pid == os.getpid()
            and owner._thread == self._creator_thread_id
            and owner._thread_object is self._creator_thread)

    def _require_token(self, token):
        try:
            with self._store._changed:
                self._require_capability()
                _require(type(token) is IOSNativeCallbackToken
                    and token._coordinator is self
                    and token._issuer is self._token_issuer
                    and self._active_token is token
                    and token._operation is self._operation
                    and self._owner._thread == threading.get_ident()
                    and self._owner._thread_object is threading.current_thread())
                self._owner._check()
                return token
        except IOSMobileOperationError:
            raise
        except (BaseException,):
            raise IOSMobileOperationError() from None

    def _set_outcome(self, token, success, outcome_digest):
        with self._store._changed:
            self._require_token(token)
            _require(token._outcome is None)
            token._outcome = (bool(success), _digest_or_none(outcome_digest))

    @staticmethod
    def _phase_name(phase, iteration):
        if phase == "install":
            return "install"
        if phase == "replay":
            return f"replay-{iteration:03d}"
        return "cleanup"

    @staticmethod
    def _phase_sequence(phase, iteration):
        if phase == "install":
            return 1
        if phase == "replay":
            return 1 + iteration
        return 5

    def _require_phase_request(self, operation, phase, iteration):
        _require(operation is self._operation)
        _require(type(phase) is str and phase in _PHASES)
        _require(type(iteration) is int and not isinstance(iteration, bool) and 0 <= iteration <= _MAX_REPLAYS)
        _require(not self._done and self._active_token is None and not self._uncertain)
        if phase == "install":
            _require(self._stage == "install" and iteration == 0)
        elif phase == "replay":
            _require(self._stage == "replay" and iteration == self._next_replay
                and 1 <= iteration <= _MAX_REPLAYS)
        else:
            # A failed install/replay, or all three successful replays, is the
            # only route to cleanup.  A failed cleanup is retriable only after
            # its failed state has been durably recorded and the resource
            # boundary is idle again.  Cleanup remains one fixed journal slot.
            _require(self._stage == "cleanup" and iteration == 0)

    def _resources_idle_locked(self):
        owner = self._owner
        store = self._store
        if any(getattr(item, "_owner", None) is owner and getattr(item, "_active", False)
               for item in tuple(store._native_exports.values())):
            return False
        command_lock = getattr(owner, "_command_lock", None)
        if command_lock is not None and command_lock.locked():
            return False
        for client in tuple(store._native_clients):
            if getattr(client, "native_owner", None) is not owner:
                continue
            try:
                active = client.active_processes
            except BaseException:
                return False
            if type(active) is not int or active < 0 or active != 0:
                return False
        return True

    def _open_phase(self, name):
        owner = self._owner
        phases = phase = None
        try:
            try:
                os.mkdir(_PHASE_DIRECTORY, mode=0o700, dir_fd=owner._directory)
                os.fsync(owner._directory)
            except FileExistsError:
                pass
            phases = _open_child_directory(owner._directory, _PHASE_DIRECTORY)
            try:
                os.mkdir(name, mode=0o700, dir_fd=phases)
                os.fsync(phases)
            except FileExistsError:
                pass
            phase = _open_child_directory(phases, name)
            return phases, phase
        except BaseException:
            if phase is not None:
                os.close(phase)
            if phases is not None:
                os.close(phases)
            raise

    def _open_existing_phase(self, name):
        phases = phase = None
        try:
            phases = _open_child_directory(self._owner._directory, _PHASE_DIRECTORY)
            phase = _open_child_directory(phases, name)
            return phases, phase
        except BaseException:
            if phase is not None:
                os.close(phase)
            if phases is not None:
                os.close(phases)
            raise

    def _base_record(self, phase, iteration, sequence, attempt):
        binding = json.loads(self._record)
        return {
            "schemaVersion": 1,
            "operationId": self._operation.context.operation_id,
            "requestDigest": self._operation.context.request_digest,
            "contextDigest": self._context_digest,
            "configurationDigest": self._store.configuration_digest,
            "scopeDigest": self._store.definition.scope_digest,
            "nativeBindingDigest": binding["bindingDigest"],
            "ownershipGeneration": binding["ownershipGeneration"],
            "hostIncarnation": binding["hostIncarnation"],
            "helperIncarnation": binding["helperIncarnation"],
            "authorityRootDigest": binding["authorityRootDigest"],
            "phase": phase,
            "iteration": iteration,
            "sequence": sequence,
            "attempt": attempt,
            "state": "running",
            "outcomeDigest": None,
        }

    @staticmethod
    def _same_phase_record(actual, expected):
        immutable = (
            "schemaVersion", "operationId", "requestDigest", "contextDigest",
            "configurationDigest", "scopeDigest", "nativeBindingDigest",
            "ownershipGeneration", "hostIncarnation", "helperIncarnation",
            "authorityRootDigest", "phase", "iteration", "sequence",
        )
        return (type(actual) is dict
            and all(actual.get(name) == expected[name] for name in immutable)
            and set(actual) == set(expected))

    def _journal_enter(self, phase, iteration, sequence):
        name = self._phase_name(phase, iteration)
        phases = phase_fd = None
        try:
            phases, phase_fd = self._open_phase(name)
            expected = self._base_record(phase, iteration, sequence, 0)
            try:
                intent = _read_json_at(phase_fd, "intent.json")
                state = _read_json_at(phase_fd, "state.json")
            except Exception:
                intent = state = None
            if intent is None or state is None:
                _write_new_at(phase_fd, "intent.json", expected)
                _write_new_at(phase_fd, "state.json", expected)
                return name, 0
            _require(self._same_phase_record(intent, expected)
                and self._same_phase_record(state, expected))
            # Only a durably failed cleanup can be retried.  Install/replay
            # phases are single-use, and a completed cleanup is terminal.
            _require(phase == "cleanup" and state["state"] == "failed"
                and intent["state"] == "running")
            attempt = state["attempt"] + 1
            _require(type(attempt) is int and 0 < attempt < 2 ** 31)
            record = dict(expected, attempt=attempt)
            _replace_at(phase_fd, "state.json", record)
            return name, attempt
        finally:
            if phase_fd is not None:
                os.close(phase_fd)
            if phases is not None:
                os.close(phases)

    def _require_history(self, phase, iteration):
        """Check the already completed/failed phase slots from the live FD."""
        prior = []
        if phase == "replay":
            prior.append(("install", 0))
            prior.extend(("replay", number) for number in range(1, iteration))
        elif phase == "cleanup":
            if self._early_cleanup:
                prior.append(('install',0))
                prior.extend(('replay',number) for number in range(1,self._next_replay))
            elif self._failed_phase is None:
                prior.extend(("replay", number) for number in range(1, _MAX_REPLAYS + 1))
            else:
                prior.append(self._failed_phase)
        if not prior:
            return
        for prior_phase, prior_iteration in prior:
            name = self._phase_name(prior_phase, prior_iteration)
            phases = phase_fd = None
            try:
                phases, phase_fd = self._open_existing_phase(name)
                intent = _read_json_at(phase_fd, "intent.json")
                state = _read_json_at(phase_fd, "state.json")
                expected = self._base_record(
                    prior_phase, prior_iteration,
                    self._phase_sequence(prior_phase, prior_iteration), 0)
                _require(self._same_phase_record(intent, expected)
                    and self._same_phase_record(state, expected)
                    and intent["state"] == "running"
                    and state["state"] == ("failed" if (prior_phase, prior_iteration) == self._failed_phase
                                             else "completed"))
            finally:
                if phase_fd is not None:
                    os.close(phase_fd)
                if phases is not None:
                    os.close(phases)

    def _journal_exit(self, token, success, outcome_digest):
        phases = phase_fd = None
        try:
            name = self._phase_name(token._phase, token._iteration)
            phases, phase_fd = self._open_phase(name)
            state = _read_json_at(phase_fd, "state.json")
            expected = self._base_record(token._phase, token._iteration, token._sequence, token._attempt)
            _require(self._same_phase_record(state, expected)
                and state["state"] == "running" and state["attempt"] == token._attempt)
            state["state"] = "completed" if success else "failed"
            state["outcomeDigest"] = _digest_or_none(outcome_digest)
            _replace_at(phase_fd, "state.json", state)
        finally:
            if phase_fd is not None:
                os.close(phase_fd)
            if phases is not None:
                os.close(phases)

    def _enter(self, operation, phase, iteration):
        try:
            with self._store._changed:
                self._require_capability()
                self._require_phase_request(operation, phase, iteration)
                self._require_creator_bound()
                owner = self._owner
                # The operation/store checks are deliberately performed before
                # changing the owner thread.  The full device and binding
                # checks run immediately after transfer below.
                _require(owner._active and owner.operations._native_owners.get(id(owner)) is owner)
                self._store._require_operation(self._operation)
                _require(self._resources_idle_locked())

                current = threading.current_thread()
                current_id = threading.get_ident()
                owner._thread, owner._thread_object = current_id, current
                try:
                    # This is where expiry, revocation, inode identity, the
                    # original binding and the exact device generation are
                    # rechecked.  A callback never gets a soft, digest-only
                    # transfer of the owner.
                    owner._check()
                    sequence = self._phase_sequence(phase, iteration)
                    self._require_history(phase, iteration)
                    name, attempt = self._journal_enter(phase, iteration, sequence)
                except BaseException:
                    owner._thread, owner._thread_object = self._creator_thread_id, self._creator_thread
                    raise
                token = IOSNativeCallbackToken(self, self._operation, phase, iteration, sequence, attempt)
                self._active_token = token
                self._store._callbacks.add(token)
                self._store._changed.notify_all()
                return token
        except IOSMobileOperationError:
            raise
        except (BaseException,):
            raise IOSMobileOperationError() from None

    def _leave(self, token, success, outcome_digest):
        journal_error = None
        final_success = False
        try:
            with self._store._changed:
                self._require_capability()
                _require(self._active_token is token)
                owner = self._owner
                current = threading.current_thread()
                current_id = threading.get_ident()
                exact_thread = owner._thread == current_id and owner._thread_object is current
                idle = self._resources_idle_locked()
                owner_valid = False
                if success and exact_thread:
                    try:
                        owner._check()
                        owner_valid = True
                    except BaseException:
                        owner_valid = False
                final_success = bool(success and exact_thread and idle and owner_valid)
                # A trusted adapter may return an explicit, settled phase
                # failure and then run cleanup.  That is known failure, not an
                # ownership uncertainty.  Uncertainty is retained when the
                # thread fence or resource boundary itself is no longer
                # trustworthy (or when a successful callback lost owner
                # validation).
                if not exact_thread or not idle or (success and not owner_valid):
                    self._uncertain = True
                try:
                    self._journal_exit(token, final_success, outcome_digest)
                except BaseException as error:
                    journal_error = error
                    self._uncertain = True
                    final_success = False

                if final_success:
                    if token._phase == "install":
                        self._stage = "replay"
                    elif token._phase == "replay":
                        if token._iteration == _MAX_REPLAYS:
                            self._stage = "cleanup"
                        else:
                            self._next_replay = token._iteration + 1
                    else:
                        self._stage = "done"
                        self._done = True
                        self._cleanup_failed = False
                        self._failed_phase = None
                else:
                    self._stage = "cleanup"
                    self._cleanup_failed = token._phase == "cleanup"
                    if token._phase != "cleanup":
                        self._failed_phase = (token._phase, token._iteration)

                # An uncertain resource boundary or journal fences every
                # client of this owner, including direct creator-thread use.
                # Returning thread ownership cannot grant fresh dispatches.
                if self._uncertain:
                    owner._active = False
                owner._thread, owner._thread_object = self._creator_thread_id, self._creator_thread
                self._active_token = None
                self._store._callbacks.discard(token)
                self._store._changed.notify_all()
        except (BaseException,):
            # Best effort fencing if an unexpected failure occurred while
            # leaving the scope.  The store callback is still removed so close
            # cannot wait forever on a completed Python context.
            with self._store._changed:
                self._uncertain = True
                self._owner._active = False
                self._owner._thread, self._owner._thread_object = self._creator_thread_id, self._creator_thread
                self._active_token = None
                self._store._callbacks.discard(token)
                self._store._changed.notify_all()
            raise IOSMobileOperationError() from None
        if journal_error is not None or (success and not final_success):
            raise IOSMobileOperationError() from None

    @contextmanager
    def callback(self, operation, phase, iteration=0):
        """Enter one trusted phase on the calling ``invoke_fixed`` thread."""
        try:
            token = self._enter(operation, phase, iteration)
        except IOSMobileOperationError:
            raise
        except (BaseException,):
            raise IOSMobileOperationError() from None
        try:
            yield token
        except BaseException:
            try:
                self._leave(token, False, None)
            except BaseException:
                pass
            raise
        else:
            outcome = token._outcome
            success = True if outcome is None else outcome[0]
            digest = None if outcome is None else outcome[1]
            self._leave(token, success, digest)


def require_cleanup_callback(owner):
    coordinator=_live_coordinator(owner)
    _require(coordinator is not None)
    token=coordinator._active_token
    _require(type(token) is IOSNativeCallbackToken and token.phase=='cleanup')
    token.require()
    _require(token.owner is owner)
    return token


__all__ = ["IOSNativeCallbackCoordinator", "IOSNativeCallbackToken", "require_cleanup_callback"]
