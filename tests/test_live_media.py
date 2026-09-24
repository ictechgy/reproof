import io
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from reproof.core import ContractError
from reproof.live.media import MAX_IMAGE_BYTES, MAX_METADATA_BYTES, encode_frame, serve_frames
from reproof.live.model import Lab, LiveError
from reproof.live.providers import demo_device


def read_records(data):
    records = []
    stream = io.BytesIO(data)
    while stream.tell() < len(data):
        header = stream.read(8)
        if len(header) != 8:
            raise AssertionError("truncated frame header")
        metadata_length, image_length = struct.unpack(">II", header)
        metadata = json.loads(stream.read(metadata_length))
        image = stream.read(image_length)
        if len(image) != image_length:
            raise AssertionError("truncated frame image")
        records.append((metadata, image))
    return records


class MediaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    lab = None
    sid = None
    owner = None
    duration = .4

    def do_GET(self):
        requested_owner = self.path.partition("?")[2].removeprefix("owner=") or self.owner
        try:
            serve_frames(self, self.lab, self.sid, requested_owner, duration=self.duration, max_fps=30)
        except LiveError as error:
            self.send_response(error.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


class FakeHandler:
    def __init__(self, writer):
        self.wfile = writer
        self.connection = socket.socket()
        self.protocol_version = "HTTP/1.1"
        self.close_connection = False
        self.headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


class TimeoutWriter:
    def write(self, data):
        raise TimeoutError()

    def flush(self):
        pass


class CollectWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    def flush(self):
        pass


class NoFrameCondition:
    def __init__(self):
        self.waits = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        time.sleep(timeout or 0)


class NoFrameLab:
    def __init__(self):
        self.condition = NoFrameCondition()
        self.session = {"state": "active", "frame": None, "frameCondition": self.condition}

    def _session(self, sid, owner):
        return self.session

    def get_session(self, sid, owner):
        return {"state": self.session["state"]}

    def heartbeat(self, sid, owner, client_id):
        return {"state": "active"}


class LiveMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lab = Lab([demo_device()], Path(self.temp.name))
        self.session = self.lab.create_session("demo", "owner", "client")
        self.sid = self.session["id"]

    def tearDown(self):
        self.lab.close_all()
        self.temp.cleanup()

    def frame_command(self, sequence=1):
        frame = self.lab.frame(self.sid)
        return {
            "controllerId": "client", "epoch": self.session["epoch"], "sequence": sequence,
            "commandId": f"command-{sequence}", "frameId": frame["id"],
            "geometryVersion": frame["geometryVersion"], "action": "tap", "payload": {"x": .5, "y": .5},
        }

    def test_encode_frame_validates_binary_lengths_and_end_records(self):
        packet = encode_frame({"type": "end", "reason": "stream_complete"})
        metadata_length, image_length = struct.unpack(">II", packet[:8])
        self.assertEqual(image_length, 0)
        self.assertEqual(json.loads(packet[8:8 + metadata_length]), {"type": "end", "reason": "stream_complete"})
        with self.assertRaises(ContractError):
            encode_frame({"type": "end", "reason": "stream_complete"}, b"x")
        with self.assertRaises(ContractError):
            encode_frame(None)
        with self.assertRaises(ContractError):
            encode_frame({"type": "frame", "id": 1, "geometryVersion": 1, "width": 1,
                          "height": 1, "orientation": "portrait", "capturedAt": 0,
                          "mime": "image/png"})
        with self.assertRaises(ContractError):
            encode_frame({"type": "frame", "id": 1, "geometryVersion": 1, "width": 1,
                          "height": 1, "orientation": "portrait", "capturedAt": 0,
                          "mime": "image/png", "extra": "x"})
        huge_metadata = {"type": "end", "reason": "stream_complete", "x": "x" * MAX_METADATA_BYTES}
        with self.assertRaises(ContractError):
            encode_frame(huge_metadata)
        with self.assertRaises(ContractError):
            encode_frame({"type": "frame", "id": 1, "geometryVersion": 1, "width": 1,
                          "height": 1, "orientation": "portrait", "capturedAt": 0,
                          "mime": "image/png"}, b"x" * (MAX_IMAGE_BYTES + 1))

    def test_real_http_stream_is_binary_latest_frames_and_has_end_record(self):
        handler_type = type("BoundMediaHandler", (MediaHandler,), {
            "lab": self.lab, "sid": self.sid, "owner": "owner", "duration": .5,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = urlopen(f"http://127.0.0.1:{server.server_port}/frames", timeout=5)
            first_header = response.read(8)
            metadata_length, image_length = struct.unpack(">II", first_header)
            first_metadata = json.loads(response.read(metadata_length))
            first_image = response.read(image_length)
            self.assertEqual(first_metadata["type"], "frame")
            self.assertNotIn("imageBase64", first_metadata)
            self.assertTrue(first_image.startswith(b"<svg"))
            self.lab.input(self.sid, "owner", self.frame_command())
            records = [(first_metadata, first_image)] + read_records(response.read())
            self.assertGreaterEqual(len(records), 3)
            self.assertEqual(records[-1][0], {"type": "end", "reason": "stream_complete"})
            self.assertEqual(records[-1][1], b"")
            frame_ids = [metadata["id"] for metadata, _ in records if metadata["type"] == "frame"]
            self.assertEqual(frame_ids, sorted(set(frame_ids)))
            response.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_foreign_owner_is_rejected_before_stream_headers(self):
        handler_type = type("BoundMediaHandler", (MediaHandler,), {
            "lab": self.lab, "sid": self.sid, "owner": "owner", "duration": .1,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as context:
                urlopen(f"http://127.0.0.1:{server.server_port}/frames?owner=foreign", timeout=5)
            self.assertEqual(context.exception.code, 403)
            context.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_session_close_emits_closed_end_record(self):
        handler_type = type("BoundMediaHandler", (MediaHandler,), {
            "lab": self.lab, "sid": self.sid, "owner": "owner", "duration": 5,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = urlopen(f"http://127.0.0.1:{server.server_port}/frames", timeout=5)
            header = response.read(8)
            metadata_length, image_length = struct.unpack(">II", header)
            response.read(metadata_length + image_length)
            self.lab.close_session(self.sid, "owner")
            records = read_records(response.read())
            self.assertEqual(records[-1][0], {"type": "end", "reason": "session_closed"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_client_write_timeout_is_quiet_and_does_not_close_session(self):
        handler = FakeHandler(TimeoutWriter())
        try:
            serve_frames(handler, self.lab, self.sid, "owner", duration=.1)
        finally:
            handler.connection.close()
        self.assertEqual(self.lab.get_session(self.sid, "owner")["state"], "active")

    def test_no_frame_wait_uses_positive_bounded_condition_timeouts(self):
        lab = NoFrameLab()
        writer = CollectWriter()
        handler = FakeHandler(writer)
        try:
            serve_frames(handler, lab, "session", "owner", duration=.03)
        finally:
            handler.connection.close()
        self.assertTrue(lab.condition.waits)
        self.assertTrue(all(timeout > 0 for timeout in lab.condition.waits))
        self.assertEqual(read_records(bytes(writer.data))[-1][0], {"type": "end", "reason": "stream_complete"})


if __name__ == "__main__":
    unittest.main()
