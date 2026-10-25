"""Portable local SDK review: fixed siblings, bounded race checks, no effects."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reproof import cli, sdk_review_cli
from reproof.sdk_review import MAX_BYTES, ReviewInvalid, export_digest, normalize_export
from reproof.sdk_review_demo import sample_export

ROOT = Path(__file__).resolve().parents[1]
CANARY = 'SECRET_CANARY_path_token_and_raw_event'
ERROR = {'status': 'sdk_export_invalid'}


class SDKReviewCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Test fixtures use the actual temp directory, including on macOS where
        # the OS temp directory spelling can itself contain a symlink.
        self.root = Path(self.temp.name).resolve()

    def session(self, platform='android', *, name=CANARY):
        directory = self.root / name
        directory.mkdir(parents=True)
        files = sample_export(platform)
        names = {'capture': 'capture.json', 'metadata': 'session.json' if platform == 'android'
                 else 'metadata.json', 'marker': 'finalized.json'}
        for key, filename in names.items():
            if files[key] is not None:
                # Preserve noncanonical but valid exact-byte identity.
                files[key] = b' \n' + files[key] + b'\n\t'
                (directory / filename).write_bytes(files[key])
        return directory, files

    def run_cli(self, *args, dispatch=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = (cli.main(['sdk-review', *map(str, args)]) if dispatch else
                        sdk_review_cli.main(list(map(str, args))))
            except SystemExit as exc:
                code = exc.code
        self.assertNotIn(CANARY, out.getvalue() + err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def invoke(self, directory, platform='android'):
        result = subprocess.run([sys.executable, '-B', '-m', 'reproof', 'sdk-review',
            '--platform', platform, '--session-directory', str(directory)], cwd=ROOT,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        return result

    def rejected(self, directory, platform='android'):
        code, out, err = self.run_cli('--platform', platform, '--session-directory', directory)
        self.assertEqual((code, json.loads(out), err), (2, ERROR, ''))

    def test_both_real_file_layouts_print_only_summary_and_preserve_inputs(self):
        for platform in ('android', 'ios'):
            with self.subTest(platform=platform):
                directory, files = self.session(platform, name=platform + CANARY)
                (directory / 'events.jsonl').write_text(CANARY)
                before = {p.name: (p.read_bytes(), sdk_review_cli._version(p.stat()))
                          for p in directory.iterdir()}
                result = self.invoke(directory, platform)
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                envelope = normalize_export(**files)
                self.assertEqual(json.loads(result.stdout), envelope.summary())
                for private in (envelope._digest, envelope._session, 'parameters', 'startedAtMs'):
                    self.assertNotIn(private, result.stdout + result.stderr)
                self.assertEqual({p.name: (p.read_bytes(), sdk_review_cli._version(p.stat()))
                                  for p in directory.iterdir()}, before)
                self.assertEqual(set(self.root.iterdir()),
                                 {self.root / (p + CANARY) for p in ('android', 'ios')
                                  if (self.root / (p + CANARY)).exists()})

    def test_exact_raw_bytes_and_private_digest_reach_normalizer(self):
        for platform in ('android', 'ios'):
            directory, files = self.session(platform, name=platform)
            with patch.object(sdk_review_cli, 'normalize_export', wraps=normalize_export) as normalize:
                envelope = sdk_review_cli.review_session(platform=platform, session_directory=directory)
            normalize.assert_called_once_with(**files)
            self.assertEqual(envelope._digest, export_digest(**files))
            changed = dict(files, capture=files['capture'] + b' ')
            self.assertNotEqual(envelope._digest, export_digest(**changed))

    def test_extra_siblings_are_never_opened_listed_or_recursed(self):
        for kind in ('fifo', 'symlink', 'directory'):
            with self.subTest(kind=kind):
                directory, _ = self.session(name=kind)
                extra = directory / 'events.jsonl'
                if kind == 'fifo':
                    os.mkfifo(extra)
                elif kind == 'symlink':
                    extra.symlink_to(self.root / CANARY)
                else:
                    extra.mkdir(); (extra / CANARY).write_text(CANARY)
                real_open = os.open
                def opened(name, flags, *args, **kwargs):
                    self.assertNotIn(str(name), ('events.jsonl', CANARY))
                    return real_open(name, flags, *args, **kwargs)
                with patch.object(sdk_review_cli.os, 'open', side_effect=opened), \
                     patch.object(os, 'listdir', side_effect=AssertionError('must not list')), \
                     patch.object(os, 'scandir', side_effect=AssertionError('must not recurse')):
                    code, out, err = self.run_cli('--platform', 'android', '--session-directory', directory)
                self.assertEqual((code, json.loads(out)['status'], err), (0, 'review-only', ''))

    def test_open_all_required_regular_files_before_reading_with_safe_flags(self):
        directory, _ = self.session('ios')
        real_open, real_read = os.open, os.read
        required = {'capture.json', 'metadata.json', 'finalized.json'}
        opened_files, descriptors = set(), []
        def opened(name, flags, *args, **kwargs):
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC), 0)
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NONBLOCK)
            if name in required:
                self.assertIsInstance(kwargs['dir_fd'], int)
                opened_files.add(name)
            descriptor = real_open(name, flags, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        def read(descriptor, amount):
            self.assertEqual(opened_files, required)
            self.assertLessEqual(amount, MAX_BYTES + 1)
            return real_read(descriptor, amount)
        with patch.object(os, 'open', side_effect=opened), patch.object(os, 'read', side_effect=read):
            sdk_review_cli.review_session(platform='ios', session_directory=directory)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_partial_reads_are_bounded_and_preserve_exact_bytes(self):
        directory, files = self.session()
        real_read = os.read
        with patch.object(os, 'read', side_effect=lambda fd, count: real_read(fd, min(7, count))):
            result = sdk_review_cli.review_session(platform='android', session_directory=directory)
        self.assertEqual(result._digest, export_digest(**files))

    def test_missing_required_siblings_never_fall_back_to_root_capture(self):
        for platform in ('android', 'ios'):
            names = ('capture.json', 'session.json') if platform == 'android' else (
                'capture.json', 'metadata.json', 'finalized.json')
            for name in names:
                with self.subTest(platform=platform, name=name):
                    directory, _ = self.session(platform, name=platform + name)
                    (directory / name).rename(self.root / name)
                    self.rejected(directory, platform)

    def test_invalid_json_and_finalization_mismatches_are_fixed_non_echoing_errors(self):
        for platform in ('android', 'ios'):
            for part in ('capture', 'metadata', 'marker') if platform == 'ios' else ('capture', 'metadata'):
                directory, files = self.session(platform, name=platform + part)
                name = 'capture.json' if part == 'capture' else ('finalized.json' if part == 'marker'
                       else 'session.json' if platform == 'android' else 'metadata.json')
                value = json.loads(files[part])
                value['sessionId' if part != 'marker' else 'endSequence'] = CANARY
                (directory / name).write_text(json.dumps(value))
                self.rejected(directory, platform)
                (directory / name).write_bytes(b'{"' + CANARY.encode() + b'":')
                self.rejected(directory, platform)

    def test_empty_or_oversized_required_files_rejected_before_any_read(self):
        for platform in ('android', 'ios'):
            names = ('capture.json', 'session.json') if platform == 'android' else (
                'capture.json', 'metadata.json', 'finalized.json')
            for name in names:
                for size in (0, MAX_BYTES + 1):
                    directory, _ = self.session(platform, name=platform + name + str(size))
                    (directory / name).write_bytes(b' ' * size)
                    with patch.object(os, 'read', side_effect=AssertionError('must not read')):
                        self.rejected(directory, platform)

    def test_exact_size_limit_is_accepted_and_growth_read_is_bounded(self):
        directory, files = self.session('ios')
        for key, name in (('capture', 'capture.json'), ('metadata', 'metadata.json'),
                          ('marker', 'finalized.json')):
            files[key] = files[key].ljust(MAX_BYTES, b' ')
            (directory / name).write_bytes(files[key])
        envelope = sdk_review_cli.review_session(platform='ios', session_directory=directory)
        self.assertEqual(envelope._digest, export_digest(**files))
        with patch.object(os, 'read', return_value=b' ' * (MAX_BYTES + 1)) as read:
            with self.assertRaisesRegex(ReviewInvalid, '^sdk_export_invalid$'):
                sdk_review_cli._read(-1)
        read.assert_called_once_with(-1, MAX_BYTES + 1)

    def test_symlinked_required_files_and_directory_ancestors_rejected(self):
        for platform in ('android', 'ios'):
            directory, _ = self.session(platform, name=platform)
            for source in list(directory.iterdir()):
                saved = self.root / source.name
                source.rename(saved); source.symlink_to(saved)
                self.rejected(directory, platform)
                source.unlink(); saved.rename(source)
            alias = self.root / (platform + '-alias')
            alias.symlink_to(directory, target_is_directory=True)
            self.rejected(alias, platform)
            # A lexical '..' cannot erase a symlink traversal before opening it.
            self.rejected(alias / '..' / platform, platform)
        ancestor = self.root / 'linked-ancestor'
        ancestor.symlink_to(self.root, target_is_directory=True)
        self.rejected(ancestor / 'android')

    def test_special_required_files_and_session_directory_never_hang(self):
        for platform in ('android', 'ios'):
            names = ('capture.json', 'session.json') if platform == 'android' else (
                'capture.json', 'metadata.json', 'finalized.json')
            for name in names:
                directory, _ = self.session(platform, name=platform + name)
                path = directory / name
                path.unlink(); os.mkfifo(path)
                result = self.invoke(directory, platform)
                self.assertEqual((result.returncode, json.loads(result.stdout), result.stderr), (2, ERROR, ''))
                path.unlink(); path.mkdir()
                self.rejected(directory, platform)
        fifo = self.root / 'session-fifo'
        os.mkfifo(fifo)
        self.assertEqual(self.invoke(fifo).returncode, 2)

    def test_replacement_before_or_during_open_is_rejected_without_read(self):
        for phase in ('before-open', 'after-open'):
            directory, _ = self.session(name=phase)
            real_open = os.open
            def opened(name, flags, *args, **kwargs):
                if name == ('capture.json' if phase == 'before-open' else 'session.json'):
                    path = directory / 'capture.json'
                    path.rename(directory / 'saved-capture')
                    if phase == 'before-open':
                        path.symlink_to(directory / 'saved-capture')
                    else:
                        path.write_bytes((directory / 'saved-capture').read_bytes())
                return real_open(name, flags, *args, **kwargs)
            with patch.object(os, 'open', side_effect=opened), \
                 patch.object(os, 'read', side_effect=AssertionError('must not read')):
                self.rejected(directory)

    def test_fifo_swap_between_named_check_and_open_never_hangs(self):
        directory, _ = self.session()
        script = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
from reproof.sdk_review_cli import main
directory = Path(sys.argv[1])
real_open = os.open
def opened(name, flags, *args, **kwargs):
    if name == 'capture.json':
        path = directory / name
        path.unlink()
        os.mkfifo(path)
    return real_open(name, flags, *args, **kwargs)
with patch.object(os, 'open', side_effect=opened):
    raise SystemExit(main(['--platform', 'android', '--session-directory', str(directory)]))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(directory)], cwd=ROOT,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, json.loads(result.stdout), result.stderr), (2, ERROR, ''))

    def test_file_replacement_or_mutation_during_read_is_rejected(self):
        for change in ('replace-current', 'replace-unread', 'mutate-current', 'grow-current'):
            directory, _ = self.session(name=change)
            real_read = os.read
            fired = False
            def read(fd, amount):
                nonlocal fired
                raw = real_read(fd, amount)
                if not fired:
                    fired = True
                    path = directory / ('session.json' if change == 'replace-unread' else 'capture.json')
                    if change.startswith('replace'):
                        path.rename(directory / 'saved'); path.write_bytes((directory / 'saved').read_bytes())
                    elif change == 'grow-current':
                        with path.open('ab') as stream:
                            stream.write(b' ' * (MAX_BYTES + 1))
                    else:
                        info = path.stat()
                        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
                return raw
            with patch.object(os, 'read', side_effect=read):
                self.rejected(directory)
            self.assertTrue(fired)

    def test_mutation_of_previously_read_sibling_with_restored_mtime_is_rejected(self):
        directory, _ = self.session()
        capture = directory / 'capture.json'
        metadata_inode = (directory / 'session.json').stat().st_ino
        original = capture.stat()
        real_read = os.read
        fired = False
        def read(fd, amount):
            nonlocal fired
            raw = real_read(fd, amount)
            if not fired and os.fstat(fd).st_ino == metadata_inode:
                fired = True
                capture.write_bytes(capture.read_bytes())
                os.utime(capture, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertNotEqual(capture.stat().st_ctime_ns, original.st_ctime_ns)
            return raw
        with patch.object(os, 'read', side_effect=read):
            self.rejected(directory)
        self.assertTrue(fired)

    def test_directory_and_ancestor_replacement_during_read_is_rejected(self):
        for replace_ancestor in (False, True):
            parent = self.root / str(replace_ancestor)
            directory, _ = self.session(name=str(replace_ancestor) + '/session')
            real_read = os.read
            fired = False
            def read(fd, amount):
                nonlocal fired
                raw = real_read(fd, amount)
                if not fired:
                    fired = True
                    target = parent if replace_ancestor else directory
                    target.rename(target.with_name(target.name + '-saved'))
                    directory.mkdir(parents=True)
                return raw
            with patch.object(os, 'read', side_effect=read):
                self.rejected(directory)

    def test_mutation_during_normalization_is_rejected(self):
        directory, _ = self.session()
        def normalize(**kwargs):
            envelope = normalize_export(**kwargs)
            path = directory / 'capture.json'
            path.rename(directory / 'saved'); path.write_bytes((directory / 'saved').read_bytes())
            return envelope
        with patch.object(sdk_review_cli, 'normalize_export', side_effect=normalize):
            self.rejected(directory)

    def test_io_failures_are_non_echoing_and_all_descriptors_close(self):
        directory, _ = self.session()
        descriptors = []
        real_open = os.open
        def opened(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            descriptors.append(fd)
            return fd
        with patch.object(os, 'open', side_effect=opened), \
             patch.object(os, 'read', side_effect=PermissionError(CANARY)):
            self.rejected(directory)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_missing_directory_and_invalid_path_are_non_echoing(self):
        self.rejected(self.root / CANARY)
        self.rejected(CANARY + '\0')

    def test_relative_directory_is_anchored_without_resolution(self):
        directory, files = self.session()
        saved = os.getcwd()
        try:
            os.chdir(self.root)
            result = sdk_review_cli.review_session(platform='android', session_directory=directory.name)
            self.assertEqual(result._digest, export_digest(**files))
        finally:
            os.chdir(saved)

    def test_parser_rejects_extra_options_without_echo_or_file_reads(self):
        base = ['--platform', 'android', '--session-directory', str(self.root / CANARY)]
        for option in ('--server', '--ticket', '--registry', '--credentials', '--credential-stdin',
                       '--ai', '--approval', '--upload', '--output-file', '--plat'):
            with self.subTest(option=option), \
                 patch.object(os, 'open', side_effect=AssertionError('must not open')):
                code, out, err = self.run_cli(*base, option, CANARY)
            self.assertEqual((code, out, err), (2, '', 'Invalid SDK review arguments; use --help\n'))
        for args in ([], ['--platform', CANARY], ['--session-directory', CANARY],
                     ['--platform', 'android', '--session-directory'], ['@' + CANARY]):
            code, out, err = self.run_cli(*args)
            self.assertEqual((code, out, err), (2, '', 'Invalid SDK review arguments; use --help\n'))

    def test_help_and_public_dispatch(self):
        code, out, err = self.run_cli('--help')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('--session-directory', out)
        self.assertIn('repository sample policies only', out)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(['--help'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('sdk-review', out.getvalue())

    def test_command_has_no_network_device_agent_credential_or_collector_effects(self):
        directory, _ = self.session()
        def forbidden(*_args, **_kwargs):
            raise AssertionError('No external or trusted-binding effects')
        with patch('socket.socket', side_effect=forbidden), \
             patch('subprocess.Popen', side_effect=forbidden), \
             patch('getpass.getpass', side_effect=forbidden), \
             patch('builtins.input', side_effect=forbidden), \
             patch('reproof.cli.AdbDevice', side_effect=forbidden), \
             patch('reproof.cli.ClaudeAgent', side_effect=forbidden), \
             patch('reproof.sdk_review.CollectorBinding', side_effect=forbidden), \
             patch('reproof.sdk_review.linked_review', side_effect=forbidden), \
             patch('reproof.sdk_review.intake_ticket', side_effect=forbidden):
            code, out, err = self.run_cli('--platform', 'android', '--session-directory', directory)
        self.assertEqual((code, json.loads(out)['status'], err), (0, 'review-only', ''))

    def test_portable_existing_goal_includes_cli_regressions(self):
        inventory = json.loads((ROOT / 'tests/release_inventory.json').read_text())
        commands = [c['command'] for c in inventory['goals']['TICKET_INTAKE']['checks']]
        self.assertTrue(any('tests.test_sdk_review_cli' in command for command in commands))


if __name__ == '__main__':
    unittest.main()
