"""Fake device/ACL controls only; no real device, privilege, or native tools."""
import importlib.util
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kvm_acl", ROOT / "scripts/ci/android-kvm-acl.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
EXTENDED = "user::rw-\nuser:2000:r--\ngroup::rw-\nmask::rw-\nother::---\n"
BASIC = "user::rw-\ngroup::rw-\nother::---\n"

class Fake:
    def __init__(self, original=EXTENDED):
        self.original = m.parse_acl(original)
        self.acl = dict(self.original)
        self.ident = dict(dev=1, ino=2, rdev=3, uid=0, gid=108, mode=stat.S_IFCHR | 0o660)
        self.calls = []
        self.access = True
        self.fail_grant = False
        self.fail_restore = False
        self.directory = None
    def uid(self): return os.getuid()
    def device_identity(self): return dict(self.ident)
    def read_acl(self): return m.encode_acl(self.acl)
    def accessible(self): return self.access
    def grant(self, uid, added_mask=None):
        self.calls.append(("grant", uid, added_mask))
        saved = json.loads((self.directory / m.STATE).read_text())
        assert saved["status"] == "inflight", "inflight marker must precede mutation"
        self.acl = m.expected_grant(self.original, uid)
        if self.fail_grant:
            raise m.ACLStop("fake_grant_failed")
    def restore(self, acl):
        self.calls.append(("restore", dict(acl)))
        if self.fail_restore:
            raise m.ACLStop("fake_restore_failed")
        self.acl = dict(acl)

class ACLTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.backend = Fake()
        self.backend.directory = self.directory
        denial = patch.object(m.subprocess, "run", side_effect=AssertionError("native invocation forbidden"))
        denial.start()
        self.addCleanup(denial.stop)

    def test_extended_grant_restore_preserves_other_effective_rights(self):
        m.prepare(self.directory, self.backend)
        before = m.effective(self.backend.acl)
        self.assertTrue(m.grant(self.directory, self.backend)["granted"])
        self.assertEqual(self.backend.calls[0], ("grant", os.getuid(), None))
        after = m.effective(self.backend.acl)
        self.assertTrue(all(after[key] == value for key, value in before.items()))
        self.assertEqual(stat.S_IMODE((self.directory / m.STATE).stat().st_mode), 0o600)
        self.assertTrue(m.restore(self.directory, self.backend)["restored"])
        self.assertEqual(self.backend.acl, self.backend.original)

    def test_basic_mask_is_exact_original_group_bits_and_restored_removed(self):
        self.backend = Fake(BASIC)
        self.backend.directory = self.directory
        m.prepare(self.directory, self.backend)
        m.grant(self.directory, self.backend)
        self.assertEqual(self.backend.calls[0], ("grant", os.getuid(), "rw-"))
        self.assertEqual(self.backend.acl["group:"], "rw-")
        self.assertEqual(self.backend.acl["mask:"], "rw-")
        m.restore(self.directory, self.backend)
        self.assertNotIn("mask:", self.backend.acl)

    def test_basic_group_missing_write_is_rejected_without_mutation(self):
        self.backend = Fake(BASIC.replace("group::rw-", "group::r--"))
        self.backend.ident["mode"] = stat.S_IFCHR | 0o640
        with self.assertRaisesRegex(m.ACLStop, "acl_mask_cannot_grant_rw"):
            m.prepare(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_extended_mask_missing_write_is_never_expanded(self):
        self.backend = Fake(EXTENDED.replace("mask::rw-", "mask::r--"))
        self.backend.ident["mode"] = stat.S_IFCHR | 0o640
        with self.assertRaisesRegex(m.ACLStop, "acl_mask_cannot_grant_rw"):
            m.prepare(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_original_named_runner_entry_restored_exactly(self):
        self.backend.acl[f"user:{os.getuid()}"] = "r--"
        self.backend.original = dict(self.backend.acl)
        with m.temporary_grant(self.directory, self.backend):
            self.assertEqual(self.backend.acl[f"user:{os.getuid()}"], "rw-")
        self.assertEqual(self.backend.acl[f"user:{os.getuid()}"], "r--")

    def test_runner_owner_rejected(self):
        self.backend.ident["uid"] = os.getuid()
        with self.assertRaisesRegex(m.ACLStop, "runner_is_device_owner"):
            m.prepare(self.directory, self.backend)

    def test_parser_rejects_duplicate_default_header_missing_mask(self):
        for invalid in (EXTENDED + "user::rw-\n", EXTENDED + "default:user::rw-\n",
                        "# owner: 0\n" + EXTENDED, EXTENDED.replace("mask::rw-\n", ""),
                        EXTENDED.replace("user:2000", "user:02000")):
            with self.subTest(invalid=invalid), self.assertRaises(m.ACLStop):
                m.parse_acl(invalid)

    def test_device_symlink_or_regular_file_identity_rejected(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFREG | 0o660):
            fake_stat = type("Info", (), {"st_mode": mode})()
            with self.assertRaisesRegex(m.ACLStop, "kvm_not_character_device"):
                m.identity(fake_stat)

    def test_changed_device_identity_before_grant_blocks_mutation(self):
        m.prepare(self.directory, self.backend)
        self.backend.ident["rdev"] = 99
        with self.assertRaisesRegex(m.ACLStop, "kvm_identity_changed"):
            m.grant(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_grant_failure_attempts_restore_and_reports_no_success(self):
        m.prepare(self.directory, self.backend)
        self.backend.fail_grant = True
        with self.assertRaisesRegex(m.ACLStop, "acl_grant_failed_restored"):
            m.grant(self.directory, self.backend)
        self.assertEqual(self.backend.acl, self.backend.original)
        self.assertEqual([item[0] for item in self.backend.calls], ["grant", "restore"])

    def test_inaccessible_after_grant_restores(self):
        m.prepare(self.directory, self.backend)
        self.backend.access = False
        with self.assertRaisesRegex(m.ACLStop, "acl_grant_failed_restored"):
            m.grant(self.directory, self.backend)
        self.assertEqual(self.backend.acl, self.backend.original)

    def test_failed_restore_leaves_pending_state_and_fails_truthfully(self):
        m.prepare(self.directory, self.backend)
        self.backend.fail_grant = self.backend.fail_restore = True
        with self.assertRaisesRegex(m.ACLStop, "acl_grant_failed_restore_uncertain"):
            m.grant(self.directory, self.backend)
        self.assertEqual(json.loads((self.directory / m.STATE).read_text())["status"], "pending")

    def test_fallback_restores_pending_transaction(self):
        m.prepare(self.directory, self.backend)
        directory, state = m.read_state(self.directory, self.backend)
        state["status"] = "pending"
        m.write_state(directory, state)
        self.backend.acl = m.expected_grant(self.backend.original, os.getuid())
        self.assertTrue(m.restore(self.directory, self.backend)["restored"])

    def test_concurrent_acl_change_not_overwritten(self):
        m.prepare(self.directory, self.backend)
        self.backend.acl["other:"] = "r--"
        with self.assertRaisesRegex(m.ACLStop, "acl_state_current_mismatch"):
            m.restore(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_cancellation_restores_and_second_grant_rejected(self):
        with self.assertRaises(KeyboardInterrupt):
            with m.temporary_grant(self.directory, self.backend):
                raise KeyboardInterrupt()
        self.assertEqual(self.backend.acl, self.backend.original)
        with self.assertRaisesRegex(m.ACLStop, "acl_grant_already_attempted"):
            m.grant(self.directory, self.backend)

    def test_state_directory_symlink_rejected(self):
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(m.ACLStop, "acl_state_directory_rejected"):
            m.prepare(alias, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_state_file_symlink_rejected_without_mutation(self):
        target = self.directory / "other"
        target.write_text("{}")
        (self.directory / m.STATE).symlink_to(target)
        with self.assertRaises(OSError):
            m.prepare(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(target.read_text(), "{}")

    def test_canonical_identity_binds_kernel_misc_device_without_native_commands(self):
        import io
        expected = dict(self.backend.ident, rdev=os.makedev(10, 232))
        dev = type("Info", (), {"st_" + key: value for key, value in expected.items()})()
        parent = type("Info", (), {"st_mode": stat.S_IFDIR | 0o755})()
        with patch.object(m.Path, "resolve", return_value=Path("/dev")), \
             patch.object(m.os, "lstat", side_effect=[parent, dev]), \
             patch("builtins.open", return_value=io.StringIO("10:232\n")):
            self.assertEqual(m.canonical_identity(), expected)
        with patch.object(m.Path, "resolve", return_value=Path("/dev")), \
             patch.object(m.os, "lstat", side_effect=[parent, dev]), \
             patch("builtins.open", return_value=io.StringIO("10:999\n")):
            with self.assertRaisesRegex(m.ACLStop, "kvm_kernel_identity_rejected"):
                m.canonical_identity()

    def test_original_held_inode_restored_but_path_replacement_reported_failed(self):
        m.prepare(self.directory, self.backend)
        m.grant(self.directory, self.backend)
        original_identity = dict(self.backend.ident)
        self.backend.held_identity = lambda: original_identity
        self.backend.ident["ino"] = 99
        with self.assertRaisesRegex(m.ACLStop, "kvm_identity_changed_after_restore"):
            m.restore(self.directory, self.backend)
        self.assertEqual(self.backend.acl, self.backend.original)
        self.assertEqual(json.loads((self.directory / m.STATE).read_text())["status"], "restored")

    def test_corrupted_snapshot_identity_rejected_before_privilege(self):
        m.prepare(self.directory, self.backend)
        path = self.directory / m.STATE
        state = json.loads(path.read_text())
        state["identity"]["mode"] = True
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(m.ACLStop, "acl_state_rejected"):
            m.restore(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_unconfirmed_privileged_completion_blocks_fallback_restoration(self):
        m.prepare(self.directory, self.backend)
        self.backend._mutation_quiescent = False
        self.backend.fail_grant = True
        with self.assertRaisesRegex(m.ACLStop, "acl_command_completion_uncertain"):
            m.grant(self.directory, self.backend)
        self.assertEqual([item[0] for item in self.backend.calls], ["grant"])
        with self.assertRaisesRegex(m.ACLStop, "acl_command_completion_uncertain"):
            m.restore(self.directory, self.backend)
        self.assertEqual(json.loads((self.directory / m.STATE).read_text())["status"], "mutation_uncertain")

    def test_parent_death_inflight_marker_blocks_late_grant_race(self):
        m.prepare(self.directory, self.backend)
        directory, state = m.read_state(self.directory, self.backend)
        state["status"] = "inflight"
        m.write_state(directory, state)
        self.backend.acl = m.expected_grant(self.backend.original, os.getuid())
        with self.assertRaisesRegex(m.ACLStop, "acl_command_completion_uncertain"):
            m.restore(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(json.loads((self.directory / m.STATE).read_text())["status"], "inflight")

    def test_restore_command_also_has_durable_inflight_marker(self):
        m.prepare(self.directory, self.backend)
        m.grant(self.directory, self.backend)
        original_restore = self.backend.restore
        def checked_restore(acl):
            self.assertEqual(json.loads((self.directory / m.STATE).read_text())["status"], "inflight")
            original_restore(acl)
        self.backend.restore = checked_restore
        self.assertTrue(m.restore(self.directory, self.backend)["restored"])

    def test_killed_monitor_is_not_quiescence_but_normal_timeout_is(self):
        for returncode, expected in ((-9, False), (137, False), (143, False), (124, True), (1, True), (0, True)):
            backend = m.NativeBackend()
            class Process:
                def __init__(self): self.returncode = None
                def communicate(self, input=None): self.returncode = returncode
            with self.subTest(returncode=returncode), patch.object(m.signal, "signal"), \
                 patch.object(m.subprocess, "Popen", return_value=Process()):
                if returncode == 0:
                    backend._run(["fake-monitor"], mutation=True)
                else:
                    with self.assertRaises(m.ACLStop):
                        backend._run(["fake-monitor"], mutation=True)
                self.assertEqual(backend._mutation_quiescent, expected)

    def test_named_groups_and_masked_raw_rights_are_unchanged(self):
        text = "user::rw-\nuser:2000:rwx\ngroup::rwx\ngroup:3000:rwx\nmask::rw-\nother::---\n"
        self.backend = Fake(text)
        self.backend.directory = self.directory
        before = m.effective(self.backend.acl)
        with m.temporary_grant(self.directory, self.backend):
            self.assertEqual(self.backend.acl["group:3000"], "rwx")
            self.assertEqual(self.backend.acl["user:2000"], "rwx")
            self.assertTrue(all(m.effective(self.backend.acl)[key] == value for key, value in before.items()))
        self.assertEqual(self.backend.acl, self.backend.original)

    def test_basic_group_rwx_mask_preserves_mode_and_other_rights(self):
        self.backend = Fake(BASIC.replace("group::rw-", "group::rwx"))
        self.backend.directory = self.directory
        self.backend.ident["mode"] = stat.S_IFCHR | 0o670
        with m.temporary_grant(self.directory, self.backend):
            self.assertEqual(self.backend.acl["mask:"], "rwx")
            self.assertEqual(self.backend.acl["group:"], "rwx")
            self.assertEqual(self.backend.acl[f"user:{os.getuid()}"], "rw-")
            self.assertEqual(self.backend.ident["mode"], stat.S_IFCHR | 0o670)
        self.assertEqual(self.backend.acl, self.backend.original)

    def test_special_device_mode_bits_rejected(self):
        for bit in (stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX):
            info = type("Info", (), {"st_mode": stat.S_IFCHR | 0o660 | bit})()
            with self.subTest(bit=bit), self.assertRaisesRegex(m.ACLStop, "kvm_special_mode_rejected"):
                m.identity(info)
        m.prepare(self.directory, self.backend)
        path = self.directory / m.STATE
        state = json.loads(path.read_text())
        state["identity"]["mode"] |= stat.S_ISGID
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(m.ACLStop, "acl_state_rejected"):
            m.restore(self.directory, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_fixed_native_argv_only_mocked(self):
        backend = m.NativeBackend()
        @contextmanager
        def pinned():
            yield "/proc/123/fd/7"
        prefix = ["sudo", "-n", "timeout", "--foreground", "--signal=TERM", "--kill-after=1", "10", "setfacl", "--no-mask"]
        with patch.object(backend, "_pinned", pinned), patch.object(backend, "_run", return_value=b"") as command:
            backend.grant(1000)
            command.assert_called_with(prefix + ["-m", "u:1000:rw", "/proc/123/fd/7"], mutation=True)
            backend.grant(1000, "rw-")
            command.assert_called_with(prefix + ["-m", "u:1000:rw,m::rw-", "/proc/123/fd/7"], mutation=True)
            backend.restore(m.parse_acl(BASIC))
            self.assertEqual(command.call_args.args[0], prefix + ["--set-file=-", "/proc/123/fd/7"])
            self.assertNotIn(b"#", command.call_args.args[1])

    def test_replaced_device_before_pin_never_reaches_privilege_boundary(self):
        backend = m.NativeBackend()
        backend._expected = dict(self.backend.ident)
        other = dict(self.backend.ident, ino=99)
        stat_result = type("Info", (), {"st_" + key: value for key, value in other.items()})()
        with patch.object(m.os, "open", return_value=7), patch.object(m.os, "fstat", return_value=stat_result), \
             patch.object(m.os, "close") as close, patch.object(backend, "_run") as command:
            with self.assertRaisesRegex(m.ACLStop, "kvm_identity_changed"):
                backend.grant(1000)
        command.assert_not_called()
        close.assert_called_once_with(7)

    def test_signal_deferred_until_privileged_process_completes(self):
        backend = m.NativeBackend()
        handlers, events = {}, []
        class Process:
            returncode = None
            def communicate(self, input=None):
                events.append("command-started")
                handlers[m.signal.SIGTERM](m.signal.SIGTERM, None)
                events.append("late-grant-completed")
                self.returncode = 0
                events.append("command-reaped")
            def wait(self):
                raise AssertionError("already reaped")
        def register(number, handler):
            old = handlers.get(number)
            handlers[number] = handler
            return old
        with patch.object(m.signal, "signal", side_effect=register), patch.object(m.subprocess, "Popen", return_value=Process()):
            with self.assertRaisesRegex(m.ACLStop, "acl_command_canceled"):
                backend._run(["fake-privileged-command"], mutation=True)
        events.append("caller-may-restore")
        self.assertEqual(events, ["command-started", "late-grant-completed", "command-reaped", "caller-may-restore"])

    def test_unexpected_interrupt_waits_for_privileged_quiescence(self):
        backend = m.NativeBackend()
        events = []
        class Process:
            returncode = None
            def communicate(self, input=None):
                raise KeyboardInterrupt()
            def wait(self):
                events.append("child-reaped")
                self.returncode = 0
        with patch.object(m.signal, "signal"), patch.object(m.subprocess, "Popen", return_value=Process()):
            with self.assertRaises(KeyboardInterrupt):
                backend._run(["fake-privileged-command"], mutation=True)
        events.append("caller-may-restore")
        self.assertEqual(events, ["child-reaped", "caller-may-restore"])

if __name__ == "__main__":
    unittest.main()
