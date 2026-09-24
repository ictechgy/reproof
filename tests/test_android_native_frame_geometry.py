"""Execute the real Android frame dimension helper against edge-shaped screenshots."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from reproof.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment


ROOT = Path(__file__).resolve().parents[1]


class AndroidNativeFrameGeometryTests(unittest.TestCase):
    def test_real_native_dimension_helper_emits_even_bounded_ratio_preserving_frames(self):
        source = (ROOT / "android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt").read_text()
        start = source.index("private fun evenFrameDimensions(")
        end = source.index("\n\nclass LiveInstrumentation", start)
        helper = source[start:end]
        program = helper + r'''
private fun requireDimensions(width: Int, height: Int, maxWidth: Int): Pair<Int, Int> =
    evenFrameDimensions(width, height, maxWidth) ?: error("missing dimensions")

fun main() {
    val portrait = requireDimensions(1080, 2400, 960)
    check(portrait.first == 960)
    check(portrait.second in setOf(2132, 2134))
    check(portrait.first % 2 == 0 && portrait.second % 2 == 0)
    check(portrait.first <= 960)
    check(kotlin.math.abs(portrait.second.toDouble() / portrait.first - 2400.0 / 1080.0) < 0.01)

    val landscape = requireDimensions(2400, 1080, 959)
    check(landscape == Pair(958, 432))
    check(landscape.first <= 959)

    check(requireDimensions(960, 2400, 960) == Pair(960, 2400))
    check(requireDimensions(959, 1001, 959).first == 958)
    check(requireDimensions(1, 1, 2) == Pair(2, 2))
    check(evenFrameDimensions(1, 1, 1) == null)
    println("android-native-frame-geometry-passed")
}
'''
        java_home = _java_home()
        _ensure_built(java_home)
        compiler = (ROOT / "tools/kotlin-instrumenter/build/classes/classpath").read_text().strip()
        stdlib = next(name for name in compiler.split(os.pathsep) if name.endswith("kotlin-stdlib-2.2.10.jar"))
        java = str(Path(java_home) / "bin/java")
        with tempfile.TemporaryDirectory(prefix="repro-native-frame-geometry-") as directory:
            root = Path(directory)
            (root / "Geometry.kt").write_text(program)
            compiled = subprocess.run(
                [java, "-cp", compiler, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                 "-jvm-target", "17", "-classpath", stdlib, "-d", str(root / "classes"),
                 str(root / "Geometry.kt")], capture_output=True, text=True, timeout=60,
                env=_subprocess_environment(java_home))
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-2000:])
            result = subprocess.run(
                [java, "-cp", str(root / "classes") + os.pathsep + stdlib, "GeometryKt"],
                capture_output=True, text=True, timeout=20, env=_subprocess_environment(java_home))
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            self.assertEqual(result.stdout.strip(), "android-native-frame-geometry-passed")


if __name__ == "__main__":
    unittest.main()
