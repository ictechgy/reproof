"""Real HTTP framing and range validation through the public worker client."""
from __future__ import annotations
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from reproof.live.model import LiveError
from reproof.live.worker import WorkerClient


class ResponseFramingTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *_):
                pass

            def do_GET(self):
                status, headers, body = self.server.reply
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.client = WorkerClient(f'http://127.0.0.1:{self.server.server_port}', 'p' * 40)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def reply(self, body, *, status=200, length=None, range_value=None, binary=False):
        headers = [('Content-Type', 'application/octet-stream' if binary else 'application/json'),
                   ('Content-Length', str(len(body)) if length is None else str(length))]
        if range_value is not None:
            headers.append(('Content-Range', range_value))
        self.server.reply = status, headers, body

    def test_complete_json_control_succeeds(self):
        self.reply(b'{"devices":[]}')
        self.assertEqual(self.client.call('/v1/devices'), {'devices': []})

    def test_complete_range_control_succeeds(self):
        self.reply(b'cdef', status=206, range_value='bytes 2-5/8', binary=True)
        self.assertEqual(self.client.download_artifact(
            'artifact_parent', start=2, end=6, project_id='parent_project'), b'cdef')

    def test_truncated_json_body_cannot_be_a_successful_response(self):
        self.reply(b'{"devices":[]}', length=100)
        with self.assertRaises(LiveError):
            self.client.call('/v1/devices')

    def test_truncated_binary_download_is_rejected(self):
        self.reply(b'abcd', length=8, status=206, range_value='bytes 0-7/8', binary=True)
        with self.assertRaises(LiveError):
            self.client.download_artifact(
                'artifact_parent', start=0, end=8, project_id='parent_project')

    def test_wrong_range_is_rejected_even_with_the_requested_byte_count(self):
        self.reply(b'abcd', status=206, range_value='bytes 0-3/8', binary=True)
        with self.assertRaises(LiveError):
            self.client.download_artifact(
                'artifact_parent', start=2, end=6, project_id='parent_project')

    def test_full_response_cannot_silently_replace_a_requested_partial_range(self):
        self.reply(b'abcdefgh', binary=True)
        try:
            result = self.client.download_artifact(
                'artifact_parent', start=2, end=6, project_id='parent_project')
        except LiveError:
            return
        # HTTP may legally ignore Range. The client may reject that reply or
        # extract the requested bytes, but must not return the whole object.
        self.assertEqual(result, b'cdef')
