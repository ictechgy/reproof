package io.reproof.sdk

import io.reproof.fixture.*
import org.junit.Assert.*
import org.junit.Test
import java.util.ArrayDeque
import java.util.concurrent.Executor

/** Executes the SDK's real queue/state machine with inert handlers, without Android or devices. */
class FixtureExecutionTest {
    private class Queue : Executor {
        val tasks = ArrayDeque<Runnable>()
        override fun execute(command: Runnable) { tasks.add(command) }
        fun next() { tasks.removeFirst().run() }
    }
    private fun challenge() = FixtureChallenge("attempt_1", "a".repeat(64), "session_1", "b".repeat(64),
        "c".repeat(64), "build_1", "app_1", "d".repeat(64), "e".repeat(64), "fixture_1", 1,
        "f".repeat(64), "1".repeat(64), null, "host_1", "helper_1", "provider_1", 1)
    private fun binding(phase: String) = FixtureOperationBinding("operation_$phase", "allocation_1", 1,
        when (phase) { "prepare" -> "2"; "check" -> "3"; else -> "4" }.repeat(64), "5".repeat(64), "recipe_$phase", phase)
    private fun rejected(block: () -> Unit) { try { block(); fail("invalid request admitted") } catch (_: IllegalArgumentException) {} }
    private open class Handler : ReproofFixtureHandler {
        val effects = mutableListOf<String>()
        override fun prepare(attempt: ReproofFixtureAttempt): Boolean { effects += "prepare"; return true }
        override fun check(attempt: ReproofFixtureAttempt): Boolean { effects += "check"; return true }
        override fun cleanup(attempt: ReproofFixtureAttempt): Boolean { effects += "cleanup"; return true }
    }
    @Test fun completeLifecycleIsSerializedAndDuplicateDoesNotRepeatEffect() {
        val queue = Queue(); val handler = Handler(); val terminals = mutableListOf<FixtureTerminal>()
        val engine = FixtureExecution(handler, { true }, { it == 42 }, { 10 }, queue)
        fun request(phase: FixtureOperation) = engine.request(phase, challenge(),
            binding(if (phase == FixtureOperation.CANCEL) "prepare" else phase.name.lowercase()), 100, 42) { terminals += it }
        request(FixtureOperation.PREPARE); request(FixtureOperation.PREPARE)
        assertEquals(1, queue.tasks.size); assertTrue(terminals.isEmpty())
        rejected { request(FixtureOperation.CHECK) }
        queue.next(); assertEquals(listOf(FixtureTerminal.READY), terminals)
        request(FixtureOperation.PREPARE); queue.next()
        assertEquals(listOf("prepare"), handler.effects)
        request(FixtureOperation.CHECK); queue.next()
        rejected { request(FixtureOperation.CLEANUP) }
        request(FixtureOperation.CANCEL); queue.next()
        request(FixtureOperation.CLEANUP); queue.next()
        assertEquals(listOf("prepare", "check", "cleanup"), handler.effects)
        assertEquals(FixtureTerminal.CLEANED, terminals.last())
    }
    @Test fun cancellationCannotClaimQuiescenceBeforeRunningHandlerReturns() {
        val queue = Queue(); val terminals = mutableListOf<Pair<FixtureOperation, FixtureTerminal>>()
        lateinit var engine: FixtureExecution
        val handler = object : Handler() {
            override fun prepare(attempt: ReproofFixtureAttempt): Boolean {
                effects += "prepare"
                engine.request(FixtureOperation.CANCEL, challenge(), binding("prepare"), 100, 42) {
                    terminals += FixtureOperation.CANCEL to it
                }
                assertTrue(attempt.cancelled)
                assertTrue(terminals.isEmpty())
                return true // Cancellation must defeat this late ready return.
            }
        }
        engine = FixtureExecution(handler, { true }, { it == 42 }, { 10 }, queue)
        engine.request(FixtureOperation.PREPARE, challenge(), binding("prepare"), 100, 42) {
            terminals += FixtureOperation.PREPARE to it
        }
        queue.next()
        assertEquals(listOf(FixtureOperation.PREPARE to FixtureTerminal.CANCELLED), terminals)
        queue.next()
        assertEquals(FixtureOperation.CANCEL to FixtureTerminal.CANCELLED, terminals.last())
        rejected { engine.request(FixtureOperation.CHECK, challenge(), binding("check"), 100, 42) {} }
    }
    @Test fun deadlineAndIdentityAreRecheckedBeforeQueuedEffect() {
        val queue = Queue(); val handler = Handler(); var now = 10L; var valid = true
        val results = mutableListOf<FixtureTerminal>()
        val engine = FixtureExecution(handler, { true }, { valid && it == 42 }, { now }, queue)
        engine.request(FixtureOperation.PREPARE, challenge(), binding("prepare"), 20, 42) { results += it }
        now = 20; valid = false
        queue.next()
        assertTrue(handler.effects.isEmpty()); assertEquals(listOf(FixtureTerminal.FAILED), results)
    }
    @Test fun wrongFreshnessAndFencingFailBeforeEffects() {
        val queue = Queue(); val handler = Handler()
        val engine = FixtureExecution(handler, { true }, { it == 42 }, { 10 }, queue)
        rejected { engine.request(FixtureOperation.PREPARE, challenge(), binding("prepare"), 100, 7) {} }
        engine.request(FixtureOperation.PREPARE, challenge(), binding("prepare"), 100, 42) {}
        rejected { engine.request(FixtureOperation.PREPARE, challenge().copy(nonce = "9".repeat(64)), binding("prepare"), 100, 42) {} }
        rejected { engine.request(FixtureOperation.PREPARE, challenge(), binding("prepare").copy(payloadDigest = "9".repeat(64)), 100, 42) {} }
        queue.next()
        rejected { engine.request(FixtureOperation.CHECK, challenge(), binding("check").copy(generation = 2), 100, 42) {} }
        rejected { engine.request(FixtureOperation.CHECK, challenge(), binding("check").copy(allocationId = "allocation_2"), 100, 42) {} }
        assertEquals(listOf("prepare"), handler.effects)
    }
    @Test fun lostAuthorizerNeverBecomesReady() {
        val queue = Queue(); var authorized = true; val terminals = mutableListOf<FixtureTerminal>()
        val handler = Handler()
        val engine = FixtureExecution(handler, { true }, { authorized }, { 10 }, queue)
        engine.request(FixtureOperation.PREPARE, challenge(), binding("prepare"), 100, 42) { terminals += it }
        authorized = false; queue.next()
        assertTrue(handler.effects.isEmpty()); assertEquals(listOf(FixtureTerminal.FAILED), terminals)
        rejected { engine.request(FixtureOperation.CANCEL, challenge(), binding("prepare"), 100, 42) {} }
    }
}
