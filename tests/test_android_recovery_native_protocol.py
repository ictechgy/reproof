"""Execute the real private native recovery state/barrier code without Android or device effects."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from reproof.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment

ROOT = Path(__file__).resolve().parents[1]


class AndroidRecoveryNativeProtocolTests(unittest.TestCase):
    def test_finite_grants_barriers_and_terminal_delivery(self):
        program = r'''
package io.reproof.live

fun denied(block: () -> Unit) {
    try { block(); error("invalid input admitted") } catch (_: IllegalArgumentException) {}
}
fun config(phase: String): AndroidRecoverySelection {
    val data = mapOf("schemaVersion" to 1, "kind" to "reproof-android-recovery-qualification", "phase" to phase,
        "attemptId" to "attempt_one", "sessionId" to "session_one", "deviceScopeDigest" to "a".repeat(64),
        "applicationId" to "android_app", "artifactDigest" to "b".repeat(64), "profileDigest" to "c".repeat(64),
        "helperArtifactDigest" to "d".repeat(64), "priorOwnershipGeneration" to 1)
    val bytes = recoveryCanonical(data).toByteArray(Charsets.US_ASCII)
    return AndroidRecoverySelection.parse(bytes, data, recoveryDigest(bytes))
}
fun grant(c: AndroidRecoverySelection, purpose: String, id: String, sequence: Int, deadline: Long = 100): AndroidRecoveryGrant {
    val data = mapOf("schemaVersion" to 1, "purpose" to purpose, "operationId" to id, "sequence" to sequence,
        "configurationDigest" to c.digest, "attemptId" to "attempt_one", "sessionId" to "session_one",
        "priorOwnershipGeneration" to 1, "recoveryOwnershipGeneration" to 2,
        "hostIncarnation" to "host_one", "helperIncarnation" to "helper_one", "providerIncarnation" to "provider_one",
        "nativeIncarnation" to "native_one", "nativeClockId" to "android-elapsed-realtime", "nativeDeadlineMs" to deadline,
        "payloadDigest" to recoveryDigest(recoveryCanonical(mapOf("configurationDigest" to c.digest, "purpose" to purpose)).toByteArray(Charsets.US_ASCII)))
    return AndroidRecoveryGrant.parse(data, c, "host_one", "helper_one", "provider_one", "native_one", 10)
}
fun evidence() = mapOf("normalAdmissionClosed" to true, "commandQueueDrained" to true,
    "inflightCompleted" to true, "pointersCancelled" to true, "captureWorkersStopped" to true,
    "inspectionMediaDrained" to true, "publicationClosed" to true, "admissionWatermark" to 12, "detachedCommandId" to "queued_one")
fun main() {
    var now = 10L
    val process = mapOf("pid" to 321, "startTicks" to "123456")
    val c = config("session")
    val p = AndroidRecoveryProtocol(c) { now }
    check(p.normalAllowed())
    val retire = grant(c, "old-retire", "retire_one", 1)
    check(p.admit(retire)); check(!p.normalAllowed()); check(!p.admit(retire))
    denied { p.admit(grant(c, "old-retire", "retire_two", 2)) }
    denied { p.admit(retire.copy(data = retire.data + ("sequence" to 2))) }
    check(p.status("retire_one")!!["pending"] == true)
    for (name in listOf("commandQueueDrained", "inflightCompleted", "pointersCancelled", "captureWorkersStopped", "inspectionMediaDrained", "publicationClosed")) {
        denied { p.complete(retire, process, evidence() + (name to false)) }
    }
    check(p.complete(retire, process, evidence()))
    val terminal = p.status("retire_one")!!
    check(!p.complete(retire, process, evidence()))
    check(p.status("retire_one") == terminal)
    val exit = grant(c, "old-exit", "exit_one", 2)
    check(p.admit(exit)); check(p.complete(exit, process, emptyMap()))
    val delivery = AndroidRecoveryExitDelivery()
    var exited = false
    delivery.afterSuccessfulWrite("exit_one", mapOf("pending" to true, "operationId" to "exit_one"))
    delivery.onTerminalDelivery("exit_one") { exited = true }
    check(!exited) // A pending response is not a terminal-delivery acknowledgment.
    delivery.afterSuccessfulWrite("other_exit", p.status("exit_one")!!); check(!exited)
    delivery.afterSuccessfulWrite("exit_one", p.status("exit_one")!!); check(exited)
    val late = AndroidRecoveryProtocol(c) { now }
    check(late.admit(retire)); now = 100
    check(late.status("retire_one")!!["status"] == "unknown")
    check(!late.complete(retire, process, evidence()))
    now = 10
    check(!late.normalAllowed()); check(!late.complete(retire, process, evidence()))
    val racing = AndroidRecoveryProtocol(c) { now }
    val start = java.util.concurrent.CountDownLatch(1)
    val winners = java.util.concurrent.atomic.AtomicInteger(0)
    val threads = (1..2).map { number -> Thread {
        start.await()
        try { if (racing.admit(grant(c, "old-retire", "racing_$number", number))) winners.incrementAndGet() }
        catch (_: IllegalArgumentException) {}
    }.apply { start() } }
    start.countDown(); threads.forEach { it.join(2000); check(!it.isAlive) }
    check(winners.get() == 1 && !racing.normalAllowed())
    val earlyDelivery = AndroidRecoveryExitDelivery(); var lateRegistrationCalled = false
    earlyDelivery.afterSuccessfulWrite("exit_one", p.status("exit_one")!!)
    earlyDelivery.onTerminalDelivery("exit_one") { lateRegistrationCalled = true }
    check(lateRegistrationCalled)
    val failedWrite = AndroidRecoveryExitDelivery(); var escaped = false
    failedWrite.onTerminalDelivery("exit_one") { escaped = true }
    val writeAttempt: () -> Unit = { throw java.io.IOException("synthetic failed response") }
    try { writeAttempt(); failedWrite.afterSuccessfulWrite("exit_one", p.status("exit_one")!!) }
    catch (_: java.io.IOException) {}
    check(!escaped)
    val probeConfig = config("probe"); val probe = AndroidRecoveryProtocol(probeConfig) { now }
    check(!probe.normalAllowed())
    denied { probe.admit(grant(probeConfig, "old-retire", "wrong_phase", 1)) }
    val launch = grant(probeConfig, "fresh-probe", "probe_one", 1)
    check(probe.admit(launch))
    val frame = mapOf("nativeFrameId" to 1, "frameSha256" to "e".repeat(64), "frameBytes" to 100,
        "width" to 960, "height" to 2000, "geometryVersion" to 1, "targetForeground" to true, "captureStartMs" to 5, "captureEndMs" to 9)
    denied { probe.complete(launch, process, frame + ("targetForeground" to false)) }
    check(probe.complete(launch, process, frame))
    denied { probe.admit(grant(probeConfig, "fresh-probe", "probe_again", 2)) }
    check(probe.admit(grant(probeConfig, "probe-retire", "probe_retire", 2)))
    // shutdownNow / interruption alone is never an observed termination.
    var stopped = false; now = 10
    check(!awaitRecoveryBarrier(20, { now }, { true }, { stopped }) { now += 1 })
    stopped = true
    check(!awaitRecoveryBarrier(20, { now }, { true }, { stopped }) {})
    now = 10
    check(awaitRecoveryBarrier(20, { now }, { true }, { stopped }) {})
    check(!awaitRecoveryBarrier(20, { now }, { false }, { true }) {})
    var interrupted = false
    try { awaitRecoveryBarrier(20, { now }, { true }, { false }) { throw InterruptedException() } }
    catch (_: InterruptedException) { interrupted = true }
    check(interrupted)
    // Wrong incarnation, clock, deadline and generation cannot enter the state machine.
    for ((key, value) in listOf("nativeIncarnation" to "native_other", "nativeClockId" to "wall-clock",
            "recoveryOwnershipGeneration" to 3, "nativeDeadlineMs" to 10L, "sequence" to 0, "purpose" to "tap")) {
        denied { AndroidRecoveryGrant.parse(retire.data + (key to value), c, "host_one", "helper_one", "provider_one", "native_one", 10) }
    }
    val statFields = MutableList(20) { "0" }; statFields[0] = "S"; statFields[19] = "123456"
    val stat = ("321 (command with ) parens) " + statFields.joinToString(" ")).toByteArray(Charsets.US_ASCII)
    check(parseRecoveryProcessStat(stat, 321) == process)
    denied { parseRecoveryProcessStat(stat, 322) }
    denied { parseRecoveryProcessStat("321 invalid".toByteArray(), 321) }
    denied { parseRecoveryProcessStat(ByteArray(4097) { 32 }, 321) }
    statFields[19] = "0"
    denied { parseRecoveryProcessStat(("321 (command) " + statFields.joinToString(" ")).toByteArray(), 321) }
    println("android-recovery-protocol-passed")
}
'''
        java_home = _java_home()
        _ensure_built(java_home)
        compiler = (ROOT / 'tools/kotlin-instrumenter/build/classes/classpath').read_text().strip()
        stdlib = next(item for item in compiler.split(os.pathsep) if item.endswith('kotlin-stdlib-2.2.10.jar'))
        java = str(Path(java_home) / 'bin/java')
        with tempfile.TemporaryDirectory(prefix='repro-recovery-jvm-') as directory:
            root = Path(directory)
            (root / 'RecoveryMain.kt').write_text(program)
            compiled = subprocess.run([java, '-cp', compiler, 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                '-jvm-target', '17', '-classpath', stdlib, '-d', str(root / 'classes'), str(root / 'RecoveryMain.kt'),
                str(ROOT / 'android/live/src/main/java/io/reproof/live/AndroidRecoveryProtocol.kt')],
                capture_output=True, text=True, timeout=60, env=_subprocess_environment(java_home))
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-4000:])
            result = subprocess.run([java, '-cp', str(root / 'classes') + os.pathsep + stdlib, 'io.reproof.live.RecoveryMainKt'],
                capture_output=True, text=True, timeout=20, env=_subprocess_environment(java_home))
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            self.assertEqual(result.stdout.strip(), 'android-recovery-protocol-passed')

    def test_native_integration_has_distinct_recovery_boundary(self):
        source = (ROOT / 'android/live/src/main/java/io/reproof/live/LiveInstrumentation.kt').read_text()
        self.assertIn('action != MotionEvent.ACTION_CANCEL || recoveryGrant.purpose', source)
        self.assertNotIn('recoveryEffect', source)
        self.assertIn('watchdogExecutor.isTerminated && screenshotExecutor.isTerminated', source)
        self.assertIn('recoveryCommandDrained.count == 0L && nativeServer.recoveryInflightEmpty()', source)
        self.assertIn('recoveryExitDelivery.afterSuccessfulWrite(id, result)', source)
        self.assertIn('File("/proc/self/stat")', source)
        self.assertIn('normalClosed = true; ready.set(false)', source)
        self.assertIn('private fun recoveryHandshake()', source)
        self.assertIn('targetPackage == "io.reproof.live"', source)
        self.assertNotIn('pm clear', source[source.index('    private fun performRecovery('):source.index('    private fun stopFromServer(')])


if __name__ == '__main__':
    unittest.main()
