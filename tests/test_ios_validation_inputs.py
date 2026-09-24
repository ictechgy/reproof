"""Focused inert iOS validation-input binding tests."""
import hashlib
import json
from pathlib import Path
import unittest

from reproof import contracts
from reproof.ios_mobile_inputs import LoadedIOSMobileInputs
from reproof.protected_validation_inputs import load_ios_validation_inputs
from reproof.validation import ValidationError
from tests.ios_service_support import IOSServiceFixture


class IOSValidationInputsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = IOSServiceFixture()
        self.addCleanup(self.fixture.close)
        self.source_digest = "a" * 64
        config = self.fixture.config
        self.mobile = LoadedIOSMobileInputs(
            "ios-profile", config, self.source_digest, config.snapshot(self.source_digest), ())
        self.plan = {
            "schemaVersion": 1,
            "id": "ios-validation",
            "projectDigest": config.registration.project_digest,
            "checks": [{"id": "external", "recipeId": "ios_external",
                         "kind": "external-observation", "evidenceSourceId": "ios-source"}],
            "candidateReports": "supplemental-only",
        }

    def _write_observers(self):
        path = Path(self.fixture.root) / "ios-observers.json"
        value = {"schemaVersion": 1, "kind": "unix-validation-observers-v1", "observers": [{
            "sourceId": "ios-source", "providerId": "ios-provider",
            "socketPath": str(Path(self.fixture.root) / "observer.sock"),
            "authenticationReferenceId": "ios-auth",
        }]}
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_loader_binds_loaded_ios_snapshot_without_connecting(self):
        reference = self._write_observers()
        inputs = load_ios_validation_inputs(reference, plan=self.plan,
                                            mobile_inputs=self.mobile)
        self.assertEqual(inputs.mobile_inputs, self.mobile)
        self.assertEqual(inputs.definition_digest, contracts.digest({
            "sourceDigest": reference["sha256"],
            "planDigest": contracts.digest(self.plan),
            "mobileDefinitionDigest": self.mobile.definition_digest,
        }))
        inputs.validate_binding(self.fixture.config)

    def test_changed_snapshot_is_refused(self):
        inputs = load_ios_validation_inputs(self._write_observers(), plan=self.plan,
                                            mobile_inputs=self.mobile)
        changed_mobile = LoadedIOSMobileInputs(
            self.mobile.profile_id, self.mobile.config, "b" * 64, "c" * 64, ())
        with self.assertRaises(ValidationError):
            type(inputs)(changed_mobile, inputs._plan, inputs._observers, inputs.source_digest).validate_binding(
                self.fixture.config)


if __name__ == "__main__":
    unittest.main()
