from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
JAVA_HOME = Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home")
SDK_HOME = Path.home() / "Library/Android/sdk"
GRADLE_ROOT = Path.home() / ".gradle/wrapper/dists/gradle-8.14.5-bin"


class G1bNativeCompileTests(unittest.TestCase):
    def test_android_v2_helper_compiles_offline(self):
        distributions = sorted(GRADLE_ROOT.glob("*/gradle-8.14.5/bin/gradle"))
        self.assertEqual(len(distributions), 1, "Installed Gradle 8.14.5 is required")
        self.assertTrue((JAVA_HOME / "bin/javac").is_file(), "Installed Java 17 is required")
        self.assertTrue((SDK_HOME / "platforms").is_dir(), "Installed Android SDK is required")
        with tempfile.TemporaryDirectory(prefix="repro-g1b-gradle-") as temporary:
            root = Path(temporary)
            environment = {
                "PATH": f"{JAVA_HOME / 'bin'}:/usr/bin:/bin",
                "JAVA_HOME": str(JAVA_HOME),
                "ANDROID_HOME": str(SDK_HOME),
                "ANDROID_SDK_ROOT": str(SDK_HOME),
                "GRADLE_USER_HOME": str(root / "gradle-home"),
                "GRADLE_RO_DEP_CACHE": str(Path.home() / ".gradle/caches"),
                "TMPDIR": temporary,
            }
            result = subprocess.run(
                [str(distributions[0]), "--offline", "--no-daemon",
                 "--project-cache-dir", str(root / "project-cache"),
                 ":live:compileDebugKotlin", ":driver:compileDebugKotlin"],
                cwd=ROOT / "android", env=environment,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])

    def test_ios_v2_helper_builds_for_testing_without_signing(self):
        with tempfile.TemporaryDirectory(prefix="repro-g1b-xcode-") as temporary:
            environment = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TMPDIR": temporary,
            }
            result = subprocess.run(
                ["/usr/bin/xcodebuild", "build-for-testing",
                 "-project", str(ROOT / "live-ios/ReproLive.xcodeproj"),
                 "-scheme", "ReproLive",
                 "-destination", "generic/platform=iOS Simulator",
                 "-derivedDataPath", str(Path(temporary) / "DerivedData"),
                 "-disableAutomaticPackageResolution",
                 "-onlyUsePackageVersionsFromResolvedFile",
                 "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO"],
                cwd=ROOT, env=environment,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])


