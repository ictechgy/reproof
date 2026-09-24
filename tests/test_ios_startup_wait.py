"""Deterministic coverage for the authenticated iOS helper startup window."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import unittest
from unittest import mock

from reproof.core import ContractError
from reproof.live import providers


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value


class _Process:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class _Event:
    def __init__(self, clock, *, ready_at=None, on_wait=None):
        self.clock = clock
        self.ready_at = ready_at
        self.on_wait = on_wait
        self.waits = []
        self.set_called = False

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait(self)
        self.clock.value += max(0.0, timeout)
        return self.ready_at is not None and self.clock.value >= self.ready_at

    def set(self):
        self.set_called = True


class _Stop:
    def __init__(self, clock):
        self.clock = clock
        self.set_called = False

    def wait(self, timeout):
        self.clock.value += timeout
        return False

    def set(self):
        self.set_called = True


class _Lab:
    def __init__(self):
        self.failed = []

    def _session(self, sid):
        return {"state": "connecting"}

    def fail(self, sid, message):
        self.failed.append((sid, message))


def _provider(clock, *, event, process=None, check_permit=None):
    value = providers.IosProvider("owned-simulator", Path("/tmp/products"), "bundle.example")
    value.native_handshake = object()
    value.process = process or _Process()
    value._check_permit = check_permit or (lambda permit: None)
    value.start = lambda session, lab, permit=None: None
    return value


@contextmanager
def _fake_start_clock(clock, event):
    with mock.patch.object(providers.time, "monotonic", clock.monotonic), \
         mock.patch.object(providers.threading, "Event", return_value=event):
        yield


class IOSStartupWaitTests(unittest.TestCase):
    def test_handshake_after_old_fifteen_second_window_is_accepted_before_bound(self):
        clock = _Clock()
        event = _Event(clock, ready_at=20.0)
        provider = _provider(clock, event=event)
        with _fake_start_clock(clock, event):
            result = provider.start_authorized({}, None, object())
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(clock.value, 20.0)
        self.assertLessEqual(clock.value, 90.0)
        self.assertGreater(len(event.waits), 1)
        self.assertTrue(all(0 < value <= 1.0 for value in event.waits))

    def test_expired_original_permit_aborts_polling_without_renewal(self):
        clock = _Clock()
        event = _Event(clock, ready_at=20.0)
        checks = []

        def check(permit):
            checks.append(clock.value)
            if len(checks) >= 3:
                raise ContractError("original permit expired")

        provider = _provider(clock, event=event, check_permit=check)
        with self.assertRaises(ContractError), _fake_start_clock(clock, event):
            provider.start_authorized({}, None, object())
        self.assertGreaterEqual(len(checks), 3)
        self.assertFalse(provider._startup_accepting)
        self.assertTrue(provider.stop.is_set())
        self.assertLess(clock.value, 20.0)

    def test_cancellation_and_process_exit_fail_before_waiting_to_deadline(self):
        for mode in ("cancelled", "exited"):
            with self.subTest(mode=mode):
                clock = _Clock()
                if mode == "cancelled":
                    event = _Event(clock, on_wait=lambda _: provider.stop.set())
                    process = _Process()
                else:
                    event = _Event(clock)
                    process = _Process(exit_code=65)
                provider = _provider(clock, event=event, process=process)
                if mode == "cancelled":
                    # The callback closes over the instance after construction.
                    event.on_wait = lambda _: provider.stop.set()
                with self.assertRaises((ContractError, providers.LiveError)), \
                     _fake_start_clock(clock, event):
                    provider.start_authorized({}, None, object())
                self.assertFalse(provider._startup_accepting)
                self.assertLess(clock.value, 1.0)
                self.assertLessEqual(len(event.waits), 1)

    def test_startup_deadline_is_bounded_and_closes_late_handshake_gate(self):
        clock = _Clock()
        event = _Event(clock)
        provider = _provider(clock, event=event)
        with mock.patch.object(providers, "IOS_STARTUP_TIMEOUT_SECONDS", 3.0), \
             _fake_start_clock(clock, event), \
             self.assertRaises((ContractError, providers.LiveError)):
            provider.start_authorized({}, None, object())
        self.assertGreaterEqual(clock.value, 3.0)
        self.assertLess(clock.value, 4.0)
        self.assertFalse(provider._startup_accepting)
        with self.assertRaises((ContractError, providers.LiveError)):
            provider._require_startup_window()

    def test_monitor_uses_the_waiters_absolute_deadline(self):
        clock = _Clock()
        provider = _provider(clock, event=_Event(clock))
        provider.stop = _Stop(clock)
        provider.process = _Process()
        provider._startup_accepting = True
        provider._startup_deadline = 3.0
        lab = _Lab()
        provider.sid = "ios-startup"
        with mock.patch.object(providers.time, "monotonic", clock.monotonic):
            provider._monitor = providers.IosProvider._monitor.__get__(provider)
            provider.lab = lab
            provider._monitor()
        self.assertEqual(clock.value, 3.0)
        self.assertEqual(lab.failed, [("ios-startup", "Native driver startup timed out")])
        self.assertTrue(provider.stop.set_called)
        self.assertFalse(provider._startup_accepting)

    def test_successful_event_without_native_handshake_is_rejected(self):
        clock = _Clock()
        event = _Event(clock, ready_at=0.0)
        provider = _provider(clock, event=event)
        provider.native_handshake = None
        with self.assertRaises((ContractError, providers.LiveError)), _fake_start_clock(clock, event):
            provider.start_authorized({}, None, object())
        self.assertFalse(provider._startup_accepting)


if __name__ == "__main__":
    unittest.main()
