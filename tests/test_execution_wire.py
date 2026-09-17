"""Real socket boundary tests; these do not assert VM containment."""
import base64
import json
import socket
import struct
import threading
import time
import unittest

from reproloop.execution import wire


class ExecutionWireTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)
        self.host = wire.Channel(self.left, key=b"k" * 32, run_id="one", role="host",
                                 deadline=time.monotonic() + 2)
        self.guest = wire.Channel(self.right, key=b"k" * 32, run_id="one", role="guest",
                                  deadline=time.monotonic() + 2)

    def test_authenticated_bidirectional_messages(self):
        self.host.send("run", {"recipeId": "build", "inputDigest": "a" * 64})
        self.assertEqual(self.guest.receive()[0], "run")
        self.guest.send("result", {"exitCode": 0, "outputTruncated": False,
                                   "logDigest": "b" * 64})
        self.assertEqual(self.host.receive()[1]["exitCode"], 0)

    def capture_frame(self):
        self.host.send("cancel", {})
        header = self.right.recv(4)
        size = struct.unpack("!I", header)[0]
        body = b""
        while len(body) < size:
            body += self.right.recv(size - len(body))
        return header + body

    def test_replay_and_reflection_poison_the_channel(self):
        frame = self.capture_frame()
        self.left.sendall(frame)
        self.assertEqual(self.guest.receive(), ("cancel", {}))
        self.left.sendall(frame)
        with self.assertRaises(wire.ProtocolError):
            self.guest.receive()
        with self.assertRaises(wire.ProtocolError):
            self.guest.send("result", {"exitCode": 0, "outputTruncated": False,
                                       "logDigest": "b" * 64})
        self.right.sendall(frame)
        with self.assertRaises(wire.ProtocolError):
            self.host.receive()

    def test_forgery_and_wrong_session_never_produce_a_message(self):
        frame = self.capture_frame()
        body = json.loads(frame[4:])
        body["runId"] = "another"
        raw = json.dumps(body).encode()
        self.left.sendall(struct.pack("!I", len(raw)) + raw)
        with self.assertRaises(wire.ProtocolError):
            self.guest.receive()

    def test_closed_types_paths_and_chunk_bounds(self):
        invalid = [
            ("shell", {"command": "true"}),
            ("run", {"recipeId": "build", "inputDigest": "a" * 64, "argv": []}),
            ("input-start", {"digest": "a" * 64, "files": [
                {"path": "../escape", "digest": "b" * 64, "size": 1}]}),
            ("input-start", {"digest": "a" * 64, "files": [
                {"path": "signing.mobileprovision", "digest": "b" * 64, "size": 1}]}),
            ("input-chunk", {"index": True, "offset": 0, "data": "eA=="}),
            ("input-chunk", {"index": 0, "offset": 0, "data": "%%%"}),
            ("input-chunk", {"index": 0, "offset": 0, "data":
                             base64.b64encode(b"x" * (wire.CHUNK_BYTES + 1)).decode()}),
        ]
        for kind, payload in invalid:
            with self.subTest(kind=kind, keys=tuple(payload)):
                with self.assertRaises(wire.ProtocolError):
                    wire.validate_message("host", kind, payload)

    def test_oversize_header_and_duplicate_json_keys(self):
        self.left.sendall(struct.pack("!I", wire.MAX_FRAME_BYTES + 1))
        with self.assertRaises(wire.ProtocolError):
            self.guest.receive()
        with self.assertRaises(wire.ProtocolError):
            wire.decode_json(b'{"a":1,"a":2}')
        with self.assertRaises(wire.ProtocolError):
            wire.decode_json(b'{"a":NaN}')

    def test_timeout_is_absolute_and_partial_eof_is_terminal(self):
        self.guest.deadline = time.monotonic() + 0.04
        self.left.sendall(struct.pack("!I", 100) + b"{")
        with self.assertRaises(wire.ProtocolError):
            self.guest.receive()
        self.assertTrue(self.guest.failed)

    def test_cancel_interrupts_blocked_receive(self):
        self.host.cancel.set()
        with self.assertRaises(wire.ProtocolError):
            self.host.receive()

    def test_bootstrap_uses_private_transport_and_binds_ready(self):
        accepted = []
        def guest():
            accepted.append(wire.accept_bootstrap(self.right, deadline=time.monotonic() + 2))
            accepted[0].send("ready", {"agentDigest": "a" * 64, "catalogDigest": "b" * 64})
        thread = threading.Thread(target=guest)
        thread.start()
        channel = wire.bootstrap(self.left, run_id="boot-one", deadline=time.monotonic() + 2)
        self.assertEqual(channel.receive(), ("ready", {"agentDigest": "a" * 64,
                                                       "catalogDigest": "b" * 64}))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(accepted[0].run_id, "boot-one")
