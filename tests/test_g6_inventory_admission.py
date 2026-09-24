import unittest

from reproof.live.model import LiveError
from tests import test_g6_inventory_authority as authority_tests


class InventoryAdmissionTests(unittest.TestCase):
    setUp = authority_tests.InventoryAuthorityTests.setUp
    enroll = authority_tests.InventoryAuthorityTests.enroll
    _authority = authority_tests.InventoryAuthorityTests._authority
    _document = authority_tests.InventoryAuthorityTests._document
    _claim = authority_tests.InventoryAuthorityTests._claim

    def _binding(self):
        return {"hostId": "worker-a", "generation": 1, "incarnation": "boot-a",
                "alias": "phone-a", "profileDigest": None}

    def test_missing_busy_and_expired_inventory_denies_admission(self):
        authority = self._authority()
        client, host = self.enroll("worker-a", "boot-a")
        inventory = self.fixture.server.inventory
        with self.assertRaises(LiveError):
            inventory.require_available(self._binding())
        client.refresh_inventory(host["credential"], self._document(authority))
        inventory.require_available(self._binding())
        claim = self._claim(authority)
        client.refresh_inventory(host["credential"], self._document(authority, "busy"))
        with self.assertRaises(LiveError):
            inventory.require_available(self._binding())
        self.assertTrue(claim.close())
        client.refresh_inventory(host["credential"], self._document(authority))
        inventory.require_available(self._binding())
        now = inventory._clock()
        inventory._clock = lambda: now + 20_000
        with self.assertRaises(LiveError):
            inventory.require_available(self._binding())

    def test_forged_host_or_profile_cannot_adopt_an_available_alias(self):
        authority = self._authority()
        client, host = self.enroll("worker-a", "boot-a")
        client.refresh_inventory(host["credential"], self._document(authority))
        for field, value in (("incarnation", "boot-b"), ("generation", 2),
                             ("profileDigest", "f" * 64), ("alias", "phone-b")):
            binding = self._binding()
            binding[field] = value
            with self.subTest(field=field), self.assertRaises(LiveError):
                self.fixture.server.inventory.require_available(binding)

    def test_bare_available_state_is_not_qualified_for_scheduling(self):
        client, host = self.enroll("worker-a", "boot-a")
        value = authority_tests.document(1, "boot-a")
        client.refresh_inventory(host["credential"], value)
        binding = self._binding()
        binding["profileDigest"] = "a" * 64
        with self.assertRaises(LiveError):
            self.fixture.server.inventory.require_available(binding)

    def test_coordinator_list_and_session_admission_use_the_same_inventory(self):
        lab = self.fixture.lab
        lab.devices["shared-device"]["_inventoryBinding"] = self._binding()
        self.assertEqual(lab.list_devices()[0]["state"], "uncertain")
        with self.assertRaises(LiveError) as denied:
            lab.create_session("shared-device", "owner", "controller")
        self.assertEqual(denied.exception.code, "inventory_unavailable")
        self.assertEqual(lab.sessions, {})
        authority = self._authority()
        client, host = self.enroll("worker-a", "boot-a")
        client.refresh_inventory(host["credential"], self._document(authority))
        self.assertEqual(lab.list_devices()[0]["state"], "available")

    def test_worker_presence_never_clears_an_existing_quarantine(self):
        lab = self.fixture.lab
        lab.apply_device_presence(set())
        self.assertEqual(lab.list_devices()[0]["state"], "missing")
        with self.assertRaises(LiveError):
            lab.create_session("shared-device", "owner", "controller")
        lab.devices["shared-device"]["state"] = "quarantined"
        lab.apply_device_presence({"shared-device"})
        self.assertEqual(lab.list_devices()[0]["state"], "quarantined")
