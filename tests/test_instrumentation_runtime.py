from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "reproof" / "instrumentation_templates" / "android"
DEBUG_RUNTIME = TEMPLATES / "debug" / "java" / "io" / "reproof" / "autotrace" / "ReproAuto.kt"
RECEIVER = TEMPLATES / "debug" / "java" / "io" / "reproof" / "autotrace" / "AutoExportReceiver.kt"
RELEASE_RUNTIME = TEMPLATES / "release" / "java" / "io" / "reproof" / "autotrace" / "ReproAuto.kt"


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
        self.assertIn("io.reproof.EXPORT_CAPTURE", manifest)
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
        source = (ROOT / "android" / "sdk" / "src" / "main" / "java" / "io" / "reproof" / "sdk" / "ReproRecorder.kt").read_text()
        self.assertIn("fun lastEventSequence(): Int = synchronized(lock) { nextSequence - 1 }", source)

    def test_recorder_admission_precedes_side_effects_and_raw_serialization(self):
        source = (ROOT / "android/sdk/src/main/java/io/reproof/sdk/ReproRecorder.kt").read_text()
        adapter = source[source.index("private fun admitLegacy"):source.index("class ReproRecorder")]
        self.assertNotIn("toString(", adapter)
        self.assertNotIn("getString(", adapter)
        self.assertIn('raw.length() == 2 && raw.has("screen") && raw.has("nodes")', adapter)
        self.assertIn('raw.opt("nodes") as? JSONObject', adapter)
        self.assertIn("admitStartState(trusted", adapter)
        self.assertIn("this(context, admitLegacy({", source)
        self.assertNotIn("JSONObject(startState.toString())", source)
        self.assertIn("private val appContext by lazy", source)
        self.assertIn("private val executor: ExecutorService by lazy", source)
        self.assertIn("if (isEnabled) executor.execute", source)
        self.assertIn("if (!isEnabled || frozen) return false", source)
        self.assertNotIn("private fun safeTarget", source)
        for signature, admission in (("fun recordTap", "policy.allowsTap"),
                                     ("fun recordReplace", "policy.allowsReplace"),
                                     ("fun recordScrollTo", "policy.allowsScroll")):
            body = source[source.index(signature):]
            self.assertLess(body.index(admission), body.index("JSONObject()"))
            self.assertLess(body.index("if (!isEnabled) return false"), body.index("JSONObject()"))

    def test_runtime_passes_explicit_host_validated_numeric_and_screen_policy(self):
        source = DEBUG_RUNTIME.read_text()
        self.assertIn("numericTargets = configuration.numericTargets", source)
        self.assertIn("allowedStartScreens = setOf(configuration.startScreen)", source)
        self.assertIn('val startScreen = startState.opt("screen") as? String ?: return null', source)
        self.assertIn("if (!IDENTIFIER.matches(startScreen)) return null", source)
        self.assertIn("if (!recorder.isEnabled) return false", source)
        self.assertNotIn("JSONObject(startState.toString())", source)

    def test_actual_debug_templates_are_compiled_only_in_sdk_test_source_set(self):
        build = (ROOT / "android/sdk/build.gradle.kts").read_text()
        self.assertIn('sourceSets.getByName("test").java.srcDir("../../reproof/instrumentation_templates/android/debug/java")', build)
        config = (ROOT / "android/sdk/src/test/java/io/reproof/autotrace/ReproConfig.kt").read_text()
        for name in ("PROFILE_JSON", "PROFILE_DIGEST", "SITES_JSON", "APP_LOGS_ENABLED", "SCREEN_TARGETS_JSON"):
            self.assertIn(f"const val {name}", config)
        workflow = (ROOT / ".github/workflows/android-compile.yml").read_text()
        self.assertEqual(workflow.count("'reproof/instrumentation_templates/android/**'"), 2)
        self.assertIn(":sdk:testDebugUnitTest", workflow)

    def test_distribution_keeps_single_file_policy_support(self):
        import json
        manifest = json.loads((ROOT / "distribution-resources.json").read_text())
        self.assertIn("android/sdk/src/main/java/io/reproof/sdk/ReproRecorder.kt", manifest["files"])
        for resource in (
            "android/sdk/src/test/java/io/reproof/autotrace/ReproConfig.kt",
            "android/sdk/src/test/java/io/reproof/sdk/ReproLegacyConstructorCompileCheck.java",
            "android/sdk/src/test/java/io/reproof/sdk/ReproPrivacyConfigTest.kt",
        ):
            self.assertIn(resource, manifest["files"])
            self.assertTrue((ROOT / resource).is_file())
        recorder = (ROOT / "android/sdk/src/main/java/io/reproof/sdk/ReproRecorder.kt").read_text()
        for declaration in ("class ReproPrivacyConfig(", "class ReproStartState(", "class ReproRecorder"):
            self.assertIn(declaration, recorder)


if __name__ == "__main__":
    unittest.main()
