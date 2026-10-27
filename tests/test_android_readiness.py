import base64
import copy
import json
import unittest
from reproof.android_profile import validate_android_runtime_profile
from reproof.android_readiness import validate_android_fixture, require_android_fixture
from reproof.core import ContractError
from tests.test_worker_profiles import android_document


def fixture():
    document = android_document()
    document['approvedReferences']['preparations'] = ['qa_seed']
    document['capabilities']['observations'].remove('logs')
    document['capabilities']['logAdapter'] = None
    profile = validate_android_runtime_profile(document)
    value = {'schemaVersion': 1, 'kind': 'reproof-approved-android-fixture',
             'referenceId': 'qa_seed', 'profileDigest': profile.digest,
             'artifactDigest': profile.data['artifact']['sha256'],
             'fixtureId': 'qa_home', 'fixtureVersion': 1, 'policyDigest': 'a' * 64,
             'helperSignerDigest': 'b' * 64, 'launchConfigurationDigest': None}
    return profile, value


class AndroidFixtureSelectionTests(unittest.TestCase):
    def test_selection_is_closed_immutable_and_needs_no_numeric_or_edit(self):
        profile, value = fixture()
        selection = validate_android_fixture(value, profile)
        value['fixtureId'] = 'other'
        self.assertEqual(selection.data['fixtureId'], 'qa_home')
        self.assertEqual(require_android_fixture(selection, profile).digest, selection.digest)
        self.assertEqual(json.loads(base64.b64decode(selection.native()['configurationBase64'])), selection.data)
        self.assertNotIn('qa_home', repr(selection))

    def test_rejects_unknown_paths_commands_and_unapproved_or_mistyped_fields(self):
        profile, value = fixture()
        for key in ('path', 'command', 'url', 'oracle', 'edit', 'callback', 'reset'):
            bad = dict(value, **{key: 'anything'})
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_android_fixture(bad, profile)
        changes = [('schemaVersion', True), ('fixtureVersion', True), ('fixtureVersion', 0),
                   ('fixtureVersion', 1001), ('fixtureId', '../x'), ('referenceId', 'unapproved'),
                   ('profileDigest', 'c' * 64), ('artifactDigest', 'c' * 64),
                   ('policyDigest', '0' * 64), ('helperSignerDigest', '0' * 64),
                   ('helperSignerDigest', []),
                   ('helperSignerDigest', 'a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc'),
                   ('launchConfigurationDigest', 'd' * 64)]
        for key, item in changes:
            bad = copy.deepcopy(value);bad[key] = item
            with self.subTest(key=key, value=item), self.assertRaises(ContractError):
                validate_android_fixture(bad, profile)

    def test_changed_full_profile_or_untyped_json_cannot_be_selected(self):
        profile, value = fixture()
        selection = validate_android_fixture(value, profile)
        changed = profile.data;changed['buildId'] = 'different_build'
        with self.assertRaises(ContractError):
            require_android_fixture(selection, validate_android_runtime_profile(changed))
        with self.assertRaises(ContractError):
            require_android_fixture(value, profile)


