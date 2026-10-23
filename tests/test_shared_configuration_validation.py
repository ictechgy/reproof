"""Shared public configuration validation must precede runtime initialization."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof.core import ContractError
from reproof.live.configuration import load_shared_configuration
from reproof.storage import read_json


class SharedConfigurationValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'shared.json'
        self.document = {
            'schemaVersion': 2,
            'kind': 'reproof-shared-coordinator',
            'stateRoot': str(self.root / 'coordinator-v2'),
            'listen': {
                'host': '127.0.0.1', 'port': 9443,
                'origin': 'http://127.0.0.1:9443',
                'tlsCertificateFile': None, 'tlsPrivateKeyFile': None,
            },
            'projects': [{
                'projectFile': str(self.root / 'project.json'),
                'collectionPolicyFile': str(self.root / 'policy.json'),
            }],
            'browserSessionSeconds': 900,
        }

    def load(self, **listener):
        document = deepcopy(self.document)
        document['listen'].update(listener)
        self.path.write_text(json.dumps(document), encoding='utf-8')
        return load_shared_configuration(self.path)

    def test_valid_loopback_and_remote_listeners_preserve_configuration(self):
        cases = (
            ('127.0.0.1', 'http://127.0.0.1:9443', False),
            ('127.0.0.1', 'http://localhost:9443/', False),
            ('localhost', 'http://localhost:9443', False),
            ('::1', 'http://[::1]:9443/', False),
            ('127.0.0.1', 'https://127.0.0.1:9443', True),
            ('::1', 'https://[::1]:9443', True),
            ('192.0.2.10', 'https://192.0.2.10:9443', True),
            ('2001:db8::1', 'https://[2001:db8::1]:9443', True),
            ('coordinator.example.internal', 'https://coordinator.example.internal:9443', True),
            ('0.0.0.0', 'https://coordinator.example.internal:9443', True),
            ('::', 'https://[2001:db8::1]:9443', True),
        )
        for host, origin, tls in cases:
            with self.subTest(host=host, origin=origin):
                loaded = self.load(host=host, origin=origin,
                    tlsCertificateFile='tls/server.crt' if tls else None,
                    tlsPrivateKeyFile='tls/server.key' if tls else None)
                self.assertEqual(loaded.host, host)
                self.assertEqual(loaded.origin, origin.rstrip('/'))
                self.assertEqual(loaded.port, 9443)
                self.assertEqual(loaded.tls_certificate_file,
                                 Path('tls/server.crt').absolute() if tls else None)
                self.assertEqual(loaded.tls_private_key_file,
                                 Path('tls/server.key').absolute() if tls else None)
                self.assertEqual(loaded.state_root, self.root / 'coordinator-v2')
                self.assertFalse(loaded.state_root.exists())

    def test_invalid_origin_types_are_contract_errors(self):
        for origin in (None, False, True, 1, 1.5, [], ['http://127.0.0.1:9443'], {}, ''):
            with self.subTest(origin=origin), self.assertRaises(ContractError):
                self.load(origin=origin)

    def test_malformed_urls_are_contract_errors(self):
        origins = (
            'http://[::1:9443', 'http://[invalid]:9443',
            'http://127.0.0.1:not-a-port', 'http://127.0.0.1:65536',
            'http://127.0.0.1:-1', 'http://127.0.0.1',
            'http://@127.0.0.1:9443', 'http://:secret@127.0.0.1:9443',
            'http://user@127.0.0.1:9443',
            'http://127.0.0.1:9443/path', 'http://127.0.0.1:9443?query=1',
            'http://127.0.0.1:9443#fragment', 'ftp://127.0.0.1:9443',
        )
        for origin in origins:
            with self.subTest(origin=origin), self.assertRaises(ContractError):
                self.load(origin=origin)

    def test_origin_whitespace_and_controls_are_not_silently_normalized(self):
        for origin in (' http://127.0.0.1:9443', 'http://127.0.0.1:9443 ',
                       '\nhttp://127.0.0.1:9443', 'http://127.0.0.1:\t9443',
                       '\0http://127.0.0.1:9443', 'http://127.0.0.1:9443\x7f',
                       'http://127.0.0.1:9443\u00a0'):
            with self.subTest(origin=origin), self.assertRaises(ContractError):
                self.load(origin=origin)

    def test_invalid_listener_hosts_are_contract_errors(self):
        for host in (None, False, 1, [], {}, '', 'bad host', ' localhost',
                     'localhost\n', 'local\thost', 'host\0name', 'host\x7fname',
                     'host/name', 'host\\name', 'host\u00a0name'):
            with self.subTest(host=host), self.assertRaises(ContractError):
                self.load(host=host)

    def test_whitespace_host_is_rejected_even_when_origin_matches(self):
        for host in ('bad host', 'bad\u00a0host'):
            with self.subTest(host=host), self.assertRaises(ContractError):
                self.load(host=host, origin=f'https://{host}:9443',
                    tlsCertificateFile='/not-opened/server.crt',
                    tlsPrivateKeyFile='/not-opened/server.key')

    def test_invalid_tls_path_types_and_values_are_contract_errors(self):
        for field in ('tlsCertificateFile', 'tlsPrivateKeyFile'):
            for value in (False, True, 1, 1.5, [], {}, '', '\0file'):
                listener = {'origin': 'https://127.0.0.1:9443',
                            'tlsCertificateFile': '/not-opened/server.crt',
                            'tlsPrivateKeyFile': '/not-opened/server.key'}
                listener[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    self.load(**listener)

    def test_tls_paths_must_be_paired_and_match_origin_scheme(self):
        cases = (
            {'tlsCertificateFile': '/not-opened/server.crt'},
            {'tlsPrivateKeyFile': '/not-opened/server.key'},
            {'tlsCertificateFile': '/not-opened/server.crt',
             'tlsPrivateKeyFile': '/not-opened/server.key'},
            {'origin': 'https://127.0.0.1:9443'},
            {'host': '::1', 'origin': 'https://[::1]:9443'},
            {'host': '192.0.2.10', 'origin': 'http://192.0.2.10:9443'},
        )
        for listener in cases:
            with self.subTest(listener=listener), self.assertRaises(ContractError):
                self.load(**listener)

    def test_existing_listener_origin_consistency_checks_remain(self):
        for host, origin in (
            ('127.0.0.1', 'http://127.0.0.2:9443'),
            ('127.0.0.1', 'http://127.0.0.1:9444'),
            ('0.0.0.0', 'https://0.0.0.0:9443'),
            ('::', 'https://[::]:9443'),
            ('::', 'https://localhost:9443'),
        ):
            with self.subTest(host=host, origin=origin), self.assertRaises(ContractError):
                self.load(host=host, origin=origin,
                    tlsCertificateFile='/not-opened/server.crt' if origin.startswith('https:') else None,
                    tlsPrivateKeyFile='/not-opened/server.key' if origin.startswith('https:') else None)

    def test_loader_reads_only_public_configuration_without_runtime_effects(self):
        self.document['listen'].update(origin='https://127.0.0.1:9443',
            tlsCertificateFile=str(self.root / 'server.crt'),
            tlsPrivateKeyFile=str(self.root / 'server.key'))
        self.path.write_text(json.dumps(self.document), encoding='utf-8')
        before = self.path.read_bytes()
        with (patch('reproof.live.configuration.read_json', wraps=read_json) as read,
              patch('reproof.live.configuration.AccessStore', side_effect=AssertionError('store initialized')),
              patch('reproof.live.model.Lab', side_effect=AssertionError('lab initialized')),
              patch('ssl.SSLContext', side_effect=AssertionError('TLS initialized')),
              patch('socket.socket', side_effect=AssertionError('socket opened')),
              patch('subprocess.Popen', side_effect=AssertionError('process started'))):
            loaded = load_shared_configuration(self.path)
        read.assert_called_once_with(self.path.absolute())
        self.assertEqual(loaded.project_files,
                         ((self.root / 'project.json', self.root / 'policy.json'),))
        self.assertEqual(list(self.root.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), before)

    def test_expected_read_failures_are_sanitized_contract_errors(self):
        for error in (FileNotFoundError('private detail'), PermissionError('private detail'),
                      ContractError('private detail')):
            with self.subTest(error=type(error).__name__), \
                 patch('reproof.live.configuration.read_json', side_effect=error), \
                 self.assertRaisesRegex(ContractError, '^Shared coordinator configuration could not be loaded$'):
                load_shared_configuration(self.path)

    def test_unexpected_read_failures_and_interrupts_propagate(self):
        for error in (RuntimeError('unexpected failure'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), \
                 patch('reproof.live.configuration.read_json', side_effect=error), \
                 self.assertRaises(type(error)) as raised:
                load_shared_configuration(self.path)
            self.assertIs(raised.exception, error)


if __name__ == '__main__':
    unittest.main()
