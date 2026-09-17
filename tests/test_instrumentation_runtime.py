from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "reproloop" / "instrumentation_templates" / "android"
DEBUG_RUNTIME = TEMPLATES / "debug" / "java" / "io" / "reproloop" / "autotrace" / "ReproAuto.kt"
RECEIVER = TEMPLATES / "debug" / "java" / "io" / "reproloop" / "autotrace" / "AutoExportReceiver.kt"
RELEASE_RUNTIME = TEMPLATES / "release" / "java" / "io" / "reproloop" / "autotrace" / "ReproAuto.kt"


class InstrumentationRuntimeTemplateTests(unittest.TestCase):
    def test_debug_runtime_exposes_fixed_no_throw_hook_order_and_bounds(self):
        source = DEBUG_RUNTIME.read_text()
        positions = [source.index(signature) for signature in (
            "fun start(activity: Activity)",
            "fun stop(activity: Activity)",
            "fun beforeTap(activity: Activity, target: String, siteId: String): Long",
            "fun threw(token: Long)",
            "fun afterTap(token: Long)",
            "fun export(): Boolean",
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("const val MAX_ACTIONS = 500", source)
        self.assertIn("MAX_DIAGNOSTICS_BYTES", source)
        self.assertIn("ReproConfig.SITES_JSON", source)
        self.assertIn("ReproConfig.PROFILE_DIGEST", source)
        self.assertIn("WeakReference(activity)", source)
        self.assertIn('getStringExtra("fixture_id") == configuration.fixtureId', source)
        self.assertIn('getIntExtra("fixture_version", Int.MIN_VALUE) == configuration.fixtureVersion', source)

    def test_receiver_is_debug_only_and_requires_dump_permission(self):
        manifest = (TEMPLATES / "debug" / "AndroidManifest.xml").read_text()
        receiver = RECEIVER.read_text()
        self.assertIn('android:permission="android.permission.DUMP"', manifest)
        self.assertIn('android:exported="true"', manifest)
        self.assertIn("io.reproloop.EXPORT_CAPTURE", manifest)
        self.assertIn("if (intent.action != ACTION_EXPORT_CAPTURE)", receiver)
        self.assertIn("ReproAuto.export()", receiver)
        self.assertIn("RESULT_ACCEPTED = 0", receiver)
        self.assertIn("RESULT_REJECTED = 1", receiver)

    def test_release_stub_has_no_recorder_or_collection_dependency(self):
        source = RELEASE_RUNTIME.read_text()
        self.assertNotIn("ReproRecorder", source)
        self.assertNotIn("ReproConfig", source)
        self.assertIn("fun beforeTap(activity: Activity, target: String, siteId: String): Long = 0L", source)
        self.assertIn("fun export() = false", source)

    def test_config_template_declares_all_generated_values(self):
        source = (TEMPLATES / "debug" / "ReproConfig.kt.template").read_text()
        for name in ("PROFILE_JSON", "PROFILE_DIGEST", "SITES_JSON"):
            self.assertIn(f"const val {name}", source)

    def test_recorder_exposes_accepted_event_sequence(self):
        source = (ROOT / "android" / "sdk" / "src" / "main" / "java" / "io" / "reproloop" / "sdk" / "ReproRecorder.kt").read_text()
        self.assertIn("fun lastEventSequence(): Int = synchronized(lock) { nextSequence - 1 }", source)


if __name__ == "__main__":
    unittest.main()
