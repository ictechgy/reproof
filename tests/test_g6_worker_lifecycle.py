"""Real worker service retention, request deadlines and storage shutdown."""
import hashlib
import os
import socket
import threading
import time
import unittest
from unittest import mock

from reproloop.live import worker as worker_module
from tests import test_g6_transfer_http as http_fixture


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        interval = mock.patch.object(worker_module, 'ARTIFACT_RETENTION_INTERVAL', .05, create=True)
        interval.start(); self.addCleanup(interval.stop)
        http_fixture.TransferHttpTests.setUp(self)

    tearDown = http_fixture.TransferHttpTests.tearDown
    publish = http_fixture.TransferHttpTests.publish

    def test_service_periodically_removes_expired_uploads(self):
        upload = self.publish(expires=1001)
        self.now = 1002
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self.transfer.status(upload['objectId'], host_identity=self.server.host_identity)['state'] == 'tombstoned':
                break
            time.sleep(.02)
        self.assertEqual(self.transfer.status(
            upload['objectId'], host_identity=self.server.host_identity)['state'], 'tombstoned')
        self.assertIsNone(self.evidence.lookup(upload['digest']))

    def test_shutdown_drains_an_admitted_write_before_closing_lab_storage(self):
        body = b'owned write held during worker shutdown'
        upload = self.client.allocate_artifact(
            project_id='project_a', kind='manifest', size=len(body),
            digest=hashlib.sha256(body).hexdigest(), metadata={},
            retention_class='original', retain_until_ms=2000)
        entered = threading.Event(); release = threading.Event(); lab_closed = threading.Event()
        original_write = os.pwrite; original_close = self.lab.close_all
        outcome = []

        def held_write(*args):
            entered.set()
            self.assertTrue(release.wait(4))
            return original_write(*args)

        def close_lab():
            lab_closed.set(); original_close()

        def send():
            try:
                outcome.append(self.client.upload_artifact_chunk(
                    upload['objectId'], upload['uploadGeneration'], 0, body))
            except Exception as exc:
                outcome.append(type(exc).__name__)

        sender = threading.Thread(target=send)
        closer = threading.Thread(target=self.server.close_operations)
        with mock.patch.object(os, 'pwrite', held_write), mock.patch.object(self.lab, 'close_all', close_lab):
            try:
                sender.start(); self.assertTrue(entered.wait(3))
                closer.start()
                self.assertFalse(lab_closed.wait(.15), 'Lab storage closed while the writer was still active')
            finally:
                release.set(); sender.join(5)
                if closer.ident is not None: closer.join(5)
        self.assertFalse(sender.is_alive() or closer.is_alive())
        self.assertTrue(lab_closed.is_set())
        self.assertTrue(self.transfer._closed)

    def test_dripped_request_headers_have_an_absolute_service_deadline(self):
        stopped = threading.Event()
        result = {'sent': 0}
        with mock.patch.object(worker_module, 'REQUEST_DEADLINE_SECONDS', .25, create=True):
            connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=1.2)
            connection.settimeout(1.2)
            connection.sendall((f'GET /v1/devices HTTP/1.1\r\nHost: {self.server.origin_netloc}\r\nX-Probe: ').encode())

            def drip():
                while not stopped.wait(.03):
                    try:
                        connection.sendall(b'x'); result['sent'] += 1
                    except OSError:
                        return

            thread = threading.Thread(target=drip); thread.start()
            started = time.monotonic()
            try:
                try:
                    connection.recv(4096)
                    result['returned'] = True
                except socket.timeout:
                    result['returned'] = False
            finally:
                result['elapsed'] = time.monotonic() - started
                stopped.set(); connection.close(); thread.join(2)
        self.assertTrue(result['returned'], result)
        self.assertGreater(result['sent'], 2)
        self.assertLess(result['elapsed'], .8, result)