class AndroidFixtureProviderTests(unittest.TestCase):
    def provider(self):
        import threading
        from unittest.mock import Mock
        from reproof.live.android_live import AndroidLiveProvider
        from reproof import contracts
        profile, config = fixture()
        provider = object.__new__(AndroidLiveProvider)
        provider.fixture_config = validate_android_fixture(config, profile)
        provider._fixture_guard = Mock()
        provider._fixture_native_command = Mock()
        provider.stop = threading.Event()
        provider._fixture_challenge = {'nonce': 'c' * 64, 'sessionId': 'session_test'}
        operation = {'operationId': 'operation_test', 'allocationId': 'allocation_test',
                     'generation': 1, 'idempotencyKey': 'd' * 64, 'operation': 'prepare',
                     'recipeId': 'qa_home', 'payloadDigest': contracts.digest({})}
        receipt = {'challenge': copy.deepcopy(provider._fixture_challenge),
                   'operation': copy.deepcopy(operation), 'phase': 'prepare',
                   'status': 'complete', 'quiescent': True}
        provider._fixture_status = Mock(return_value={'fixtureReceipts': [receipt],
            'fixturePending': False, 'fixtureQuiescent': True, 'fixtureCancelled': False})
        return provider, operation, receipt

    def test_exact_native_terminal_is_required_and_status_never_redispatches(self):
        provider, operation, receipt = self.provider()
        result = provider.fixture_operation(operation, provider._fixture_challenge, 1)
        self.assertEqual(result, receipt)
        provider._fixture_native_command.assert_called_once()
        result['operation']['generation'] = 99
        self.assertEqual(receipt['operation']['generation'], 1)
        provider._fixture_native_command.reset_mock()
        self.assertEqual(provider.fixture_operation(operation, provider._fixture_challenge, 1,
                                                   status_only=True), receipt)
        provider._fixture_native_command.assert_not_called()

    def test_changed_challenge_rejected_before_effect(self):
        from reproof.live.model import LiveError
        provider, operation, _ = self.provider()
        challenge = dict(provider._fixture_challenge, nonce='e' * 64)
        with self.assertRaises(LiveError):
            provider.fixture_operation(operation, challenge, 1)
        provider._fixture_native_command.assert_not_called()

    def test_wrong_phase_boolean_generation_duplicate_and_unquiescent_rejected(self):
        from reproof.live.model import LiveError
        variants = [lambda r: r.update(phase='check'),
                    lambda r: r['operation'].update(generation=True),
                    lambda r: r.update(quiescent=False),
                    lambda r: r['challenge'].update(nonce='f' * 64),
                    lambda r: r.update(status='ready')]
        for mutate in variants:
            provider, operation, receipt = self.provider();mutate(receipt)
            with self.subTest(mutate=mutate), self.assertRaises(LiveError):
                provider.fixture_operation(operation, provider._fixture_challenge, 1, status_only=True)
        provider, operation, receipt = self.provider()
        provider._fixture_status.return_value['fixtureReceipts'].append(copy.deepcopy(receipt))
        with self.assertRaises(LiveError):
            provider.fixture_operation(operation, provider._fixture_challenge, 1, status_only=True)

    def test_no_receipt_is_not_success_or_stopped(self):
        from reproof.live.model import LiveError
        provider, operation, _ = self.provider()
        provider._fixture_status.return_value['fixtureReceipts'] = []
        with self.assertRaises(LiveError):
            provider.fixture_operation(operation, provider._fixture_challenge, .001, status_only=True)
        provider._fixture_status.return_value.update(fixtureQuiescent=False, fixturePending=False)
        self.assertFalse(provider.quiesce_android_preparation(.001))

    def test_disconnected_status_is_not_quiescence(self):
        provider, _, _ = self.provider()
        provider._fixture_status.side_effect = OSError('disconnected')
        with self.assertRaises(OSError):
            provider.quiesce_android_preparation(1)

    def test_artifact_recheck_rejects_nonfixed_paths_without_hash_command(self):
        from unittest.mock import Mock
        from reproof.live.model import LiveError
        provider, _, _ = self.provider()
        provider.target_package = 'io.example.app'
        provider.device = Mock()
        provider.device.shell.return_value = 'package:/data/app/evil;command/base.apk'
        with self.assertRaises(LiveError):
            provider._verify_fixture_installed_artifact()
        provider.device.shell.assert_called_once_with('pm', 'path', provider.target_package)

    def test_successful_ready_state_still_requires_cancel_barrier(self):
        provider, _, _ = self.provider()
        ready = {'fixtureReceipts': [], 'fixturePending': False,
                 'fixtureQuiescent': True, 'fixtureCancelled': False}
        cancelled = dict(ready, fixtureCancelled=True)
        provider._fixture_status.side_effect = [ready, cancelled]
        self.assertTrue(provider.quiesce_android_preparation(1))
        args = provider._fixture_native_command.call_args.args
        self.assertEqual(args[0], 'fixture_cancel')
        self.assertEqual(args[1]['challenge'], provider._fixture_challenge)
        self.assertEqual(provider._fixture_status.call_count, 2)

    def test_generic_helper_false_is_unknown_not_no_effect_rejection(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock
        from reproof.live.android_live import AndroidLiveProvider
        from reproof.live.model import LiveError
        provider, _, _ = self.provider()
        # Exercise the actual command admission, not the convenience mock.
        del provider._fixture_native_command
        provider._fixture_session = {'lock': threading.RLock()}
        provider.lab = Mock()
        provider.lab._prepare_effect.return_value = SimpleNamespace(operation_id='operation_1')
        provider._check_permit = Mock()
        provider.native_handshake = object()
        provider.device_authority = Mock()
        grant = {'operationId': 'operation_1', 'ownershipGeneration': 1}
        provider.device_authority.native_grant.return_value.wire.return_value = grant
        provider.transport = Mock()
        provider.transport.call.side_effect = [
            {'accepted': True}, {'pending': False, 'id': 'operation_1', 'ok': False,
                'timing': 'best-effort', 'authority': grant, 'error': 'input_failed'}]
        with self.assertRaises(LiveError) as raised:
            AndroidLiveProvider._fixture_native_command(provider, 'fixture_prepare', {}, 1)
        self.assertEqual(raised.exception.code, 'fixture_unknown')
        provider.lab._confirm_effect.assert_not_called()

    def test_pre_admission_frame_is_not_read_or_consumed(self):
        import threading
        from unittest.mock import Mock
        from reproof.live.android_live import AndroidLiveProvider
        provider, _, _ = self.provider()
        provider._fixture_session = {'lock': threading.RLock(), '_androidAdmissionOpen': False}
        provider.transport = Mock()
        provider.last_native_frame = 0
        AndroidLiveProvider._receive_frame(provider)
        provider.transport.call.assert_not_called()
        self.assertEqual(provider.last_native_frame, 0)

    def test_real_authority_can_observe_its_unconfirmed_fixture_ack(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from reproof.live.android_live import AndroidLiveProvider
        from reproof.live.android_preparation import AndroidPreparation
        from tests.test_live_authority_integration import LiveAuthorityIntegrationTests
        case = LiveAuthorityIntegrationTests()
        case.setUp()
        session = None
        try:
            public = case.session()
            session = case.lab._session(public['id'])
            handle = session['deviceAuthority']
            context = object.__new__(AndroidPreparation)
            session['_androidPreparation'] = context
            profile, config = fixture()
            provider = object.__new__(AndroidLiveProvider)
            provider.fixture_config = validate_android_fixture(config, profile)
            provider._fixture_context = context
            provider._fixture_session = session
            provider.device_authority = handle
            provider.stop = threading.Event()
            provider.process = Mock();provider.process.poll.return_value = None
            provider.lab = case.lab
            provider.native_handshake = object()
            grant = {'operationId': 'wire_test', 'ownershipGeneration': handle.generation}
            command = {}
            def call(path, value=None, **kwargs):
                if path == '/command':
                    command.update(value)
                    with self.assertRaises(ContractError):handle.check_ownership()
                    handle.check_observation_authority()
                    return {'accepted': True}
                self.assertEqual(path, '/ack/' + command['id'])
                return {'pending': False, 'id': command['id'], 'ok': True,
                        'timing': 'best-effort', 'authority': grant}
            provider.transport = Mock();provider.transport.call.side_effect = call
            with patch.object(handle, 'native_grant', return_value=SimpleNamespace(wire=lambda: grant)):
                provider._fixture_native_command('fixture_prepare', {}, 1)
            handle.check_ownership()
        finally:
            if session is not None:session.pop('_androidPreparation', None)
            case.tearDown()
