"""Thread and journal boundaries for the protected iOS callback bridge."""
from copy import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from reproof.ios_mobile_callbacks import IOSNativeCallbackCoordinator
from reproof.ios_mobile_operation import IOSMobileOperationError
from reproof.repair_callbacks import invoke_fixed
from tests.test_ios_mobile_native import IOSMobileNativeTests


class IOSMobileCallbackCoordinatorTests(IOSMobileNativeTests):
    def test_validation_failure_can_enter_cleanup_before_any_replay(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator=IOSNativeCallbackCoordinator(owner)
            with coordinator.callback(operation,'install'):pass
            coordinator.request_cleanup(operation)
            with coordinator.callback(operation,'cleanup') as token:
                token.require()
            with self.assertRaises(IOSMobileOperationError):
                with coordinator.callback(operation,'replay',1):pass

    def _worker(self, coordinator, operation, phase, iteration=0, body=None):
        entered = threading.Event()
        release = threading.Event()
        result = []

        def run():
            try:
                with coordinator.callback(operation, phase, iteration) as token:
                    token.require()
                    if body is not None:
                        body(token)
                    entered.set()
                result.append(("ok", threading.current_thread()))
            except BaseException as error:
                result.append(("error", error))

        worker = threading.Thread(target=run, name="ios-callback-test")
        worker.start()
        return worker, entered, release, result

    def test_fixed_sequence_uses_original_owner_and_retained_descriptors(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            workers = []
            phases = [("install", 0), ("replay", 1), ("replay", 2),
                      ("replay", 3), ("cleanup", 0)]
            for phase, iteration in phases:
                entered = threading.Event()
                result = []

                def run(phase=phase, iteration=iteration):
                    try:
                        with coordinator.callback(operation, phase, iteration) as token:
                            token.require()
                            if phase != "cleanup":
                                with owner.borrow_descriptors() as borrowed:
                                    owner.require_descriptors(borrowed)
                            entered.set()
                        result.append(("ok", threading.current_thread()))
                    except BaseException as error:
                        result.append(("error", error))

                worker = threading.Thread(target=run, name="ios-callback-sequence")
                worker.start(); worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertTrue(entered.is_set())
                self.assertEqual(result[0][0], "ok", result)
                workers.append(result[0][1])

            self.assertEqual(len({id(worker) for worker in workers}), 5)
            self.assertEqual(owner._thread_object, threading.current_thread())
            owner._check()
            phase_root = self.root / "phases"
            self.assertEqual(
                {path.name for path in phase_root.iterdir()},
                {"install", "replay-001", "replay-002", "replay-003", "cleanup"},
            )
            for path in phase_root.iterdir():
                intent = json.loads((path / "intent.json").read_bytes())
                state = json.loads((path / "state.json").read_bytes())
                self.assertEqual(state["state"], "completed")
                self.assertEqual(intent["operationId"], operation.context.operation_id)
                self.assertEqual(intent["contextDigest"], operation.context.digest)
                self.assertEqual(intent["nativeBindingDigest"], owner.binding_digest)
                self.assertEqual(intent["ownershipGeneration"], self.device.generation)
                self.assertEqual(intent["sequence"], state["sequence"])

    def test_invoke_fixed_workers_can_each_use_the_same_live_coordinator(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            workers = []
            for phase, iteration in (
                ("install", 0), ("replay", 1), ("replay", 2),
                ("replay", 3), ("cleanup", 0),
            ):
                def callback(*args, phase=phase, iteration=iteration, **kwargs):
                    with coordinator.callback(operation, phase, iteration):
                        workers.append(threading.current_thread())
                    return None

                value, returned = invoke_fixed(
                    callback,
                    cancellation=threading.Event(),
                    deadline_monotonic=time.monotonic() + 3,
                )
                self.assertTrue(returned)
                self.assertIsNone(value)
            self.assertEqual(len({id(worker) for worker in workers}), 5)
            owner._check()

    def test_owner_rejects_worker_before_and_after_scope_but_accepts_inside(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            outcomes = []

            def worker():
                for position in ("before", "after"):
                    try:
                        owner._check()
                    except IOSMobileOperationError:
                        outcomes.append(position)
                try:
                    with coordinator.callback(operation, "install") as token:
                        token.require()
                        owner._check()
                        outcomes.append("inside")
                except BaseException as error:
                    outcomes.append(type(error).__name__)
                try:
                    owner._check()
                except IOSMobileOperationError:
                    outcomes.append("after")

            worker_thread = threading.Thread(target=worker)
            worker_thread.start(); worker_thread.join(3)
            self.assertFalse(worker_thread.is_alive())
            self.assertEqual(outcomes, ["before", "after", "inside", "after"])
            owner._check()

    def test_concurrent_duplicate_reordered_and_copied_capabilities_are_rejected(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            entered = threading.Event(); release = threading.Event(); result = []

            def first():
                try:
                    with coordinator.callback(operation, "install") as token:
                        entered.set(); release.wait(3)
                    result.append("ok")
                except BaseException as error:
                    result.append(type(error).__name__)

            worker = threading.Thread(target=first); worker.start()
            self.assertTrue(entered.wait(3))
            with self.assertRaises(IOSMobileOperationError):
                with coordinator.callback(operation, "install"):
                    pass
            with self.assertRaises(IOSMobileOperationError):
                with coordinator.callback(operation, "replay", 1):
                    pass
            with self.assertRaises(IOSMobileOperationError):
                copy(coordinator)
            release.set(); worker.join(3)
            self.assertEqual(result, ["ok"])

            token_copy_result = []

            def copied_token():
                try:
                    with coordinator.callback(operation, "replay", 1) as token:
                        try:
                            copy(token)
                        except IOSMobileOperationError:
                            token_copy_result.append("copy")
                        token_copy_result.append("live")
                except BaseException as error:
                    token_copy_result.append(type(error).__name__)

            copied = threading.Thread(target=copied_token); copied.start(); copied.join(3)
            self.assertEqual(token_copy_result, ["copy", "live"])

    def test_export_or_active_child_must_be_idle_before_transfer(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            with owner.borrow_descriptors():
                worker, entered, release, result = self._worker(
                    coordinator, operation, "install")
                worker.join(3)
                self.assertEqual(result[0][0], "error")
                self.assertFalse(entered.is_set())

            class ActiveChild:
                active_processes = 1

            child = ActiveChild()
            child.native_owner = owner
            child.close = lambda **kwargs: None
            self.c.operations._native_clients.add(child)
            try:
                worker, entered, release, result = self._worker(
                    coordinator, operation, "install")
                worker.join(3)
                self.assertEqual(result[0][0], "error")
                self.assertFalse(entered.is_set())
            finally:
                self.c.operations._native_clients.discard(child)

    def test_revocation_wrong_operation_and_cleanup_retry_remain_conservative(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            wrong = object()
            worker, entered, release, result = self._worker(
                coordinator, operation, "install", body=lambda token: token.fail())
            worker.join(3)
            self.assertEqual(result[0][0], "ok")
            with self.assertRaises(IOSMobileOperationError):
                with coordinator.callback(wrong, "cleanup"):
                    pass

            worker, entered, release, result = self._worker(
                coordinator, operation, "cleanup", body=lambda token: token.fail())
            worker.join(3)
            self.assertEqual(result[0][0], "ok")
            worker, entered, release, result = self._worker(
                coordinator, operation, "cleanup")
            worker.join(3)
            self.assertEqual(result[0][0], "ok")
            self.assertFalse(coordinator.uncertain)

    def test_escaped_child_fences_native_owner_after_callback(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)

            class ActiveChild:
                native_owner = owner
                active_processes = 1
                def close(self, **kwargs): return False

            child = ActiveChild()
            try:
                worker, entered, _, result = self._worker(coordinator, operation, 'install',
                    body=lambda token: self.c.operations._native_clients.add(child))
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result[0][0], 'error')
                self.assertTrue(coordinator.uncertain)
                with self.assertRaises(IOSMobileOperationError): owner._check()
                with self.assertRaises(IOSMobileOperationError):
                    with owner.borrow_descriptors(): pass
                with self.assertRaises(IOSMobileOperationError):
                    with coordinator.callback(operation, 'cleanup'): pass
                self.assertFalse(owner._active)
            finally:
                self.c.operations._native_clients.discard(child)

    def test_failed_callback_journal_fences_native_owner(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            with patch.object(IOSNativeCallbackCoordinator, '_journal_exit', side_effect=OSError):
                worker, _, _, result = self._worker(coordinator, operation, 'install')
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0][0], 'error')
            self.assertTrue(coordinator.uncertain)
            self.assertFalse(owner._active)
            with self.assertRaises(IOSMobileOperationError): owner._check()
            with self.assertRaises(IOSMobileOperationError):
                with owner.borrow_descriptors(): pass


    def test_revocation_rejects_a_new_callback(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            self.device.revoke_dispatches()
            worker, entered, release, result = self._worker(
                coordinator, operation, "install")
            worker.join(3)
            self.assertEqual(result[0][0], "error")
            self.assertFalse(entered.is_set())

    def test_store_close_is_busy_until_callback_worker_leaves(self):
        with self.admitted() as operation, self.owner(operation) as owner:
            coordinator = IOSNativeCallbackCoordinator(owner)
            entered = threading.Event(); release = threading.Event(); result = []

            def worker():
                try:
                    with coordinator.callback(operation, "install"):
                        entered.set(); release.wait(3)
                    result.append("ok")
                except BaseException as error:
                    result.append(type(error).__name__)

            callback_thread = threading.Thread(target=worker)
            callback_thread.start()
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.c.operations.close(deadline_monotonic=time.monotonic() + .05))
            release.set(); callback_thread.join(3)
            self.assertFalse(callback_thread.is_alive())
            self.assertEqual(len(self.c.operations._callbacks), 0)


if __name__ == "__main__":
    unittest.main()
