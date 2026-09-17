import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import URLError

from reproloop.live.client import Client
from reproloop.live.model import Lab, LiveError
from reproloop.live.providers import demo_device
from reproloop.live.server import LiveServer
from reproloop.live.tools import MAX_LINE, serve


class StubClient:
    def __init__(self):
        self.calls = []

    def call(self, path, body=None):
        self.calls.append((path, body))
        return {"path": path, "body": body}


def request(request_id, tool, arguments):
    return json.dumps({"id": request_id, "tool": tool, "arguments": arguments})


class LiveToolsTests(unittest.TestCase):
    def run_requests(self, *lines):
        client = StubClient()
        output = io.StringIO()
        self.assertEqual(serve(client, io.StringIO("\n".join(lines) + "\n"), output), 0)
        return client, [json.loads(line) for line in output.getvalue().splitlines()]
    def test_repair_tools_cannot_choose_source_paths_or_external_agent_targets(self):
        arguments={'sessionId':'session','controllerId':'controller','epoch':1,'recordingId':'recording','requestId':'request'}
        client,responses=self.run_requests(request('submit','repairs.submit',arguments),
            request('unsafe','repairs.submit',{**arguments,'source':'/private/source'}),
            request('resume','repairs.resume',{'jobId':'job','clientId':'controller'}))
        self.assertTrue(responses[0]['ok'])
        self.assertFalse(responses[1]['ok'])
        self.assertEqual([call[0] for call in client.calls],['/api/sessions/session/repair','/api/repairs/job/resume'])

    def test_maps_supported_tools_to_exact_local_routes(self):
        command = {"controllerId": "client-1", "epoch": 1, "sequence": 1, "commandId": "command-1",
                   "frameId": 1, "geometryVersion": 1, "action": "tap", "payload": {"x": .5, "y": .5}}
        lines = [
            request("a", "devices.list", {}),
            request("b", "sessions.list", {}),
            request("c", "sessions.create", {"deviceId": "demo", "clientId": "client-1"}),
            request("d", "sessions.get", {"sessionId": "session-1"}),
            request("e", "sessions.close", {"sessionId": "session-1", "controllerId": "client-1", "epoch": 1}),
            request("f", "control.claim", {"sessionId": "session-1", "clientId": "client-2", "expectedEpoch": 1, "mode": "automation"}),
            request("g", "frame.observe", {"sessionId": "session-1"}),
            request("h", "input.send", {"sessionId": "session-1", "command": command}),
            request("i", "recordings.list", {}),
            request("j", "recordings.get", {"recordingId": "recording-1"}),
            request("k", "recordings.import", {"recording": {"id": "recording-1", "events": []}}),
            request("l", "recordings.derive", {"recordingId": "recording-1", "eventIds": ["e1"], "speed": 2.0}),
            request("m", "replay.start", {"sessionId": "session-1", "controllerId": "client-1", "epoch": 1, "recordingId": "recording-1", "variables": {}}),
            request("n", "replay.cancel", {"sessionId": "session-1", "clientId": "client-1"}),
            request("o", "jobs.list", {}),
            request("p", "jobs.get", {"jobId": "job-1"}),
            request("q", "jobs.submit", {"request": {"recordingId": "recording-1", "variables": {}, "requestId": "request-1", "repeats": 2, "timeoutSeconds": 30}}),
            request("r", "jobs.cancel", {"jobId": "job-1"}),
        ]
        client, responses = self.run_requests(*lines)
        self.assertTrue(all(response["ok"] for response in responses))
        self.assertEqual([path for path, _ in client.calls], [
            "/api/devices", "/api/sessions", "/api/sessions", "/api/sessions/session-1",
            "/api/sessions/session-1/close", "/api/sessions/session-1/control", "/api/sessions/session-1/frame",
            "/api/sessions/session-1/input", "/api/recordings", "/api/recordings/recording-1",
            "/api/recordings/import", "/api/recordings/recording-1/derive", "/api/sessions/session-1/replay",
            "/api/sessions/session-1/replay/cancel", "/api/jobs", "/api/jobs/job-1", "/api/jobs", "/api/jobs/job-1/cancel",
        ])
        self.assertEqual(client.calls[10][1], {"recording": {"id": "recording-1", "events": []}})
        self.assertEqual(client.calls[16][1]["repeats"], 2)

    def test_recording_and_heartbeat_tools_map_to_session_routes(self):
        lines = [
            request("a", "sessions.heartbeat", {"sessionId": "session-1", "clientId": "client-1"}),
            request("b", "recordings.start", {"sessionId": "session-1", "controllerId": "client-1", "epoch": 1, "reset": True}),
            request("c", "recordings.stop", {"sessionId": "session-1", "controllerId": "client-1", "epoch": 1}),
        ]
        client, responses = self.run_requests(*lines)
        self.assertTrue(all(response["ok"] for response in responses))
        self.assertEqual([path for path, _ in client.calls], [
            "/api/sessions/session-1/heartbeat", "/api/sessions/session-1/recordings/start",
            "/api/sessions/session-1/recordings/stop",
        ])
        self.assertEqual(client.calls[1][1]["reset"], True)

    def test_rejects_unknown_keys_and_path_traversal_without_calling_client(self):
        client, responses = self.run_requests(
            request("bad-key", "devices.list", {"extra": 1}),
            request("bad-path", "sessions.get", {"sessionId": "../secret"}),
            request("bad-tool", "not.allowed", {}),
        )
        self.assertEqual(client.calls, [])
        self.assertTrue(all(not response["ok"] for response in responses))
        self.assertTrue(all(response["error"]["code"] == "invalid_argument" for response in responses[:2]))
        self.assertEqual(responses[2]["error"]["code"], "invalid_argument")

    def test_malformed_duplicate_and_nonfinite_lines_do_not_stop_following_requests(self):
        client, responses = self.run_requests(
            '{"id":"dup","id":"other","tool":"devices.list","arguments":{}}',
            '{"id":"nan","tool":"recordings.derive","arguments":{"recordingId":"r","speed":NaN}}',
            request("after", "devices.list", {}),
        )
        self.assertEqual(len(responses), 3)
        self.assertTrue(all(not response["ok"] for response in responses[:2]))
        self.assertTrue(responses[2]["ok"])
        self.assertEqual([path for path, _ in client.calls], ["/api/devices"])

    def test_oversized_line_is_drained_and_next_request_is_processed(self):
        oversized = "x" * (MAX_LINE + 100)
        client, responses = self.run_requests(oversized, request("after", "devices.list", {}))
        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["error"]["code"], "invalid_argument")
        self.assertTrue(responses[1]["ok"])
        self.assertEqual([path for path, _ in client.calls], ["/api/devices"])

    def test_remote_error_is_fixed_and_request_text_is_never_echoed(self):
        class FailingClient(StubClient):
            def call(self, path, body=None):
                raise LiveError("unauthorized", "remote secret: should not appear", 401)

        secret = "TOP_SECRET_TEXT"
        source = io.StringIO(request("safe", "devices.list", {}) + "\n")
        output = io.StringIO()
        serve(FailingClient(), source, output)
        encoded = output.getvalue()
        self.assertNotIn(secret, encoded)
        self.assertNotIn("remote secret", encoded)
        response = json.loads(encoded)
        self.assertEqual(response["error"], {"code": "unauthorized", "message": "Local API authentication failed"})

        _, responses = self.run_requests(request("safe", "devices.list", {"raw": secret}))
        self.assertNotIn(secret, json.dumps(responses))

        class TransportClient(StubClient):
            def call(self, path, body=None):
                raise URLError("secret host details")

        output = io.StringIO()
        serve(TransportClient(), io.StringIO(request("transport", "devices.list", {}) + "\n"), output)
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"], {"code": "transport_unavailable", "message": "Local API connection unavailable"})


class LiveToolsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lab = Lab([demo_device()], Path(self.temp.name))
        self.server = LiveServer(self.lab, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.server.origin)

    def tearDown(self):
        self.server.close_operations()
        self.lab.close_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def tool(self, request_id, name, arguments):
        output = io.StringIO()
        self.assertEqual(serve(self.client, io.StringIO(request(request_id, name, arguments) + "\n"), output), 0)
        response = json.loads(output.getvalue())
        self.assertTrue(response["ok"], response)
        return response["result"]

    def test_real_loopback_recording_flow_redacts_text_and_derives(self):
        devices = self.tool("devices", "devices.list", {})
        self.assertEqual(devices["devices"][0]["id"], "demo")
        session = self.tool("create", "sessions.create", {"deviceId": "demo", "clientId": "agent-a"})["session"]
        sid = session["id"]
        self.tool("heartbeat", "sessions.heartbeat", {"sessionId": sid, "clientId": "agent-a"})
        self.tool("start", "recordings.start", {
            "sessionId": sid, "controllerId": "agent-a", "epoch": session["epoch"], "reset": True,
        })
        frame = self.tool("frame", "frame.observe", {"sessionId": sid})
        private_text = "SYNTHETIC_PRIVATE_TEXT_SHOULD_NOT_PERSIST"
        command = {
            "controllerId": "agent-a", "epoch": session["epoch"], "sequence": 1,
            "commandId": "agent-command-1", "frameId": frame["id"],
            "geometryVersion": frame["geometryVersion"], "action": "text",
            "payload": {"value": private_text},
        }
        receipt = self.tool("input", "input.send", {"sessionId": sid, "command": command})["receipt"]
        self.assertEqual(receipt["status"], "injected")
        stopped = self.tool("stop", "recordings.stop", {
            "sessionId": sid, "controllerId": "agent-a", "epoch": session["epoch"],
        })["recording"]
        self.assertNotIn(private_text, json.dumps(stopped))
        self.assertEqual(stopped["variables"], ["text_1"])
        derived = self.tool("derive", "recordings.derive", {"recordingId": stopped["id"], "speed": 1})["recording"]
        self.assertEqual(derived["provenance"]["sourceRecordingId"], stopped["id"])
        imported = self.tool("import", "recordings.import", {"recording": derived})
        self.assertFalse(imported["imported"])

        output = io.StringIO()
        serve(self.client, io.StringIO(
            request("unknown", "unknown.tool", {}) + "\n" + request("after", "devices.list", {}) + "\n"), output)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertFalse(lines[0]["ok"])
        self.assertTrue(lines[1]["ok"])

        current = self.tool("get", "sessions.get", {"sessionId": sid})["session"]
        closed = self.tool("close", "sessions.close", {
            "sessionId": sid, "controllerId": current["controllerId"], "epoch": current["epoch"],
        })["session"]
        self.assertEqual(closed["state"], "closed")


if __name__ == "__main__":
    unittest.main()
