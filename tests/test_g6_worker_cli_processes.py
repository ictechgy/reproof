"""Two actual CLI processes with explicit owned synthetic hardware adapters."""
from tests import test_worker_recovery as worker_tests
from unittest.mock import patch
import uuid


class ActualWorkerCliProcessTests(worker_tests.TwoWorkerProcessIntegrationTests):
    worker_fixture = "tests/fixtures/g6_worker_cli_process.py"
    actual_worker_cli = True

    def test_another_cli_process_cannot_register_the_same_physical_identity(self):
        self.physical_override = 'owned-duplicate-' + uuid.uuid4().hex
        first, _ = self._start_worker('one')
        inventory = self.fixture.server.inventory
        refresh = inventory.refresh
        rejected = []

        def observe(host, document):
            try:
                return refresh(host, document)
            except Exception as error:
                if host.host_id == 'worker-two':
                    rejected.append(getattr(error, 'code', None))
                raise

        with patch.object(inventory, 'refresh', side_effect=observe):
            self._start_worker('two', expected_error='worker_configuration_failed')
        self.assertEqual(rejected, ['duplicate_device'])
        self.assertEqual(len(first.call('/v1/devices')['devices']), 1)
        self.assertEqual(inventory.list_host('worker-two'), [])
