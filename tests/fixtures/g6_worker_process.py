"""Owned same-Mac G6 worker process used by public protocol tests.

The adapter is deliberately synthetic.  It exercises enrollment, G1 device
ownership, inventory and artifact HTTP boundaries without claiming a physical
device or a second Mac.
"""
from __future__ import annotations

import json
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reproof.ios_profile import validate_ios_profile
from reproof.core import digest
from reproof.live.artifact_transfer import ArtifactTransferStore
from reproof.live.authority import HostAuthority
from reproof.live.clock_sync import ClockReading
from reproof.live.enrollment import EnrollmentClient
from reproof.live.evidence_store import EvidenceStore
from reproof.live.inventory import enrolled_inventory_document
from reproof.live.model import Lab, check
from reproof.live.worker import WorkerServer
from reproof.storage import _unique_object


class _Clock:
    def __init__(self, name):
        self.name = name
        self.now = 1_000_000_000

    def read(self):
        return ClockReading(self.name, "d" * 64, self.now, 0)


class _SyntheticPhysicalProvider:
    def bind_authority(self, authority, provider_incarnation):
        self.authority = authority

    def start_authorized(self, session, lab, permit):
        self.authority.check_dispatch_permit(permit)
        self.session = session
        self.lab = lab
        lab.publish_frame(session["id"], b"<svg>same-mac-g6</svg>",
                          "image/svg+xml", 320, 640, "portrait")
        return {"ok": True, "qualification": "synthetic-protocol-only"}

    def close_authorized(self, permit):
        self.authority.check_dispatch_permit(permit)
        return {"ok": True}


def _profile(project):
    application = project["applications"][0]
    build = project["builds"][0]
    return validate_ios_profile({
        "schemaVersion": 2,
        "kind": "reproof-runtime-application",
        "id": "synthetic_ios_profile",
        "projectId": project["id"],
        "projectDigest": digest(project),
        "applicationId": application["id"],
        "buildId": build["id"],
        "platform": "ios",
        "bundle": application["bundle"],
        "launchTarget": {"kind": "bundle", "value": application["bundle"]},
        "artifact": {
            "kind": "ios-app", "sha256": build["artifactDigest"],
            "bytes": 1, "provenanceDigest": build["sourceDigest"],
            "bundleVersion": "1.0", "bundleBuild": "1",
        },
        "helper": {"protocolVersion": 2, "version": 2},
        "capabilities": {
            "actions": ["tap"], "locator": None,
            "observations": ["pixels"],
            "geometry": {"maxWidth": 320, "maxHeight": 640,
                         "orientations": ["portrait"]},
            "captureAdapter": {"id": "native-frame", "version": 1},
            "logAdapter": None,
        },
        "approvedReferences": {"launch": "synthetic_launch",
                               "preparations": []},
        "identityRequirement": "install-and-launch",
    })


def _read_configuration():
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    check(0 < len(raw) <= 128 * 1024, "invalid_config",
          "Invalid process fixture configuration", 400)
    value = json.loads(raw, object_pairs_hook=_unique_object)
    expected = {"coordinator", "enrollmentToken", "transportToken",
                "hostId", "incarnation", "output", "authorityRoot",
                "deviceAlias", "physicalId", "project", "collectionPolicy"}
    check(type(value) is dict and set(value) == expected, "invalid_config",
          "Invalid process fixture configuration", 400)
    return value


