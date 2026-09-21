import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from reproloop.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment


ROOT = Path(__file__).resolve().parents[1]
ANDROID_SOURCE = ROOT / "android/live/src/main/java/io/reproloop/live/LiveInstrumentation.kt"
IOS_SOURCE = ROOT / "live-ios/Tests/LiveControlTests.swift"


@unittest.skipUnless(shutil.which("xcrun"), "Apple toolchain unavailable")
class NativeFrameBufferTests(unittest.TestCase):
    def test_kotlin_buffer_order_cursor_fallback_eviction_and_fps_rounding(self):
        source = ANDROID_SOURCE.read_text()
        start = source.index("internal class NativeFrameBuffer")
        end = source.index("\n\nclass LiveInstrumentation", start)
        helper = source[start:end]
        program = "import kotlin.math.ceil\n" + helper + r'''
fun main() {
    val buffer = NativeFrameBuffer(maxFrames = 3, maxBytes = 12)
    check(buffer.latest() == null)
    val first = byteArrayOf(1, 2, 3)
    check(buffer.append(1, first))
    first[0] = 9
    check(buffer.latest()!!.id == 1L && buffer.latest()!!.bytes[0] == 1.toByte())
    check(buffer.append(2, byteArrayOf(2, 2, 2)))
    check(buffer.append(3, byteArrayOf(3, 3, 3)))
    check(buffer.after(0)!!.id == 1L)
    check(buffer.after(1)!!.id == 2L)
    check(buffer.after(3)!!.id == 3L) // no newer frame: latest duplicate
    check(buffer.append(4, byteArrayOf(4, 4, 4)))
    check(buffer.after(0)!!.id == 2L) // oldest retained frame after eviction
    check(!buffer.append(4, byteArrayOf(4)))
    check(!buffer.append(0, byteArrayOf(0)))
    check(!buffer.append(5, byteArrayOf()))
    try {
        buffer.after(5)
        error("cursor ahead was accepted")
    } catch (error: IllegalArgumentException) {
        check(error.message == "frame_cursor_ahead")
    }
    val byteLimited = NativeFrameBuffer(maxFrames = 16, maxBytes = 5)
    check(byteLimited.append(1, byteArrayOf(1, 1, 1)))
    check(byteLimited.append(2, byteArrayOf(2, 2, 2)))
    check(byteLimited.after(0)!!.id == 2L)
    check(screenshotIntervalMillis(15) == 67L)
    check(screenshotIntervalMillis(60) == 17L)
    println("kotlin-native-frame-buffer-passed")
}
'''
        java_home = _java_home()
        _ensure_built(java_home)
        compiler = (ROOT / "tools/kotlin-instrumenter/build/classes/classpath").read_text().strip()
        stdlib = next(name for name in compiler.split(os.pathsep) if name.endswith("kotlin-stdlib-2.2.10.jar"))
        java = str(Path(java_home) / "bin/java")
        with tempfile.TemporaryDirectory(prefix="repro-native-frame-buffer-kotlin-") as directory:
            root = Path(directory)
            source_file = root / "FrameBuffer.kt"
            source_file.write_text(program)
            compiled = subprocess.run(
                [java, "-cp", compiler, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                 "-jvm-target", "17", "-classpath", stdlib, "-d", str(root / "classes"),
                 str(source_file)], capture_output=True, text=True, timeout=60,
                env=_subprocess_environment(java_home),
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-2000:])
            executed = subprocess.run(
                [java, "-cp", str(root / "classes") + os.pathsep + stdlib, "FrameBufferKt"],
                capture_output=True, text=True, timeout=20,
                env=_subprocess_environment(java_home),
            )
            self.assertEqual(executed.returncode, 0, executed.stderr[-1500:])
            self.assertEqual(executed.stdout.strip(), "kotlin-native-frame-buffer-passed")

    def test_swift_buffer_order_cursor_fallback_eviction_and_byte_cap(self):
        source = IOS_SOURCE.read_text()
        start = source.index("private struct NativeFrameBuffer")
        end = source.index("\n\n/// Local HTTP bridge", start)
        helper = source[start:end]
        program = "import Foundation\n\n" + helper + r'''
private var buffer = NativeFrameBuffer()
assert(buffer.latest() == nil)
assert(buffer.append(id: 1, body: Data([1, 2, 3])))
assert(buffer.append(id: 2, body: Data([2, 2, 2])))
assert(buffer.append(id: 3, body: Data([3, 3, 3])))
assert(try! buffer.after(0)?.id == 1)
assert(try! buffer.after(1)?.id == 2)
assert(try! buffer.after(3) == nil) // cursor == newest: no newer frame
for id in 4...17 {
    assert(buffer.append(id: Int64(id), body: Data([UInt8(id), UInt8(id), UInt8(id)])))
}
assert(try! buffer.after(0)?.id == 2) // oldest retained frame after eviction
assert(buffer.append(id: 4, body: Data([4])) == false)
assert(buffer.append(id: 0, body: Data([0])) == false)
do {
    _ = try buffer.after(18)
    fatalError("cursor ahead was accepted")
} catch NativeFrameBuffer.ReadError.cursorAhead {
}
private var byteLimited = NativeFrameBuffer()
assert(byteLimited.append(id: 1, body: Data(repeating: 1, count: NativeFrameBuffer.maxBytes)) == true)
assert(byteLimited.append(id: 2, body: Data([2])) == true)
assert(try! byteLimited.after(0)?.id == 2)
print("swift-native-frame-buffer-passed")
'''
        with tempfile.TemporaryDirectory(prefix="repro-native-frame-buffer-swift-") as directory:
            root = Path(directory)
            source_file = root / "FrameBuffer.swift"
            binary = root / "frame-buffer"
            source_file.write_text(program)
            developer_dir = subprocess.run(
                ["xcode-select", "-p"], capture_output=True, text=True,
                timeout=10).stdout.strip()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "DEVELOPER_DIR": developer_dir,
            }
            compiled = subprocess.run(
                ["xcrun", "swiftc", "-swift-version", "5", str(source_file), "-o", str(binary)],
                capture_output=True, text=True, timeout=60, env=environment,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-2000:])
            executed = subprocess.run(
                [str(binary)], capture_output=True, text=True, timeout=20, env=environment,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr[-1500:])
            self.assertEqual(executed.stdout.strip(), "swift-native-frame-buffer-passed")


if __name__ == "__main__":
    unittest.main()
