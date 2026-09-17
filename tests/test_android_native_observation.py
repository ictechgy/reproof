"""Execute the native JSON/launch boundary methods with inert platform effects."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from reproloop.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment

ROOT = Path(__file__).resolve().parents[1]


class AndroidNativeObservationTests(unittest.TestCase):
    def test_native_status_arrays_and_observation_launch_contract(self):
        source = (ROOT / 'android/live/src/main/java/io/reproloop/live/LiveInstrumentation.kt').read_text()
        status = source[source.index('    private fun statusSnapshot()'):source.index('    private fun observeSample()')]
        launch = source[source.index('    private fun launchGeneralTarget('):source.index('    private fun terminateGeneralTarget(')]
        ready = source[source.index('        fun isReady():'):source.index('        fun isStopped():')]
        guard = source[source.index('    private fun targetFrameAllowed('):source.index('    private fun captureFrame()')]
        program = '''
import org.json.JSONObject
import org.json.JSONArray
import java.util.concurrent.atomic.AtomicBoolean
private const val NATIVE_PROTOCOL_VERSION = 2
private const val HELPER_VERSION = 2
private const val NATIVE_CLOCK_ID = "android-elapsed-realtime"
private object SystemClock {
 var now = 100L
 fun elapsedRealtime() = 100L
 fun uptimeMillis() = now
 fun sleep(value: Long) { now += value }
}
private data class Profile(val general: Boolean = true, val observations: Set<String> = setOf("pixels", "logs")) {
 val nativeDigest = "a".repeat(64)
 val actions = linkedSetOf("tap", "launch", "home")
 val applicationId = "inventory_app"
 val componentName = "com.example.inventory/.MainActivity"
}
private data class Config(val profile: Profile = Profile()) {
 val profileDigest = "b".repeat(64)
 val targetPackage = "com.example.inventory"
 val protocolVersion: Int? = 2
 val helperIncarnation = "helper"
 val hostIncarnation = "host"
 val providerIncarnation = "provider"
 val recordSdk = false
}
private typealias LiveConfig = Config
private class Server { fun isReady() = true; fun isStopped() = false }
private class ReadinessProbe(val config: Config) {
 val ready = AtomicBoolean(true)
 val firstFrame = AtomicBoolean(false)
''' + ready + '''
}
private class Probe {
 var config: Config? = Config()
 private val server: Server? = Server()
 private val nativeIncarnation = "native"
 private val inputLock = Any()
 private val captureLock = Any()
 private val generalTargetReady = AtomicBoolean(false)
 private val pointers = linkedMapOf(1 to true, 2 to true)
 var authorized = true
 val commands = mutableListOf<String>()
 private fun cancelPointersUnsafe() = true
 private fun effectAuthorized() = authorized
 var visible = true
 var captureAvailable = true
 var captures = 0
 private fun targetAppVisible() = visible
 private fun captureFrame(): Boolean { captures += 1; return captureAvailable }
 private fun runShellAndRead(command: String): String? {
  commands.add(command)
  return "Status: ok\\nActivity: com.example.inventory/.MainActivity"
 }
 fun status() = statusSnapshot()
 fun launch(value: JSONObject) = launchGeneralTarget(value)
 fun canCapture() = targetFrameAllowed(config!!)
''' + status + launch + guard + '''
}
fun main() {
 val startup = ReadinessProbe(Config())
 check(startup.isReady()) { "General helper must accept launch before the first target frame" }
 startup.ready.set(false)
 check(!startup.isReady())
 val legacy = ReadinessProbe(Config(Profile(general = false)))
 check(!legacy.isReady())
 legacy.firstFrame.set(true)
 check(legacy.isReady())
 val probe = Probe()
 check(!probe.canCapture()) { "No general app pixels before its authorized launch" }
 val status = probe.status()
 val capabilities = status.opt("capabilities") as JSONObject
 check(capabilities.opt("actions") is JSONArray) { "Native action capability must be a JSON array" }
 check(status.opt("activePointerIds") is JSONArray) { "Native pointer state must be a JSON array" }
 check(capabilities.opt("viewsObservationLaunchVersion") == 2)
 val run = "11111111-1111-4111-8111-111111111111"
 val digest = "a".repeat(64)
 fun payload() = JSONObject().put("applicationId", "inventory_app")
 fun valid() = payload().put("appLogRunId", run).put("appLogProfileDigest", digest)
 check(probe.launch(valid()).first)
 check(probe.captures > 0) { "Launch acknowledgement requires its first native frame" }
 check(probe.canCapture())
 probe.visible = false
 check(!probe.canCapture()) { "Other application pixels remain excluded" }
 probe.visible = true
 check(probe.commands.last().contains("am start -S -W -n "))
 check(probe.commands.last().contains("--es repro_mode observe"))
 check(probe.commands.last().contains("--es repro_observation_profile $digest"))
 val commands = probe.commands.size
 for (bad in listOf(payload(), payload().put("appLogRunId", run),
    valid().put("appLogRunId", "bad"), valid().put("appLogProfileDigest", "bad"),
    valid().put("applicationId", "other"), valid().put("shell", "unexpected"))) {
  check(probe.launch(bad) == (false to "invalid_config"))
 }
 check(probe.commands.size == commands)
 probe.authorized = false
 check(probe.launch(valid()) == (false to "authority_expired"))
 check(probe.commands.size == commands)
 probe.authorized = true
 probe.config = Config(Profile(observations = setOf("pixels")))
 check(probe.launch(valid()) == (false to "invalid_config"))
 check(probe.launch(payload()).first)
 check(probe.commands.last() == "am start -S -W -n com.example.inventory/.MainActivity") {
  "Observation policy must not change the launch starting condition"
 }
 probe.captureAvailable = false
 check(probe.launch(payload()) == (false to "target_unavailable"))
 check(!probe.canCapture()) { "Failed startup must not enable background pixel collection" }
 println("native-observation-contract-passed")
}
'''
        json_boundary = '''package org.json
class JSONObject {
 companion object { val NULL = Any() }
 private val values = linkedMapOf<String, Any?>()
 fun put(key: String, value: Any?): JSONObject { values[key] = value; return this }
 fun opt(key: String): Any? = values[key]
 fun optString(key: String): String = values[key] as? String ?: ""
 fun has(key: String) = values.containsKey(key)
 fun keys() = values.keys.iterator()
}
class JSONArray(values: Collection<*>) { val entries = values.toList() }
'''
        java_home = _java_home(); _ensure_built(java_home)
        compiler = (ROOT / 'tools/kotlin-instrumenter/build/classes/classpath').read_text().strip()
        stdlib = next(name for name in compiler.split(os.pathsep) if name.endswith('kotlin-stdlib-2.2.10.jar'))
        java = str(Path(java_home) / 'bin/java')
        with tempfile.TemporaryDirectory(prefix='repro-native-observation-') as directory:
            root = Path(directory)
            (root / 'Probe.kt').write_text(program)
            (root / 'Json.kt').write_text(json_boundary)
            compiled = subprocess.run([java, '-cp', compiler, 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                '-jvm-target', '17', '-classpath', stdlib, '-d', str(root / 'classes'),
                str(root / 'Probe.kt'), str(root / 'Json.kt')], capture_output=True, text=True, timeout=60,
                env=_subprocess_environment(java_home))
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-2000:])
            result = subprocess.run([java, '-cp', str(root / 'classes') + os.pathsep + stdlib, 'ProbeKt'],
                capture_output=True, text=True, timeout=20, env=_subprocess_environment(java_home))
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            self.assertEqual(result.stdout.strip(), 'native-observation-contract-passed')
