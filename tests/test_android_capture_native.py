"""Portable source wiring and separately executed actual Kotlin capture policies."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from reproof.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt'
POLICY = ROOT / 'android/live/src/main/java/io/reproof/live/CapturePolicy.kt'


class NativeCapturePolicySourceTests(unittest.TestCase):
    def test_nondefault_configuration_is_strict_and_handshake_is_private_metadata(self):
        source = SOURCE.read_text()
        policy = POLICY.read_text()
        self.assertIn('"appOwnedFixture", "capturePolicy") + authorityFields', source)
        self.assertIn('shared = hasAuthority, general = profile.general', source)
        self.assertIn('maxWidth != capturePolicy.maxWidth', source)
        self.assertIn('if (!shared || !general || raw.keys != setOf(', policy)
        self.assertIn('raw.values.any { it !is Int }', policy)
        self.assertIn('put("capturePolicy", JSONObject(it.fields()))', source)
        self.assertIn('put("capturePolicyVersion", 1)', source)
        self.assertNotIn('capture_admit', source)
        self.assertNotIn('/capture-admit', source)
        self.assertLess(source.index('config = LiveConfig.read'), source.index('getUiAutomation('))

    def test_capture_gates_precede_screenshot_and_scaling_and_keep_raw_coordinates(self):
        source = SOURCE.read_text()
        body = source[source.index('    private fun captureFrame():'):source.index('    private fun startScreenshotLoop(')]
        self.assertLess(body.index('targetFrameAllowed(liveConfig) || !captureDisplayAllowed()'), body.index('takeScreenshot()'))
        self.assertLess(body.index('CaptureLimits.rawAllowed(originalWidth, originalHeight)'), body.index('Bitmap.createScaledBitmap'))
        self.assertLess(body.index('evenFrameDimensions'), body.index('Bitmap.createScaledBitmap'))
        self.assertIn('val output = BoundedJpegOutputStream()', body)
        self.assertNotIn('ByteArrayOutputStream()', body)
        self.assertIn('actualFrameWidth = originalWidth', body)
        self.assertIn('actualFrameHeight = originalHeight', body)
        self.assertIn('const val MAX_FRAME_BUFFER_BYTES = 12 * 1024 * 1024', source)
        gate = source[source.index('    private fun targetFrameAllowed'):source.index('    private fun captureDisplayAllowed')]
        self.assertIn('generalTargetReady.get()', gate)
        self.assertIn('targetAppVisible()', gate)

    def test_fixed_caps_and_bounded_encoder_storage_remain_explicit(self):
        policy = POLICY.read_text()
        for text in ('OUTPUT_MAX_AXIS = 8192', 'OUTPUT_PIXELS = 8_294_400L', 'RAW_PIXELS = 16_777_216L',
                     'FRAME_BYTES = 3 * 1024 * 1024', 'width.toLong() * height.toLong()',
                     'ByteArray(maximum)', 'length > maximum - count', 'if (failed) throw IOException'):
            self.assertIn(text, policy)
        self.assertNotIn('ByteArrayOutputStream', policy)


class NativeCapturePolicyExecutableTests(unittest.TestCase):
    def test_actual_kotlin_policy_rejects_malformed_limits_and_bounds_encoder_bytes(self):
        program = r'''package io.reproof.live
import java.io.IOException

private fun fails(block: () -> Unit) {
    var rejected = false
    try { block() } catch (_: IOException) { rejected = true }
    check(rejected)
}
fun main() {
    val base: Map<String, Any?> = mapOf("schemaVersion" to 1, "maxWidth" to 1280,
        "pixelLimit" to 8_294_400, "frameBytes" to 3_145_728)
    for (width in listOf(480, 720, 960, 1280, 1440, 1920)) {
        val policy = CapturePolicy.parse(base + ("maxWidth" to width), true, true) ?: error("valid policy")
        check(policy.maxWidth == width)
        check(policy.fields() == base + ("maxWidth" to width))
    }
    check(CapturePolicy.parse(base, false, true) == null)
    check(CapturePolicy.parse(base, true, false) == null)
    check(CapturePolicy.parse(base + ("extra" to 1), true, true) == null)
    for (key in base.keys) {
        check(CapturePolicy.parse(base - key, true, true) == null)
        for (wrong in listOf<Any?>(null, true, 1.0, 1L, "1")) {
            check(CapturePolicy.parse(base + (key to wrong), true, true) == null)
        }
    }
    for (width in listOf(-1, 0, 1, 479, 959, 961, 1281, 1921, Int.MAX_VALUE)) {
        check(CapturePolicy.parse(base + ("maxWidth" to width), true, true) == null)
    }
    check(CapturePolicy.parse(base + ("schemaVersion" to 2), true, true) == null)
    check(CapturePolicy.parse(base + ("pixelLimit" to 8_294_401), true, true) == null)
    check(CapturePolicy.parse(base + ("frameBytes" to 3_145_729), true, true) == null)
    check(CaptureLimits.rawAllowed(4096, 4096))
    check(!CaptureLimits.rawAllowed(4096, 4097))
    check(!CaptureLimits.rawAllowed(Int.MAX_VALUE, Int.MAX_VALUE))
    check(!CaptureLimits.rawAllowed(0, 1))
    check(!CaptureLimits.rawAllowed(1, -1))
    check(CaptureLimits.outputAllowed(1920, 4320))
    check(!CaptureLimits.outputAllowed(2, 4_000_000))
    check(CaptureLimits.outputAllowed(2, 8192))
    check(!CaptureLimits.outputAllowed(2, 8194))
    check(!CaptureLimits.outputAllowed(1920, 4322))
    check(!CaptureLimits.outputAllowed(Int.MAX_VALUE, Int.MAX_VALUE))
    val output = BoundedJpegOutputStream(8)
    output.write(byteArrayOf(1, 2, 3, 4), 0, 4)
    output.write(5); output.write(6); output.write(7); output.write(8)
    check(output.toByteArray().contentEquals(byteArrayOf(1, 2, 3, 4, 5, 6, 7, 8)))
    fails { output.write(9) }
    check(output.failed)
    fails { output.toByteArray() }
    fails { output.write(byteArrayOf(), 0, 0) }
    val bulk = BoundedJpegOutputStream(8)
    fails { bulk.write(ByteArray(9), 0, 9) }
    check(bulk.failed)
    fails { bulk.toByteArray() }
    val invalid = BoundedJpegOutputStream(8)
    var rejected = false
    try { invalid.write(ByteArray(1), Int.MAX_VALUE, 1) }
    catch (_: IndexOutOfBoundsException) { rejected = true }
    check(rejected && invalid.failed)
    println("android-native-capture-policy-passed")
}
'''
        java_home = _java_home()
        _ensure_built(java_home)
        compiler = (ROOT / 'tools/kotlin-instrumenter/build/classes/classpath').read_text().strip()
        stdlib = next(name for name in compiler.split(os.pathsep) if name.endswith('kotlin-stdlib-2.2.10.jar'))
        java = str(Path(java_home) / 'bin/java')
        with tempfile.TemporaryDirectory(prefix='reproof-capture-policy-') as directory:
            root = Path(directory).resolve()
            source = root / 'CaptureProbe.kt'; source.write_text(program)
            compiled = subprocess.run([java, '-cp', compiler, 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                '-jvm-target', '17', '-classpath', stdlib, '-d', str(root / 'classes'),
                str(POLICY), str(source)], capture_output=True, text=True, timeout=60,
                env=_subprocess_environment(java_home))
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-2000:])
            executed = subprocess.run([java, '-cp', str(root / 'classes') + os.pathsep + stdlib,
                'io.reproof.live.CaptureProbeKt'], capture_output=True, text=True, timeout=20,
                env=_subprocess_environment(java_home))
            self.assertEqual(executed.returncode, 0, executed.stderr[-1500:])
            self.assertEqual(executed.stdout.strip(), 'android-native-capture-policy-passed')


if __name__ == '__main__': unittest.main()