def main():
    server = None
    server_thread = None
    inventory_thread = None
    transfer = None
    artifact_evidence = None
    authority = None
    stop = threading.Event()
    try:
        configuration = _read_configuration()
        client = EnrollmentClient(configuration["coordinator"])
        enrolled = client.enroll(
            configuration["enrollmentToken"],
            host_id=configuration["hostId"],
            incarnation=configuration["incarnation"])
        credential = enrolled.pop("credential")
        expected_host = (enrolled["hostId"], enrolled["generation"],
                         enrolled["incarnation"])
        authority_root = Path(configuration["authorityRoot"]).absolute()
        output = Path(configuration["output"]).absolute()
        authority = HostAuthority(
            authority_root / "authority.sqlite3",
            clock=_Clock("worker-" + configuration["hostId"]),
            lease_directory=authority_root / "leases")
        profile = _profile(configuration["project"])
        device = {
            "id": configuration["deviceAlias"],
            "name": "Owned synthetic physical adapter",
            "platform": "ios", "kind": "ios-physical",
            "_authority": {"deviceKind": "ios-physical",
                           "physicalId": configuration["physicalId"]},
            "capabilities": {
                "actions": ["tap"], "inputMode": "gesture-batch",
                "media": "demo-svg", "authorityMode": "shared-v2",
                "applicationIdentity": {
                    "bundle": profile.bundle,
                    "artifactDigest": profile.data["artifact"]["sha256"],
                    "applicationProfileDigest": profile.digest},
                "applicationProfile": profile.data,
                "applicationProfileDigest": profile.digest,
                "identityEvidence": "synthetic-protocol-only",
                "locatorKinds": [],
            },
            "factory": _SyntheticPhysicalProvider,
        }
        lab = Lab([device], output, authority=authority, parent_grant=None,
                  delegated_authority_only=True)
        registration = lab.register_recording_project(
            configuration["project"], configuration["collectionPolicy"],
            capacity_bytes=16 * 1024 * 1024,
            journal_headroom_bytes=512 * 1024)
        artifact_root = output / "artifact-v2"
        artifact_evidence = EvidenceStore(
            artifact_root / "objects", lab._recording_budget,
            max_object_bytes=2 * 1024 * 1024)
        transfer = ArtifactTransferStore(
            artifact_root / "transfers", lab._recording_budget,
            artifact_evidence, object_quota_bytes=2 * 1024 * 1024,
            project_quota_bytes=4 * 1024 * 1024,
            host_quota_bytes=8 * 1024 * 1024)

        def host_authorizer(project_id=None):
            current = (client.authorize(credential, project_id=project_id)
                       if project_id is not None else
                       client.authenticate(credential))
            check((current["hostId"], current["generation"],
                   current["incarnation"]) == expected_host,
                  "unauthorized", "Enrolled host identity is stale", 401)
            return True

        def refresh_inventory():
            document = enrolled_inventory_document(
                list(lab.devices.values()), generation=expected_host[1],
                incarnation=expected_host[2], authority=authority)
            return client.refresh_inventory(credential, document)

        refresh_inventory()
        server = WorkerServer(
            lab, configuration["transportToken"], host_authorizer=host_authorizer,
            host_identity=expected_host, artifact_store=transfer,
            registered_projects=[registration])
        server_thread = threading.Thread(target=server.serve_forever,
                                         name="g6-worker-http", daemon=True)
        server_thread.start()

        def inventory_loop():
            while not stop.wait(0.5):
                try:
                    refresh_inventory()
                except Exception:
                    pass

        inventory_thread = threading.Thread(
            target=inventory_loop, name="g6-worker-inventory", daemon=True)
        inventory_thread.start()
        signal.signal(signal.SIGTERM, lambda *_args: stop.set())
        signal.signal(signal.SIGINT, lambda *_args: stop.set())
        print(json.dumps({
            "worker": server.origin, "hostId": expected_host[0],
            "hostGeneration": expected_host[1],
            "hostIncarnation": expected_host[2],
            "sameMacProtocol": True, "syntheticAdapter": True,
            "physicalDeviceAcceptance": False, "twoMacAcceptance": False,
        }, separators=(",", ":")), flush=True)
        while not stop.wait(0.1):
            pass
        return 0
    except Exception:
        print(json.dumps({"error": "g6_worker_fixture_failed"}), flush=True)
        return 2
    finally:
        stop.set()
        if inventory_thread is not None:
            inventory_thread.join(timeout=2)
        if server is not None:
            server.shutdown()
            if server_thread is not None:
                server_thread.join(timeout=3)
            server.server_close()
        if transfer is not None:
            transfer.close()
        if artifact_evidence is not None:
            artifact_evidence.close()
        if authority is not None:
            authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
