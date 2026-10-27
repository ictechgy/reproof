package io.reproof.sdk

import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.os.Binder
import android.os.SystemClock
import io.reproof.fixture.*
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** App-owned bootstrap interlock, immutable for this dedicated QA process.
 * The app must consult it before ordinary startup. Only process termination ends this gate.
 */
class ReproofFixtureBootstrapGate private constructor(private val owner: Context) {
    val normalStartupAllowed: Boolean get() = false
    internal fun heldFor(context: Context) = owner === context.applicationContext
    companion object {
        fun holdForQa(context: Context): ReproofFixtureBootstrapGate {
            val app = context.applicationContext
            require(FixtureIdentity.verifiedUid(app, app.packageName) == android.os.Process.myUid())
            return ReproofFixtureBootstrapGate(app)
        }
    }
}

/** Handlers must stop cooperatively. Returning means all work owned by that handler is quiescent. */
interface ReproofFixtureHandler {
    fun prepare(attempt: ReproofFixtureAttempt): Boolean
    fun check(attempt: ReproofFixtureAttempt): Boolean
    fun cleanup(attempt: ReproofFixtureAttempt): Boolean
}

class ReproofFixtureAttempt internal constructor(val challenge: FixtureChallenge, private val deadlineMs: Long,
    private val clock: () -> Long = { SystemClock.elapsedRealtime() },
    private val admission: () -> Boolean = { true }) {
    private val cancellation = AtomicBoolean(false)
    val cancelled: Boolean get() = cancellation.get() || clock() >= deadlineMs || !admission()
    fun ensureActive() { check(!cancelled) { "Fixture preparation cancelled" } }
    internal fun cancel() { cancellation.set(true) }
}

/** Explicit Debug Application.onCreate registration; this library exports no manifest component. */
object ReproofFixtures {
    @Volatile private var registration: Registration? = null
    @Synchronized
    fun install(context: Context, bootstrapGate: ReproofFixtureBootstrapGate, helperSignerDigest: String, fixtureId: String, fixtureVersion: Int,
        policyDigest: String, handler: ReproofFixtureHandler) {
        require(registration == null && FixtureIdentity.permittedSignerDigest(helperSignerDigest) &&
            fixtureId.matches(FixtureChallenge.CONTRACT_ID) && fixtureVersion in 1..1000 && policyDigest.matches(FixtureChallenge.DIGEST))
        val app = context.applicationContext
        require(FixtureIdentity.verifiedUid(app, app.packageName) == android.os.Process.myUid() && bootstrapGate.heldFor(app))
        registration = Registration(app, bootstrapGate, helperSignerDigest, fixtureId, fixtureVersion, policyDigest, handler)
    }
    internal fun endpoint(): IBinder? = registration?.let { if (it.gate.heldFor(it.context)) it.endpoint else null }

    private class Registration(val context: Context, val gate: ReproofFixtureBootstrapGate, val signer: String, val fixtureId: String,
        val version: Int, val policy: String, val handler: ReproofFixtureHandler) {
        private fun authenticated(uid: Int): Boolean = gate.heldFor(context) &&
            FixtureIdentity.verifiedUid(context, FixtureIdentity.HELPER_PACKAGE, signer) == uid &&
                FixtureIdentity.verifiedUid(context, context.packageName) == android.os.Process.myUid()
        private val execution = FixtureExecution(handler, { gate.heldFor(context) }, ::authenticated,
            { SystemClock.elapsedRealtime() })
        val endpoint = object : FixtureEndpoint() {
            override fun authenticateCaller(uid: Int) = authenticated(uid)
            override fun request(operation: FixtureOperation, challenge: FixtureChallenge,
                binding: FixtureOperationBinding, deadlineMs: Long, callback: IBinder) {
                // Capture authenticated kernel identity while still on this incoming Binder thread.
                val admittedUid = Binder.getCallingUid()
                require(challenge.fixtureId == fixtureId && challenge.fixtureVersion == version && challenge.policyDigest == policy)
                execution.request(operation, challenge, binding, deadlineMs, admittedUid) { state ->
                    deliver(callback, operation, challenge, binding, state)
                }
            }
        }
        private fun deliver(callback: IBinder, operation: FixtureOperation, challenge: FixtureChallenge,
            binding: FixtureOperationBinding, state: FixtureTerminal) {
            try { FixtureCallback.send(callback, operation, challenge, binding, state) } catch (_: Throwable) {
                // A lost callback cannot turn an uncertain host operation into success.
            }
        }
    }
}

/** Register only in the app's src/debug/AndroidManifest.xml, in the application's default process. */
class ReproofFixtureService : Service() {
    override fun onBind(intent: Intent?): IBinder? = ReproofFixtures.endpoint()
}
