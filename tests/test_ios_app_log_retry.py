"""Execute actual production Foundation publication helpers, without a Simulator."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from reproof.app_logs import collect_app_logs
from reproof.core import ContractError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift"
FIXTURE = ROOT / "tests/fixtures/ios-app-logs/publication-tests.swift"
RUN = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"


def _production_struct(source, name):
    """Copy the whole production declaration; never substitute a Python model."""
    start = source.index("struct " + name)
    opening = source.index("{", start)
    end, depth = opening + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@unittest.skipUnless(shutil.which("xcrun"), "Xcode Swift compiler is unavailable")
class IOSAppLogRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        source = TEMPLATE.read_text()
        declarations = [_production_struct(source, name) for name in (
            "RLAppLogRetryPolicy", "RLAppLogWriteGate", "RLAppLogPublication")]
        program = root / "main.swift"
        program.write_text("import Foundation\n" + "\n\n".join(declarations) + "\n" + FIXTURE.read_text())
        cls.executable = root / "publication-tests"
        result = subprocess.run(
            ["xcrun", "swiftc", "-swift-version", "5", "-warnings-as-errors",
             str(program), "-o", str(cls.executable)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            raise AssertionError(f"Production Foundation helpers failed to compile: {result.stderr}")

    def execute(self, scenario):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name)
        result = subprocess.run([str(self.executable), scenario, str(output)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["scenario"], scenario)
        return output, report

    def collect(self, directory):
        def read(relative):
            return json.loads((directory / relative).read_bytes())
        return collect_app_logs(
            read, platform="ios", application_id="io.reproof.sample.ios",
            profile_digest="a" * 64, run_id=RUN, click_targets=set(), screen_targets=set(),
        )

    def test_events_and_status_changes_each_reopen_one_bounded_retry_burst(self):
        self.execute("retry")

    def test_pending_startup_blocks_next_snapshot_until_failure_becomes_sticky(self):
        self.execute("gate")

    def test_initial_empty_snapshot_publishes_matching_identity_without_loss(self):
        output, report = self.execute("initial")
        document = self.collect(output / "current")
        self.assertEqual(document["events"], [])
        self.assertFalse(document["lostEvents"])
        self.assertEqual(report["attempts"], 1)

    def test_each_transient_failure_recovers_collectable_pair_with_sticky_loss(self):
        for phase in ("directory", "journal", "marker", "stale-marker"):
            with self.subTest(phase=phase):
                output, report = self.execute(phase)
                with self.assertRaises((FileNotFoundError, ContractError)):
                    self.collect(output / "before")
                document = self.collect(output / "current")
                self.assertTrue(document["lostEvents"])
                self.assertEqual(document["endSequence"], 3)
                self.assertEqual(report["attempts"], 3)
                if phase in ("directory", "journal"):
                    self.assertFalse((output / "before" / "app-log-session.json").exists())
                if phase in ("marker", "stale-marker"):
                    before = json.loads((output / "before" / "app-logs" / SESSION / "app-log.json").read_bytes())
                    self.assertFalse(before["lostEvents"])

    def test_permanent_failure_stops_and_only_new_revision_reopens_bounded_burst(self):
        for phase in ("directory", "journal", "marker"):
            with self.subTest(phase=phase):
                output, report = self.execute("permanent-" + phase)
                self.assertEqual(report["attempts"], 9)
                self.assertTrue(report["lostEvents"])
                with self.assertRaises(FileNotFoundError):
                    self.collect(output / "current")
                if phase == "marker":
                    journal = json.loads((output / "current" / "app-logs" / SESSION / "app-log.json").read_bytes())
                    self.assertTrue(journal["lostEvents"])

    def test_journal_and_marker_use_the_same_frozen_generation_identity(self):
        output, _ = self.execute("identity")
        document = self.collect(output / "current")
        self.assertEqual(document["sessionId"], SESSION)
        self.assertEqual(document["startedAtMs"], 1000)
        marker = json.loads((output / "current" / "app-log-session.json").read_bytes())
        self.assertEqual(marker, {key: document[key] for key in marker})
        self.assertNotIn("lostEvents", marker)

    def test_maximum_prefix_and_flags_survive_rejected_over_budget_attempt(self):
        output, _ = self.execute("bounds")
        document = self.collect(output / "current")
        self.assertEqual(document["endSequence"], 2000)
        self.assertTrue(document["truncated"])
        self.assertTrue(document["lostEvents"])

    def test_exact_byte_limit_is_inclusive(self):
        output, _ = self.execute("exact-byte-limit")
        self.assertFalse(self.collect(output / "current")["lostEvents"])


if __name__ == "__main__":
    unittest.main()
