import copy
from collections import OrderedDict
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from reproof.core import ContractError
from reproof.live.authority import HostAuthority, issue_local_parent_grant
from reproof.live.clock_sync import ClockReading
from reproof.live.model import Lab, LiveError
from reproof.live.providers import demo_device
from reproof.live.worker import (WORKER_CODE_VERSION, WORKER_PROTOCOL_VERSION,
                                   RemoteProvider, WorkerClient, WorkerServer, remote_devices)


TOKEN = "worker-test-token-0123456789abcdef"


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError("condition did not become true")


class WorkerProtocolUnitTests(unittest.TestCase):
    def test_authority_exchange_path_reaches_transport(self):
        class Response:
            status=200
            def read(self, limit):return b'{"ok":true}'
        class Connection:
            def __init__(self):self.path=None;self.sock=None
            def request(self,method,path,payload,headers):self.path=path
            def getresponse(self):return Response()
            def close(self):pass
        connection=Connection()
        with patch('reproof.live.worker.http.client.HTTPConnection',return_value=connection):
            client=WorkerClient('http://127.0.0.1:8765',TOKEN)
            self.assertEqual(client.call('/v1/authority/exchange'),{'ok':True})
        self.assertEqual(connection.path,'/v1/authority/exchange')

    def test_registry_rejects_old_shape_before_session_mutation(self):
        class Client:
            def __init__(self,value):self.value=value;self.calls=0
            def call(self,*args,**kwargs):self.calls+=1;return self.value
        current={"protocolVersion":WORKER_PROTOCOL_VERSION,"codeVersion":WORKER_CODE_VERSION,
                 "devices":[{"id":"device","kind":"android-live","capabilities":{}}]}
        self.assertEqual(remote_devices(Client(current),'host')[0]['id'],'host--device')
        with self.assertRaisesRegex(ContractError,'Incompatible worker protocol'):
            remote_devices(Client({"devices":current["devices"]}),'host')
        for field in ('protocolVersion', 'codeVersion'):
            invalid = copy.deepcopy(current)
            invalid[field] = float(invalid[field])
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, 'Incompatible worker protocol'):
                remote_devices(Client(invalid), 'host')
        shared = copy.deepcopy(current)
        shared['devices'][0]['capabilities']['authorityMode'] = 'shared-v2'
        client = Client(shared)
        with tempfile.TemporaryDirectory() as directory:
            lab = Lab(remote_devices(client, 'host'), directory)
            session = lab.create_session('host--device', 'owner', 'client')
            self.assertEqual(session['state'], 'failed')
            self.assertEqual(lab.list_devices()[0]['state'], 'available')
        self.assertEqual(client.calls, 1)

    def test_stable_operation_identity_survives_remote_queue_hop(self):
        operation='operation_'+'a'*40
        class Client:
            def __init__(self):self.command=None
            def call(self,path,body=None,timeout=10):
                self.command=copy.deepcopy(body)
                return {"session":{"controllerId":"remote-controller","epoch":3},
                        "receipt":{"status":"injected","id":body["commandId"],
                                   "sequence":body["sequence"],"action":body["action"],
                                   "epoch":3,"timing":"best-effort"}}
        client=Client();provider=RemoteProvider.__new__(RemoteProvider)
        provider.client=client;provider.remote_session_id='remote-session';provider.remote_controller='remote-controller'
        provider.remote_epoch=3;provider.parent_sid='parent-session';provider.sequence=0
        provider.lock=threading.RLock();provider.frame_map={9:(4,7)}
        provider.frame_ready=threading.Condition(provider.lock);provider.lab=type('LabStub',(),{'fail':lambda *args:None})()
        result=provider.execute_operation('tap',{'x':.5,'y':.5},operation_id=operation,
                                          frame={'frameId':9,'geometryVersion':1})
        self.assertTrue(result['ok']);self.assertEqual(client.command['commandId'],operation)

    def test_remote_authority_exchange_caps_worker_grant_at_origin_deadline(self):
        class Clock:
            def __init__(self, name):
                self.name = name
                self.now = 1_000_000_000

            def read(self):
                return ClockReading(self.name, 'd' * 64, self.now, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_clock = Clock('parent-clock')
            worker_clock = Clock('worker-clock')
            parent_authority = HostAuthority(
                root / 'parent.sqlite3', clock=parent_clock,
                lease_directory=root / 'parent-leases')
            worker_authority = HostAuthority(
                root / 'worker.sqlite3', clock=worker_clock,
                lease_directory=root / 'worker-leases')
            parent_grant = issue_local_parent_grant(
                parent_authority, lifetime_ns=5_000_000_000)
            worker_lab = Lab(
                [], root / 'worker-live', authority=worker_authority,
                parent_grant=issue_local_parent_grant(
                    worker_authority, lifetime_ns=60_000_000_000))
            server = WorkerServer.__new__(WorkerServer)
            server.lab = worker_lab
            server._authority_lock = threading.RLock()
            server._authority_exchanges = OrderedDict()
            server._authority_delegations = OrderedDict()

            class Client(WorkerClient):
                def __init__(self):
                    pass

                def call(self, path, body=None, timeout=10):
                    if body is None:
                        return server.begin_authority_exchange()
                    return server.finish_authority_exchange(body)

            parent_lab = Lab(
                [], root / 'parent-live', authority=parent_authority,
                parent_grant=parent_grant)
            provider = RemoteProvider(
                Client(), 'remote-device', authority_mode='shared-v2')
            try:
                identifier = provider._delegate_authority(parent_lab)
                delegated = server.consume_authority_delegation(identifier)
                self.assertLessEqual(delegated.local_deadline_ns,
                                     parent_grant.local_deadline_ns)
                self.assertLess(delegated.local_deadline_ns,
                                worker_lab.parent_grant.local_deadline_ns)
                parent_clock.now = parent_grant.local_deadline_ns + 1
                with self.assertRaises(LiveError) as expired:
                    provider._origin_current()
                self.assertEqual(expired.exception.code, 'authority_rejected')
            finally:
                parent_authority.close()
                worker_authority.close()


class WorkerTransportTests(unittest.TestCase):
    def setUp(self):
        self.worker_temp = tempfile.TemporaryDirectory()
        self.worker_lab = Lab([demo_device()], Path(self.worker_temp.name))
        self.server = WorkerServer(self.worker_lab, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = WorkerClient(self.server.origin, TOKEN)

    def tearDown(self):
        self.server.close_operations()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.worker_temp.cleanup()

    def test_auth_headers_and_bounded_json_are_rejected(self):
        bad = WorkerClient(self.server.origin, "wrong-worker-token-0123456789abcdef")
        with self.assertRaises(LiveError) as context:
            bad.call("/v1/devices")
        self.assertEqual(context.exception.code, "unauthorized")
        request = Request(self.server.origin + "/v1/devices", headers={"Authorization": "Bearer " + TOKEN, "Origin": self.server.origin})
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

        for raw in (b'{"deviceId":"demo","deviceId":"demo","clientId":"client"}',
                    b'{"deviceId":"demo","clientId":NaN}'):
            request = Request(self.server.origin + "/v1/sessions", data=raw,
                              headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as context:
                urlopen(request, timeout=5)
            self.assertEqual(context.exception.code, 400)
            context.exception.close()

    def test_remote_registry_and_public_data_never_expose_token_or_url(self):
        devices = remote_devices(self.client, "worker-a")
        self.assertEqual(devices[0]["id"], "worker-a--demo")
        public = json.dumps({key: value for key, value in devices[0].items() if key != "factory"})
        self.assertNotIn(TOKEN, public)
        self.assertNotIn(self.server.origin, public)
        with self.assertRaises(ContractError):
            WorkerServer(self.worker_lab, TOKEN, host="0.0.0.0")
        with self.assertRaises(ContractError):
            WorkerClient("http://192.0.2.1:9876", TOKEN)

    def test_wildcard_advertised_host_contract(self):
        with self.assertRaises(ContractError):
            WorkerServer(self.worker_lab, TOKEN, host="0.0.0.0")
        temp = tempfile.TemporaryDirectory()
        lab = Lab([demo_device()], Path(temp.name))
        advertised = WorkerServer(lab, TOKEN, host="127.0.0.1", advertised_host="worker.invalid")
        try:
            self.assertTrue(advertised.origin.startswith("http://worker.invalid:"))
        finally:
            advertised.server_close()
            temp.cleanup()

    def test_parent_remote_provider_allocates_inputs_records_replays_and_closes(self):
        devices = remote_devices(self.client, "worker-a")
        parent_temp = tempfile.TemporaryDirectory()
        parent = Lab(devices, Path(parent_temp.name))
        try:
            session = parent.create_session("worker-a--demo", "owner", "parent-client")
            session = wait_for(lambda: parent.get_session(session["id"], "owner") if parent.get_session(session["id"], "owner")["state"] == "active" else None)
            frame = parent.frame(session["id"])
            command = {
                "controllerId": session["controllerId"], "epoch": session["epoch"], "sequence": 1,
                "commandId": "parent-command-1", "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                "action": "tap", "payload": {"x": .5, "y": .5},
            }
            receipt = parent.input(session["id"], "owner", command)
            self.assertEqual(receipt["status"], "injected")
            current = parent.get_session(session["id"], "owner")
            parent.start_recording(session["id"], "owner", current["controllerId"], current["epoch"], reset=True)
            current = parent.get_session(session["id"], "owner")
            frame = parent.frame(session["id"])
            parent.input(session["id"], "owner", {
                "controllerId": current["controllerId"], "epoch": current["epoch"], "sequence": 2,
                "commandId": "parent-record-command", "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                "action": "tap", "payload": {"x": .5, "y": .5},
            })
            record = parent.stop_recording(session["id"], "owner", current["controllerId"], current["epoch"])
            self.assertTrue(record["replayable"])
            replay = parent.start_replay(session["id"], "owner", current["controllerId"], current["epoch"], record["id"])
            parent._session(session["id"])["replayThread"].join(timeout=5)
            final = parent.get_session(session["id"], "owner")
            self.assertEqual(final["replay"]["state"], "actions_replayed")
            self.assertTrue(replay["id"])
            closed = parent.close_session(session["id"], "owner", final["controllerId"], final["epoch"])
            self.assertEqual(closed["state"], "closed")
            self.assertEqual(self.worker_lab.list_devices()[0]["state"], "available")
            self.assertEqual(self.worker_lab.list_sessions("worker-coordinator")[0]["state"], "closed")
        finally:
            parent.close_all()
            parent_temp.cleanup()

    def test_parent_waits_for_connecting_worker_startup(self):
        class DelayedProvider:
            def __init__(self):
                self.stop = threading.Event()
                self.thread = None
                self.session = None
                self.lab = None

            def start(self, session, lab):
                self.session, self.lab = session, lab
                self.thread = threading.Thread(target=self.delayed_frame, daemon=True)
                self.thread.start()

            def delayed_frame(self):
                self.stop.wait(.2)
                if not self.stop.is_set():
                    self.lab.publish_frame(self.session["id"], b"<svg/>", "image/svg+xml", 400, 800, "portrait")

            def execute(self, action, payload):
                self.lab.publish_frame(self.session["id"], b"<svg/>", "image/svg+xml", 400, 800, "portrait")
                return {"ok": True, "timing": "best-effort"}

            def close(self):
                self.stop.set()
                if self.thread:
                    self.thread.join(timeout=2)

        worker_temp = tempfile.TemporaryDirectory()
        delayed_provider = DelayedProvider()
        delayed_lab = Lab([{
            "id": "delayed", "name": "Delayed", "platform": "demo", "kind": "demo",
            "capabilities": {"actions": ["tap", "reset"], "inputMode": "gesture-batch", "media": "demo-svg"},
            "factory": lambda: delayed_provider,
        }], Path(worker_temp.name))
        delayed_server = WorkerServer(delayed_lab, TOKEN)
        delayed_thread = threading.Thread(target=delayed_server.serve_forever, daemon=True)
        delayed_thread.start()
        parent_temp = tempfile.TemporaryDirectory()
        parent = None
        try:
            delayed_client = WorkerClient(delayed_server.origin, TOKEN)
            parent = Lab(remote_devices(delayed_client, "delayed-worker"), Path(parent_temp.name))
            session = parent.create_session("delayed-worker--delayed", "owner", "parent")
            active = wait_for(lambda: parent.get_session(session["id"], "owner") if parent.get_session(session["id"], "owner")["state"] == "active" else None)
            self.assertEqual(active["state"], "active")
            parent.close_session(session["id"], "owner")
        finally:
            if parent is not None:
                parent.close_all()
            parent_temp.cleanup()
            delayed_server.close_operations()
            delayed_server.shutdown()
            delayed_server.server_close()
            delayed_thread.join(timeout=5)
            worker_temp.cleanup()

    def test_parent_handoff_fences_old_epoch_and_lost_worker_quarantines(self):
        devices = remote_devices(self.client, "worker-a")
        parent_temp = tempfile.TemporaryDirectory()
        parent = Lab(devices, Path(parent_temp.name))
        try:
            session = parent.create_session("worker-a--demo", "owner", "parent-client")
            session = wait_for(lambda: parent.get_session(session["id"], "owner") if parent.get_session(session["id"], "owner")["state"] == "active" else None)
            old = copy.deepcopy(session)
            claimed = parent.claim(session["id"], "owner", "new-client", session["epoch"], "manual")
            self.assertEqual(claimed["controllerId"], "new-client")
            with self.assertRaises(LiveError):
                parent.input(session["id"], "owner", {
                    "controllerId": old["controllerId"], "epoch": old["epoch"], "sequence": 1,
                    "commandId": "old-command", "frameId": old["frame"]["id"],
                    "geometryVersion": old["frame"]["geometryVersion"], "action": "tap",
                    "payload": {"x": .5, "y": .5},
                })
            self.server.close_operations()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            wait_for(lambda: parent.get_session(session["id"], "owner")["state"] == "failed")
            self.assertEqual(parent.list_devices()[0]["state"], "quarantined")
        finally:
            parent.close_all()
            parent_temp.cleanup()

    def test_receipt_mismatch_fails_parent_without_retry(self):
        parent_temp = tempfile.TemporaryDirectory()
        parent = Lab(remote_devices(self.client, "worker-a"), Path(parent_temp.name))
        original_call = self.client.call
        input_calls = []

        def malformed(path, body=None, *, timeout=10):
            result = original_call(path, body, timeout=timeout)
            if path.endswith("/input"):
                input_calls.append(path)
                result["receipt"]["sequence"] += 1
            return result

        self.client.call = malformed
        try:
            session = parent.create_session("worker-a--demo", "owner", "parent")
            session = wait_for(lambda: parent.get_session(session["id"], "owner") if parent.get_session(session["id"], "owner")["state"] == "active" else None)
            frame = parent.frame(session["id"])
            with self.assertRaises(LiveError):
                parent.input(session["id"], "owner", {
                    "controllerId": session["controllerId"], "epoch": session["epoch"], "sequence": 1,
                    "commandId": "mismatch-command", "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                    "action": "tap", "payload": {"x": .5, "y": .5},
                })
            self.assertEqual(len(input_calls), 1)
            self.assertEqual(parent.get_session(session["id"], "owner")["state"], "failed")
        finally:
            parent.close_all()
            parent_temp.cleanup()

    def test_remote_epoch_drift_fails_parent_instead_of_adopting_epoch(self):
        parent_temp = tempfile.TemporaryDirectory()
        parent = Lab(remote_devices(self.client, "worker-a"), Path(parent_temp.name))
        try:
            session = parent.create_session("worker-a--demo", "owner", "parent")
            session = wait_for(lambda: parent.get_session(session["id"], "owner") if parent.get_session(session["id"], "owner")["state"] == "active" else None)
            provider = parent._session(session["id"])["provider"]
            remote = self.worker_lab._session(provider.remote_session_id)
            remote["epoch"] += 1
            wait_for(lambda: parent.get_session(session["id"], "owner")["state"] == "failed")
            self.assertEqual(parent.list_devices()[0]["state"], "quarantined")
        finally:
            parent.close_all()
            parent_temp.cleanup()


if __name__ == "__main__":
    unittest.main()
