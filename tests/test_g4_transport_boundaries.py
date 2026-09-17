"""Loopback responses have a wall deadline and unambiguous JSON fields."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from reproloop.fixtures import AdapterCapabilities, FixtureError, FixtureOperationRequest, LoopbackFixtureAdapter


@contextmanager
def response_server(raw, *, drip=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                if drip:
                    for byte in raw:
                        self.wfile.write(bytes((byte,))); self.wfile.flush(); time.sleep(.005)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


class FixtureTransportBoundaryTests(unittest.TestCase):
    def request(self):
        return FixtureOperationRequest("operation_transport", "allocation_transport", 1,
                                       "a" * 64, "prepare", "seed_account", "b" * 64, {})

    def response(self):
        now = int(time.time() * 1000)
        return {"operationId": "operation_transport", "generation": 1, "status": "complete",
                "completedAtMs": now, "retentionExpiresAtMs": now + 60000, "fence": 1}

    def adapter(self, port):
        return LoopbackFixtureAdapter("transport", f"http://127.0.0.1:{port}",
                                      capabilities=AdapterCapabilities(True, True, 60000))

    def test_duplicate_outcome_fields_cannot_be_accepted_as_terminal(self):
        raw = json.dumps(self.response()).replace('"status": "complete"',
                                                '"status": "running", "status": "complete"').encode()
        with response_server(raw) as port:
            with self.assertRaises(FixtureError):
                self.adapter(port).execute(self.request(), timeout_seconds=1)

    def test_continuously_dripping_body_cannot_extend_the_deadline(self):
        with response_server(json.dumps(self.response()).encode(), drip=True) as port:
            started = time.monotonic()
            failure = None
            try:
                self.adapter(port).execute(self.request(), timeout_seconds=.03)
            except FixtureError as error:
                failure = error.code
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, .3)
            self.assertEqual(failure, "fixture_timeout")


if __name__ == "__main__":
    unittest.main()
