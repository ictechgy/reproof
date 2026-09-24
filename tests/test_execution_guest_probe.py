"""The trusted qualification command is separate from candidate input/recipes."""
from pathlib import Path
import socket
import threading
import time
import unittest

from reproof.execution.artifacts import BlobSet, receive_blobs
from reproof.execution.guest import GuestError, serve_one
from reproof.execution.guest_probe import GuestProbe
from reproof.execution.wire import Channel, ProtocolError, validate_message
from tests.test_execution_resources import catalog


class GuestProbeTests(unittest.TestCase):
    def test_host_cannot_prepare_a_privileged_guest_probe(self):
        with self.assertRaises(GuestError):
            GuestProbe().prepare("containment")

    def test_probe_mode_is_closed(self):
        with self.assertRaises(ProtocolError):
            validate_message("host", "probe", {"mode": "custom-host-command"})

    def test_trusted_probe_uses_no_candidate_source_and_signals_started(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        host = Channel(left, key=b"k" * 32, run_id="probe", role="host", deadline=time.monotonic() + 3)
        peer = Channel(right, key=b"k" * 32, run_id="probe", role="guest", deadline=time.monotonic() + 3)
        class ProbeDouble:
            def prepare(self, mode):
                recipe = catalog()[0]
                recipe["outputPaths"] = ["probe.json"]
                return BlobSet((("probe.py", b"fixed trusted probe fixture"),)), recipe
        observed = []
        class ExecutorDouble:
            def execute(self, source, recipe, cancel, *, on_started):
                observed.append(source.entries)
                on_started()
                return ({"exitCode": 0, "outputTruncated": False, "logDigest": "a" * 64},
                        BlobSet((("probe.json", b"{}"),)))
        thread = threading.Thread(target=serve_one, args=(peer,), kwargs={"catalog": catalog(),
            "agent_digest": "a" * 64, "executor": ExecutorDouble(), "probe": ProbeDouble()})
        thread.start()
        self.addCleanup(thread.join, 4)
        self.assertEqual(host.receive()[0], "ready")
        host.send("probe", {"mode": "containment"})
        self.assertEqual(host.receive(), ("probe-started", {"mode": "containment"}))
        self.assertEqual(receive_blobs(host, prefix="artifact").entries, (("probe.json", b"{}"),))
        self.assertEqual(host.receive()[0], "result")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [(("probe.py", b"fixed trusted probe fixture"),)])
