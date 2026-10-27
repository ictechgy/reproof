package io.reproof.sdk

import io.reproof.fixture.FixtureChallenge
import io.reproof.fixture.FixtureOperation
import io.reproof.fixture.FixtureOperationBinding
import io.reproof.fixture.FixtureTerminal
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/** Serialized app-owned effects. Host absence/deadlines never manufacture a quiescent completion. */
internal class FixtureExecution(private val handler: ReproofFixtureHandler,
    private val gateHeld: () -> Boolean, private val authenticate: (Int) -> Boolean,
    private val clock: () -> Long, private val worker: Executor = Executors.newSingleThreadExecutor()) {
    private val lock = Any()
    private var attempt: ReproofFixtureAttempt? = null
    private var generation = 0L
    private val requests = linkedMapOf<FixtureOperation, FixtureOperationBinding>()
    private val results = linkedMapOf<FixtureOperation, FixtureTerminal>()
    private var pending: FixtureOperation? = null
    private var cancellationRequested = false

    fun request(operation: FixtureOperation, challenge: FixtureChallenge, binding: FixtureOperationBinding,
        deadlineMs: Long, admittedUid: Int, terminal: (FixtureTerminal) -> Unit) {
        require(challenge.valid() && binding.valid() && authenticate(admittedUid) && gateHeld() &&
            deadlineMs > clock() && deadlineMs - clock() <= 60_000)
        require(operation == FixtureOperation.CANCEL || binding.operation == operation.name.lowercase())
        synchronized(lock) {
            val existing = requests[operation]
            if (existing != null) {
                require(existing == binding && attempt?.challenge == challenge)
                results[operation]?.let { state -> worker.execute { terminal(state) } }
                return
            }
            if (operation == FixtureOperation.PREPARE) {
                require(attempt == null && binding.generation > generation)
                generation = binding.generation
                attempt = ReproofFixtureAttempt(challenge, deadlineMs, clock, gateHeld)
            } else require(attempt?.challenge == challenge && binding.generation == generation &&
                requests[FixtureOperation.PREPARE]?.allocationId == binding.allocationId)
            require(operation == FixtureOperation.CANCEL || requests.values.none {
                it.operationId == binding.operationId || it.idempotencyKey == binding.idempotencyKey })
            when (operation) {
                FixtureOperation.PREPARE -> require(pending == null)
                FixtureOperation.CHECK -> require(pending == null && !cancellationRequested && results[FixtureOperation.PREPARE] == FixtureTerminal.READY)
                FixtureOperation.CANCEL -> { cancellationRequested = true; attempt!!.cancel() }
                FixtureOperation.CLEANUP -> require(pending == null && results[FixtureOperation.CANCEL] == FixtureTerminal.CANCELLED)
            }
            if (operation in setOf(FixtureOperation.CHECK, FixtureOperation.CLEANUP))
                attempt = ReproofFixtureAttempt(challenge, deadlineMs, clock, gateHeld)
            val selected = attempt!!
            requests[operation] = binding
            if (operation != FixtureOperation.CANCEL) pending = operation
            worker.execute {
                val state = try {
                    if (operation != FixtureOperation.CANCEL) {
                        check(authenticate(admittedUid)); selected.ensureActive()
                    }
                    when (operation) {
                        FixtureOperation.PREPARE -> if (handler.prepare(selected) && !selected.cancelled) FixtureTerminal.READY
                            else if (selected.cancelled) FixtureTerminal.CANCELLED else FixtureTerminal.FAILED
                        FixtureOperation.CHECK -> if (handler.check(selected) && !selected.cancelled) FixtureTerminal.READY else FixtureTerminal.FAILED
                        FixtureOperation.CANCEL -> FixtureTerminal.CANCELLED
                        FixtureOperation.CLEANUP -> if (handler.cleanup(selected) && !selected.cancelled) FixtureTerminal.CLEANED else FixtureTerminal.FAILED
                    }
                } catch (_: Throwable) { FixtureTerminal.FAILED }
                // The handler has returned on this serial worker. A hung handler never reaches this point.
                synchronized(lock) { results[operation] = state; if (pending == operation) pending = null }
                terminal(state)
            }
        }
    }
}
