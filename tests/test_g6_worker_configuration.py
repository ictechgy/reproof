import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.live.model import LiveError
from reproof.live import worker_cli
from tests.test_worker_profiles import android_document


class WorkerConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        devices = []
        for index, project in enumerate(("checkout", "catalog")):
            profile = copy.deepcopy(android_document())
            profile["id"] = "profile_" + project
            profile["projectId"] = project
            (self.root / (project + ".json")).write_text(json.dumps(profile))
            devices.append({"platform": "android", "deviceId": "owned-" + str(index),
                            "profile": project + ".json", "application": project + ".apk",
                            "helper": "helper.apk"})
        self.document = {"schemaVersion": 1, "devices": devices}
        self.path = self.root / "devices.json"

    def _configure(self):
        self.path.write_text(json.dumps(self.document))
        with patch("reproof.live.android_live.android_live_device") as factory:
            factory.side_effect = lambda serial, *_, **kw: {
                "id": "device_" + serial,
                "_authority": {"deviceKind": "android", "physicalId": serial},
                "profile": kw["runtime_profile"].data}
            result = worker_cli.configured_local_devices(self.path)
        return result

    def test_several_projects_on_one_platform_keep_separate_profiles(self):
        devices = self._configure()
        self.assertEqual([item["profile"]["projectId"] for item in devices], ["checkout", "catalog"])

    def test_duplicate_physical_device_cannot_receive_two_profiles(self):
        self.document["devices"][1]["deviceId"] = self.document["devices"][0]["deviceId"]
        with self.assertRaises(LiveError) as duplicate:
            self._configure()
        self.assertEqual(duplicate.exception.code, "duplicate_device")

    def test_imported_command_is_not_a_worker_configuration_field(self):
        self.document["devices"][0]["command"] = "untrusted-command"
        with self.assertRaises(LiveError):
            self._configure()
