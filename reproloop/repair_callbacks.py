"""Bounded dispatch of fixed local supervisor adapters, never imported code."""
from __future__ import annotations

import threading
import time


class CallbackCancellation:
    def __init__(self, parent):
        self.parent = parent
        self.stopped = threading.Event()

    def is_set(self):
        return self.stopped.is_set() or self.parent.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            self.stopped.wait(.01 if deadline is None else max(0, min(.01, deadline-time.monotonic())))
        return self.is_set()


class RunCancellation(CallbackCancellation):
    def __init__(self, parent, run):
        super().__init__(parent)
        self.run = run

    def is_set(self):
        if not super().is_set() and not self.run.cancelled():
            return False
        if not self.run.finished:
            self.run.store.cancel(self.run.operation_id, self.run.request_digest)
        return True


def invoke_fixed(callback, *args, cancellation, deadline_monotonic):
    """Return (value, termination known); a timeout never implies termination.

    Callers must quarantine the durable scope when termination is unknown.
    Late callback results have no publication path. Cleanup callbacks receive
    a fresh cancellation event so user cancellation cannot suppress cleanup.
    """
    stopped = CallbackCancellation(cancellation)
    done = threading.Event(); values = []
    def run():
        try:
            values.append(callback(*args, cancellation=stopped,
                                   deadline_monotonic=deadline_monotonic))
        except Exception:
            pass
        finally:
            done.set()
    worker = threading.Thread(target=run, name='repro-fixed-repair-adapter', daemon=True)
    worker.start()
    deadline = deadline_monotonic
    while not done.is_set() and time.monotonic() < deadline:
        if stopped.is_set():
            deadline = min(deadline, time.monotonic() + .25)
        done.wait(max(0, min(.01, deadline-time.monotonic())))
    if not done.is_set():
        stopped.stopped.set()
        return None, False
    return (values[0], True) if len(values) == 1 else (None, False)
