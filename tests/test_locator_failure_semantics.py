"""Unsupported locator contracts differ from uncertain native observations."""
import copy
import unittest
from unittest import mock
from types import SimpleNamespace

from reproof.live.android_live import AndroidLiveProvider
from reproof.live.model import LiveError
from tests.g4_support import G4Environment, ScenarioProvider


class LocatorFailureSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.env = G4Environment()
        self.addCleanup(self.env.close)

    def lookup(self, failure=None, *, missing_resolver=False, target=None):
        handle = self.env.service.start_prepared_recording(
            device_id='device', owner='owner', controller_id='locator',
            registration=self.env.registration, application_id='ios_app', build_id='original',
            preparations=self.env.preparations())
        patch = (mock.patch.object(ScenarioProvider, 'resolve_locator', None) if missing_resolver
                 else mock.patch.object(ScenarioProvider, 'resolve_locator', side_effect=failure))
        with patch:
            with self.assertRaises(LiveError) as caught:
                self.env.lab.resolve_locator(handle.session_id, 'owner',
                    target or {'kind':'accessibility-id','value':'checkout'})
        return caught.exception

    def test_provider_unsupported_is_preserved_without_provider_message(self):
        error = self.lookup(LiveError('unsupported_operation', 'private-target-value', 400))
        self.assertEqual((error.code,error.status),('unsupported_operation',400))
        self.assertNotIn('private-target-value',str(error))
        self.assertEqual(self.env.control['calls'],[])

    def test_other_provider_codes_remain_unknown_without_provider_message(self):
        error = self.lookup(LiveError('invalid_argument', 'private-target-value', 400))
        self.assertEqual((error.code,error.status),('observation_failed',409))
        self.assertNotIn('private-target-value',str(error))

    def test_native_observation_failure_stays_unknown_and_sanitized(self):
        error = self.lookup(LiveError('observation_failed','private native response',503))
        self.assertEqual((error.code,error.status),('observation_failed',409))
        self.assertNotIn('private native response',str(error))

    def test_unexpected_provider_failure_stays_unknown_and_sanitized(self):
        error = self.lookup(RuntimeError('private native response'))
        self.assertEqual((error.code,error.status),('observation_failed',409))
        self.assertNotIn('private native response',str(error))

    def android_rejection(self, target):
        # Use the real provider's declaration guard, without initialization,
        # an ADB client, a native helper, or any actual device resource.
        provider = object.__new__(AndroidLiveProvider)
        provider.general_profile = True
        provider.app_profile = SimpleNamespace(data={'capabilities':{'locator':{'targets':['registered']}}})
        provider.transport = mock.Mock()
        def unsupported(request):
            self.assertEqual(request, target)
            return provider.resolve_locator(request)
        error = self.lookup(unsupported, target=target)
        self.assertEqual((error.code,error.status),('unsupported_operation',400))
        provider.transport.call.assert_not_called()
        self.assertEqual(self.env.control['calls'],[])

    def test_android_unregistered_locator_is_rejected_before_transport(self):
        self.android_rejection({'kind':'resource-id','value':'unregistered'})

    def test_android_role_is_rejected_before_transport(self):
        self.android_rejection({'kind':'resource-id','value':'registered','role':'button'})

    def test_android_ancestor_is_rejected_before_transport(self):
        self.android_rejection({'kind':'resource-id','value':'registered',
                                'ancestor':{'kind':'resource-id','value':'parent'}})

    def test_missing_remote_resolver_remains_unsupported(self):
        error = self.lookup(missing_resolver=True)
        self.assertEqual((error.code,error.status),('unsupported_operation',400))
        self.assertEqual(self.env.control['calls'],[])

    def replay(self, *, missing_capability=False, failure=None):
        original = self.env.record_original()
        approved = self.env.approve(original)
        immutable = copy.deepcopy(approved.specification)
        calls = copy.deepcopy(self.env.control['calls'])
        if missing_capability:
            self.env.lab.devices['device']['capabilities']['locatorKinds'] = []
        execution = self.env.registry.original_execution(approved)
        with mock.patch.object(ScenarioProvider,'resolve_locator',side_effect=failure):
            result = self.env.service.replay(execution, registration=self.env.registration,
                device_id='device',owner='owner',controller_id='replay',preparations=self.env.preparations())
        self.assertEqual(self.env.control['calls'],calls)
        self.assertEqual(approved.specification,immutable)
        self.assertEqual(result.public()['cleanup'],'complete')
        self.assertEqual(result.public()['receipts'],[])
        return result.public()

    def test_missing_platform_capability_is_failed_without_dispatch(self):
        self.assertEqual(self.replay(missing_capability=True)['verdict'],'failed')

    def test_declared_but_unsupported_locator_is_failed_without_dispatch(self):
        failure=LiveError('unsupported_operation','Unsupported target shape',400)
        self.assertEqual(self.replay(failure=failure)['verdict'],'failed')

    def test_observation_transport_error_is_unknown_without_dispatch(self):
        self.assertEqual(self.replay(failure=OSError('private transport'))['verdict'],'unknown')
