"""Fake-only public Android startup diagnostics and authority cleanup tests."""
from pathlib import Path
import tempfile
import unittest

from reproof.core import ContractError
from reproof.live.authority import HostAuthority
from reproof.live.model import ANDROID_STARTUP_DIAGNOSTICS, Lab, LiveError
from tests.test_live_authority_integration import Clock, FencedProvider, parent_grant


class _FailingProvider(FencedProvider):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.aborted = False

    def start_authorized(self, session, lab, permit):
        self.device_authority.check_dispatch_permit(permit)
        self.permits.append(('startup', permit))
        raise self.error

    def abort(self):
        self.aborted = True


class AndroidStartupModelTests(unittest.TestCase):
    def exercise_failure(self, error, expected_code=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = Clock()
            authority = HostAuthority(root / 'authority.sqlite3', clock=clock,
                                      lease_directory=root / 'leases')
            provider = _FailingProvider(error)
            descriptor = {
                'id': 'fake-android', 'name': 'Fake Android',
                'platform': 'android', 'kind': 'android-live',
                'capabilities': {'actions': ['tap'], 'inputMode': 'gesture-batch'},
                'factory': lambda: provider,
                '_authority': {'deviceKind': 'android', 'physicalId': 'fake-physical'},
            }
            lab = Lab([descriptor], root / 'live', authority=authority,
                      parent_grant=parent_grant(authority, clock))
            try:
                public = lab.create_session('fake-android', 'owner', 'browser')
                sid = public['id']
                private = lab._session(sid)
                self.assertEqual(public['state'], 'failed')
                self.assertEqual(public['error'], ANDROID_STARTUP_DIAGNOSTICS[expected_code]
                                 if expected_code else 'Provider could not start')
                self.assertNotIn('private-secret', repr(public))
                self.assertEqual(public.get('errorCode'), expected_code)
                self.assertEqual(lab.list_devices()[0]['state'], 'quarantined')
                self.assertTrue(private['cancel'].is_set())
                self.assertEqual(provider.device_authority.status, 'quarantined')
                permit = provider.permits[0][1]
                with self.assertRaises(ContractError):
                    provider.device_authority.check_dispatch_permit(permit)
                self.assertEqual(authority.store.operation(permit.operation_id)['status'], 'uncertain')
                if expected_code:
                    lab.fail(sid, 'late generic poller failure')
                    latest = lab.peek_session(sid)
                    self.assertEqual(latest['errorCode'], expected_code)
                    self.assertEqual(latest['error'], ANDROID_STARTUP_DIAGNOSTICS[expected_code])
                # Uncertain startup still cannot obtain a cleanup permit;
                # the existing abort/quarantine path remains unchanged.
                closed = lab.close_session(sid, 'owner')
                self.assertEqual(closed['state'], 'failed')
                self.assertEqual(closed['error'], 'Provider cleanup failed')
                self.assertNotIn('errorCode', closed)
                self.assertTrue(provider.aborted)
                self.assertFalse(provider.closed)
                self.assertEqual(lab.list_devices()[0]['state'], 'quarantined')
                self.assertEqual([kind for kind, _ in provider.permits], ['startup'])
            finally:
                lab.close_all()
                authority.close()

    def test_all_allowlisted_failures_preserve_fixed_text_and_cleanup(self):
        for code in ANDROID_STARTUP_DIAGNOSTICS:
            with self.subTest(code=code):
                self.exercise_failure(LiveError(code, 'private-secret native output'), code)

    def test_unknown_exceptions_and_codes_stay_redacted(self):
        for error in (RuntimeError('private-secret'),
                      LiveError('unknown_private_code', 'private-secret'),
                      ContractError('private-secret')):
            with self.subTest(error_type=type(error).__name__):
                self.exercise_failure(error)


if __name__ == '__main__':
    unittest.main()
