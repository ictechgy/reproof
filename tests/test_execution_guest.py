"""Protocol service tests use a declared executor double, never host candidates."""
import hashlib
import os
import socket
import threading
import time
import unittest
from unittest import mock

from reproof.execution import guest, wire
from reproof.execution.artifacts import BlobSet, receive_blobs, send_blobs
from tests.test_execution_resources import catalog


class ExecutionGuestTests(unittest.TestCase):
    def test_host_cannot_run_a_guest_recipe_by_setting_an_environment_flag(self):
        executor = guest.GuestExecutor(uid=501, gid=20)
        with mock.patch.dict(os.environ, {"REPROOF_GUEST": "1"}):
            with self.assertRaises(guest.GuestError):
                executor.execute(BlobSet((("input", b"data"),)), catalog()[0], threading.Event())

    def service(self, executor):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        host = wire.Channel(left, key=b"k" * 32, run_id="service", role="host", deadline=time.monotonic() + 3)
        peer = wire.Channel(right, key=b"k" * 32, run_id="service", role="guest", deadline=time.monotonic() + 3)
        thread = threading.Thread(target=guest.serve_one, args=(peer,),
                                  kwargs={"catalog": catalog(), "agent_digest": "a" * 64,
                                          "executor": executor})
        thread.start()
        self.addCleanup(thread.join, 4)
        self.assertEqual(host.receive()[0], "ready")
        return host, thread

    def test_only_registered_recipe_receives_sealed_source_and_result_is_supplemental(self):
        calls = []
        class ExecutorDouble:
            def execute(self, blobs, recipe, cancel):
                calls.append((blobs.digest, recipe["id"]))
                return ({"exitCode": 0, "outputTruncated": False,
                         "logDigest": hashlib.sha256(b"").hexdigest()},
                        BlobSet((("product.bin", b"artifact"),)))
        host, thread = self.service(ExecutorDouble())
        source = BlobSet((("src/product.py", b"source"),))
        send_blobs(host, source, prefix="input")
        host.send("run", {"recipeId": "build", "inputDigest": source.digest})
        artifact = receive_blobs(host, prefix="artifact", allowed_paths=["product.bin"])
        result = host.receive()
        self.assertEqual(artifact.entries, (("product.bin", b"artifact"),))
        self.assertEqual(result[0], "result")
        self.assertNotIn("verified", result[1])
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(source.digest, "build")])

    def test_unknown_recipe_never_dispatches(self):
        class ExecutorDouble:
            def execute(self, *_):
                raise AssertionError("unregistered recipe dispatched")
        host, thread = self.service(ExecutorDouble())
        source = BlobSet((("src/product.py", b"source"),))
        send_blobs(host, source, prefix="input")
        host.send("run", {"recipeId": "candidate-hook", "inputDigest": source.digest})
        self.assertEqual(host.receive(), ("error", {"code": "recipe-rejected"}))
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_disconnect_cancels_the_running_guest_service(self):
        observed = threading.Event()
        running = threading.Event()
        class ExecutorDouble:
            def execute(self, blobs, recipe, cancel):
                running.set()
                if cancel.wait(2):
                    observed.set()
                raise guest.GuestError("cancelled")
        host, thread = self.service(ExecutorDouble())
        source = BlobSet((("src/product.py", b"source"),))
        send_blobs(host, source, prefix="input")
        host.send("run", {"recipeId": "build", "inputDigest": source.digest})
        self.assertTrue(running.wait(1))
        host.sock.shutdown(socket.SHUT_RDWR)
        self.assertTrue(observed.wait(2))
        thread.join(3)
        self.assertFalse(thread.is_alive())
