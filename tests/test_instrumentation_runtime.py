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

    def test_recorder_atomic_write_delegates_to_same_file_owned_temporary_helper(self):
        # Supporting source wiring only. The JVM tests exercise this production helper.
        source = (ROOT / "android/sdk/src/main/java/io/reproof/sdk/ReproRecorder.kt").read_text()
        self.assertIn("internal fun atomicWriteFile(", source)
        helper = source[source.index("internal fun atomicWriteFile("):source.index("class ReproPrivacyConfig(")]
        self.assertIn("File.createTempFile(", helper)
        self.assertIn("destination.absoluteFile.parentFile", helper)
        operations = [helper.index(operation) for operation in (
            "write(output, bytes)", "output.fd.sync()", "temporary.renameTo(destination)",
        )]
        self.assertEqual(operations, sorted(operations))
        self.assertIn("if (!published)", helper)
        self.assertIn("temporary.delete()", helper)
        self.assertNotIn("destination.delete()", helper)
        wrapper = source[source.index("private fun atomicWrite("):source.index("private fun invalidateSession(")]
        self.assertIn("atomicWriteFile(destination, bytes)", wrapper)
        self.assertNotIn('"${destination.name}.tmp"', source)

    def test_recorder_export_uses_session_bound_helper_and_keeps_legacy_publication(self):
        # Supporting source wiring contract, not a substitute for the JVM helper tests.
        source = (ROOT / "android/sdk/src/main/java/io/reproof/sdk/ReproRecorder.kt").read_text()
        freeze = source[source.index("fun freezeAndExport("):source.index("fun capturePath()")]
        self.assertNotIn("callback(publishedCaptureFile)", freeze)
        self.assertIn("fun freezeAndExport(callback: (File) -> Unit = {}): Boolean", freeze)
        self.assertIn("if (!isEnabled || frozen) return false", freeze)
        self.assertLess(freeze.index("executor.execute"), freeze.index("publishSessionExport("))
        self.assertIn("sessionDirectory = sessionDirectory", freeze)
        self.assertIn("writeCapture = ::writeCapture", freeze)
        self.assertIn("writeMetadata(finalized = true, incomplete = incomplete, destination = destination)", freeze)
        self.assertIn("callback = callback", freeze)
        self.assertEqual(freeze.count("catch (_: Throwable)"), 2)
        self.assertEqual(freeze.count("markWriteFailure()"), 2)
        helper = source[source.index("internal fun publishSessionExport("):source.index("class ReproRecorder")]
        self.assertIn('val capture = File(sessionDirectory, "capture.json")', helper)
        calls = [helper.index(call) for call in (
            "writeCapture(capture)",
            'finalizeMetadata(File(sessionDirectory, "session.json"))',
            "callback(capture)",
        )]
        self.assertEqual(calls, sorted(calls))
        self.assertNotIn("catch (", helper)
        accessor = source[source.index("fun capturePath()"):source.index("fun close()")]
        self.assertIn("return publishedCaptureFile", accessor)
        writer = source[source.index("private fun writeCapture("):source.index("private fun writeMetadata(")]
        self.assertIn("atomicWrite(destination, bytes)", writer)
        self.assertIn("atomicWrite(publishedCaptureFile, bytes)", writer)
        self.assertIn("destination: File = metadataFile", source)
        self.assertIn("atomicWrite(destination, metadata.toString(2).toByteArray(StandardCharsets.UTF_8))", source)
        runtime = DEBUG_RUNTIME.read_text()
        self.assertIn("recorder.freezeAndExport { capture ->", runtime)
        self.assertIn("writeDiagnostics(session, capture)", runtime)

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
        for declaration in ("class ReproPrivacyConfig(", "class ReproStartState(", "class ReproRecorder",
                            "internal fun publishSessionExport(", "internal fun atomicWriteFile("):
            self.assertIn(declaration, recorder)


if __name__ == "__main__":
    unittest.main()
