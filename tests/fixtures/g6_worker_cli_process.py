"""Run the real worker CLI with owned synthetic native adapters.

Only hardware construction and presence are replaced. Enrollment, profile-file
loading, authority clocks/leases, registrations, HTTP, storage and shutdown use
the production CLI. This does not qualify a physical device or a second Mac.
"""
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from reproof.live import worker_cli
from reproof.live.model import check
from tests.fixtures.g6_worker_process import _profile, _read_configuration, _SyntheticPhysicalProvider


class PublicOutput:
    def __init__(self, stream):
        self.stream = stream
        self.pending = ""

    def write(self, value):
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line:
                document = json.loads(line)
                if "worker" in document:
                    document.update(sameMacProtocol=True, syntheticAdapter=True,
                                    actualWorkerCli=True, physicalDeviceAcceptance=False,
                                    twoMacAcceptance=False)
                self.stream.write(json.dumps(document) + "\n")
        return len(value)

    def flush(self):
        self.stream.flush()


def main():
    configuration = _read_configuration()
    root = Path(configuration["output"]).parent
    root.mkdir(parents=True, mode=0o700)
    profile = _profile(configuration["project"])
    documents = {
        "profile.json": profile.data,
        "registration.json": {"project": configuration["project"],
                              "collectionPolicy": configuration["collectionPolicy"]},
        "devices.json": {"schemaVersion": 1, "devices": [{
            "platform": "ios-physical", "deviceId": configuration["deviceAlias"],
            "profile": "profile.json", "products": "synthetic-products",
            "application": "synthetic.app"}]},
    }
    for name, document in documents.items():
        with (root / name).open("x") as stream:
            json.dump(document, stream)

    def synthetic_iphone(public_id, products, app, *, profile, authority_mode):
        check(public_id == configuration["deviceAlias"] and authority_mode == "shared-v2",
              "invalid_config", "Synthetic device configuration differs")
        return {
            "id": public_id, "name": "Owned synthetic CLI adapter", "platform": "ios",
            "kind": "ios-physical", "_authority": {
                "deviceKind": "ios-physical", "physicalId": configuration["physicalId"]},
            "capabilities": {"actions": ["tap"], "inputMode": "gesture-batch",
                             "media": "demo-svg", "authorityMode": "shared-v2",
                             "applicationIdentity": profile.application_identity,
                             "applicationProfile": profile.data,
                             "applicationProfileDigest": profile.digest,
                             "identityEvidence": "synthetic-protocol-only", "locatorKinds": []},
            "factory": _SyntheticPhysicalProvider,
        }

    credentials = io.StringIO(json.dumps({
        "enrollmentToken": configuration["enrollmentToken"],
        "transportToken": configuration["transportToken"]}))
    with patch("reproof.live.iphone.iphone_device", side_effect=synthetic_iphone), \
            patch.object(worker_cli, "connected_worker_devices",
                         side_effect=lambda devices: {device["id"] for device in devices}), \
            patch.object(sys, "stdin", credentials), \
            patch.object(sys, "stdout", PublicOutput(sys.stdout)):
        return worker_cli.main([
            "--port", "0", "--output", configuration["output"],
            "--authority-root", configuration["authorityRoot"],
            "--devices-config", str(root / "devices.json"),
            "--project-registration", str(root / "registration.json"),
            "--coordinator", configuration["coordinator"], "--enrollment-stdin",
            "--host-id", configuration["hostId"],
            "--host-incarnation", configuration["incarnation"],
            "--host-credential-output", str(root / "owned-host-credential.json"),
        ])


if __name__ == "__main__":
    raise SystemExit(main())