class G1bNativeExecutableBoundaryTests(unittest.TestCase):
    def test_ios_startup_and_cleanup_preserve_target_lifetime(self):
        source=(ROOT/'live-ios/Tests/LiveControlTests.swift').read_text()
        body=source[source.index('    func testControlSession() {'):source.index('    private func launchTarget(')]
        program='''import Foundation
private enum BridgeFailure: Error {
 case timeout, requestFailed, invalidResponse
 var safeCode: String { "synthetic_failure" }
}
private class NativeAuthorityContext {
 static let shared = NativeAuthorityContext()
 let enabled = true
 let valid = true
}
private struct AuthorityGrant {
 func matches(_ context: NativeAuthorityContext, requireLive: Bool) -> Bool { true }
}
private struct Command { let id = "operation-one"; let action = "authority_cleanup"; let authority: AuthorityGrant? = nil }
private struct Envelope { let stop: Bool; let command: Command? }
private struct CleanupTerminationEvidence {}
private struct NetworkCounterEvidence {}
private struct Result {
 let ok = true; let error: String? = nil
 let cleanupEvidence: CleanupTerminationEvidence? = nil
 let networkEvidence: NetworkCounterEvidence? = nil
}
private class Target {
 enum State { case runningForeground }
 let foreground: Bool
 var launches = 0
 var terminations = 0
 init(_ foreground: Bool) { self.foreground = foreground }
 func wait(for state: State, timeout: Int) -> Bool { foreground }
 func terminate() { terminations += 1 }
}
private class Bridge {
 let rejectStarted: Bool
 let cleanup: Bool
 var shutdowns = 0
 var nextCount = 0
 var acknowledgements = 0
 init(_ rejectStarted: Bool, cleanup: Bool) { self.rejectStarted = rejectStarted; self.cleanup = cleanup }
 func ready() throws -> AuthorityGrant? { AuthorityGrant() }
 func started(authority: AuthorityGrant?) throws {
  if rejectStarted { throw BridgeFailure.requestFailed }
 }
 func shutdown() { shutdowns += 1 }
 func next() throws -> Envelope {
  nextCount += 1
  if cleanup && nextCount == 1 { return Envelope(stop: false, command: Command()) }
  if cleanup && nextCount == 2 {
   Thread.sleep(forTimeInterval: 0.25)
   return Envelope(stop: false, command: nil)
  }
  return Envelope(stop: true, command: nil)
 }
 func ack(id: String, ok: Bool, error: String?, timing: String, authority: AuthorityGrant?, cleanupEvidence: CleanupTerminationEvidence?, networkEvidence: NetworkCounterEvidence?) throws { acknowledgements += 1 }
}
private class Probe {
 let targetApplication: Target
 let bridge: Bridge
 var targetWasLaunched = false
 var failures: [String] = []
 var cleaned = false
 var frames = 0
 init(foreground: Bool, rejectStarted: Bool, cleanup: Bool = false) {
  targetApplication = Target(foreground)
  bridge = Bridge(rejectStarted, cleanup: cleanup)
 }
 func makeBridge() -> Bridge? { bridge }
 func launchTarget() { targetApplication.launches += 1 }
 func XCTFail(_ message: String) { failures.append(message) }
 func sendFrame(using bridge: Bridge) throws {
  if cleaned { throw BridgeFailure.requestFailed }
  frames += 1
 }
 func execute(_ command: Command) -> Result {
  targetApplication.terminate(); cleaned = true; return Result()
 }
''' + body + '''
}
var results: [[Int]] = []
for (foreground, rejectStarted) in [(true, false), (false, false), (true, true)] {
 let probe = Probe(foreground: foreground, rejectStarted: rejectStarted)
 probe.testControlSession()
 results.append([probe.targetApplication.launches, probe.targetApplication.terminations,
                 probe.bridge.shutdowns, probe.failures.count])
}
private let cleanup = Probe(foreground: true, rejectStarted: false, cleanup: true)
cleanup.testControlSession()
results.append([cleanup.targetApplication.terminations, cleanup.failures.count,
                cleanup.frames, cleanup.bridge.acknowledgements])
print(String(data: try JSONSerialization.data(withJSONObject: results), encoding: .utf8)!)
'''
        with tempfile.TemporaryDirectory(prefix='repro-g1b-startup-probe-') as temporary:
            root=Path(temporary);source_file=root/'StartupProbe.swift';binary=root/'startup-probe'
            source_file.write_text(program)
            compiled=subprocess.run(['/usr/bin/xcrun','swiftc',str(source_file),'-o',str(binary)],
                                    capture_output=True,text=True,timeout=90)
            self.assertEqual(compiled.returncode,0,(compiled.stdout+compiled.stderr)[-3000:])
            executed=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
            self.assertEqual(executed.returncode,0,executed.stderr[-2000:])
            self.assertEqual(json.loads(executed.stdout),[[1,1,1,0],[1,1,1,1],[1,1,1,1],[2,0,1,1]])

    def test_android_stop_revokes_old_effect_before_waiting_for_input_lock(self):
        source=(ROOT/"android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt").read_text()
        effect=source[source.index("    private fun effectAuthorized("):source.index("    private fun targetAppVisible(")]
        stop=source[source.index("    private fun stopFromServer("):source.index("    private fun cleanupAndFinish(")]
        grant=source[source.index("    private data class AuthorityGrant("):source.index("    private data class LiveCommand(")]
        program='''import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
private const val NATIVE_PROTOCOL_VERSION = 2
private const val NATIVE_CLOCK_ID = "android-elapsed-realtime"
private object SystemClock { @Volatile var now = 100L; fun elapsedRealtime() = now }
private data class LiveConfig(val protocolVersion: Int? = 2, val helperIncarnation: String = "helper-one", val hostIncarnation: String = "host-one", val providerIncarnation: String = "provider-one")
private class NativeProbe {
 private var config: LiveConfig? = LiveConfig()
 private val nativeIncarnation = "native-one"
 private val inputLock = Any()
 private val effectGrant = ThreadLocal<AuthorityGrant?>()
 @Volatile private var cleanupGrant: AuthorityGrant? = null
 @Volatile private var cleanupFailed = false
 private var screenshotRunning = AtomicBoolean(true)
 private fun cancelPointersUnsafe() = true
''' + grant + effect + stop + '''
 fun run() {
  val original = AuthorityGrant(2, "operation-one", "a".repeat(64), "b".repeat(64), "project-one", "session-one", "controller-one", 1, 1, "host-one", "helper-one", "provider-one", "native-one", NATIVE_CLOCK_ID, 1000)
  effectGrant.set(original)
  check(effectAuthorized())
  SystemClock.now = 1100
  check(!effectAuthorized())
  val current = original.copy(operationId = "operation-two", sequence = 2, nativeDeadlineMs = 2000)
  effectGrant.set(current)
  check(effectAuthorized())
  var stopThread: Thread? = null
  var afterStop = true
  synchronized(inputLock) {
   val cleanup = current.copy(operationId = "cleanup-one", sequence = 3, nativeDeadlineMs = 3000)
   stopThread = thread(isDaemon = true) { stopFromServer(cleanup) }
   val limit = System.nanoTime() + 2_000_000_000L
   while (stopThread!!.state != Thread.State.BLOCKED && System.nanoTime() < limit) Thread.yield()
   check(stopThread!!.state == Thread.State.BLOCKED)
   afterStop = effectAuthorized()
  }
  stopThread!!.join(2000)
  check(!stopThread!!.isAlive)
  check(!afterStop)
 }
}
fun main() { NativeProbe().run() }
'''
        classpath_file=ROOT/"tools/kotlin-instrumenter/build/classes/classpath"
        self.assertTrue(classpath_file.is_file(),"Installed offline Kotlin classpath is required")
        classpath=classpath_file.read_text().strip()
        java=JAVA_HOME/"bin/java"
        with tempfile.TemporaryDirectory(prefix="repro-g1b-kotlin-probe-") as temporary:
            root=Path(temporary);source_file=root/"StopProbe.kt";classes=root/"classes"
            source_file.write_text(program);classes.mkdir()
            compiled=subprocess.run(
                [str(java),"-cp",classpath,"org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                 "-no-stdlib","-no-reflect","-classpath",classpath,"-d",str(classes),str(source_file)],
                capture_output=True,text=True,timeout=90)
            self.assertEqual(compiled.returncode,0,(compiled.stdout+compiled.stderr)[-2000:])
            executed=subprocess.run([str(java),"-cp",str(classes)+os.pathsep+classpath,"StopProbeKt"],
                                    capture_output=True,text=True,timeout=20)
            self.assertEqual(executed.returncode,0,(executed.stdout+executed.stderr)[-2000:])

    def test_ios_raw_decoder_rejects_ambiguous_keys_and_invalid_authority_types(self):
        source=(ROOT/"live-ios/Tests/LiveControlTests.swift").read_text()
        helpers=source[source.index("private struct AnyCodingKey:"):source.index("private func nativeContinuousTimeMS()")]
        grant=source[source.index("private struct AuthorityGrant:"):source.index("private struct LiveCommand:")]
        base={"protocolVersion":2,"operationId":"operation-one","operationFingerprint":"a"*64,
              "payloadDigest":"b"*64,"projectId":"project-one","sessionId":"session-one",
              "controllerId":"controller-one","sequence":1,"ownershipGeneration":1,
              "hostIncarnation":"host-one","helperIncarnation":"helper-one",
              "providerIncarnation":"provider-one","nativeIncarnation":"native-one",
              "nativeClockId":"ios-mach-continuous","nativeDeadlineMs":100000}
        valid=json.dumps({"authority":base},separators=(",",":"))
        cases=[{"payload":valid,"expected":True},
               {"payload":valid.replace('"sequence":1','"sequence":1,"sequence":2'),"expected":False},
               {"payload":valid.replace('"sequence":1','"sequence":1,"sequen\\u0063e":2'),"expected":False}]
        for key in ('protocolVersion', 'sequence', 'ownershipGeneration', 'nativeDeadlineMs'):
            for value in (float(base[key]), True, str(base[key])):
                cases.append({'payload': json.dumps({'authority': {**base, key: value}}),
                              'expected': False})
        cases.append({'payload': json.dumps({'authority': {**base, 'unexpectedField': 'ignored'}}),
                      'expected': False})
        program='''import Foundation
import CoreFoundation
private let nativeProtocolVersion = 2
private let nativeClockID = "ios-mach-continuous"
private func nativeContinuousTimeMS() -> Int64 { 0 }
private enum BridgeFailure: Error { case invalidResponse }
private class NativeAuthorityContext {
 let helperIncarnation: String? = "helper-one"
 let hostIncarnation: String? = "host-one"
 let providerIncarnation: String? = "provider-one"
 let nativeIncarnation = "native-one"
}
''' + helpers + grant + '''
private struct Envelope: Decodable { let authority: AuthorityGrant }
let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
var results: [Bool] = []
for item in cases {
 let data = Data((item["payload"] as! String).utf8)
 results.append(strictAuthorityNumberTypes(data) && (try? JSONDecoder().decode(Envelope.self, from: data)) != nil)
}
print(String(data: try JSONSerialization.data(withJSONObject: results), encoding: .utf8)!)
'''
        with tempfile.TemporaryDirectory(prefix="repro-g1b-swift-probe-") as temporary:
            root=Path(temporary);source_file=root/"WireProbe.swift";binary=root/"wire-probe";case_file=root/"cases.json"
            source_file.write_text(program);case_file.write_text(json.dumps(cases))
            environment={"TMPDIR":temporary,
                         "PATH":"/usr/bin:/bin:/usr/sbin:/sbin",
                         "CLANG_MODULE_CACHE_PATH":str(root/"ModuleCache"),
                         "SWIFT_MODULECACHE_PATH":str(root/"ModuleCache")}
            compiled=subprocess.run(["/usr/bin/xcrun","swiftc",str(source_file),"-o",str(binary)],
                                    capture_output=True,text=True,timeout=90,env=environment)
            self.assertEqual(compiled.returncode,0,(compiled.stdout+compiled.stderr)[-2000:])
            executed=subprocess.run([str(binary),str(case_file)],capture_output=True,text=True,timeout=20,
                                    env=environment)
            self.assertEqual(executed.returncode,0,executed.stderr[-2000:])
            self.assertEqual(json.loads(executed.stdout),[case['expected'] for case in cases])


if __name__ == "__main__":
    unittest.main()
