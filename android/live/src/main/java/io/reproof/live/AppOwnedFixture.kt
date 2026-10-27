package io.reproof.live

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.SystemClock
import android.util.Base64
import io.reproof.fixture.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

internal fun fixtureCanonical(value: Any?): String = when (value) {
    null, JSONObject.NULL -> "null"
    is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(",", "{", "}") {
        fixtureCanonical(it) + ":" + fixtureCanonical(value.get(it)) }
    is String -> { require(value.length <= 256 && value.all { it.code in 0x20..0x7e && it != '"' && it != '\\' }); "\"$value\"" }
    is Int, is Long, is Boolean -> value.toString()
    else -> throw IllegalArgumentException("invalid_fixture")
}
internal fun fixtureExact(value: JSONObject, keys: Set<String>) = value.keys().asSequence().toSet() == keys
internal fun fixtureDigest(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }

internal data class AppOwnedFixture(val digest: String, val profileDigest: String, val artifactDigest: String,
    val fixtureId: String, val fixtureVersion: Int, val policyDigest: String, val helperSignerDigest: String,
    val launchConfigurationDigest: String?) {
    companion object {
        fun parse(envelope: JSONObject, selectedProfile: String?, selectedLaunch: String?): AppOwnedFixture? = try {
            require(fixtureExact(envelope, setOf("configurationBase64", "configurationDigest")))
            val encoded = envelope.getString("configurationBase64"); require(encoded.length <= 8192)
            val raw = Base64.decode(encoded, Base64.NO_WRAP)
            require(raw.size <= 4096 && Base64.encodeToString(raw, Base64.NO_WRAP) == encoded)
            val digest = envelope.getString("configurationDigest"); require(fixtureDigest(raw) == digest)
            val value = JSONObject(String(raw, Charsets.US_ASCII)); require(fixtureCanonical(value).toByteArray(Charsets.US_ASCII).contentEquals(raw))
            require(fixtureExact(value, setOf("schemaVersion", "kind", "referenceId", "profileDigest", "artifactDigest", "fixtureId", "fixtureVersion", "policyDigest", "helperSignerDigest", "launchConfigurationDigest")))
            require(value.opt("schemaVersion") is Int && value.getInt("schemaVersion") == 1 && value.getString("kind") == "reproof-approved-android-fixture")
            require(value.getString("referenceId").matches(FixtureChallenge.CONTRACT_ID))
            val profile = value.getString("profileDigest"); val artifact = value.getString("artifactDigest")
            val fixture = value.getString("fixtureId"); val version = value.opt("fixtureVersion") as? Int ?: error("version")
            val policy = value.getString("policyDigest"); val signer = value.getString("helperSignerDigest")
            val launch = if (value.isNull("launchConfigurationDigest")) null else value.getString("launchConfigurationDigest")
            require(listOf(profile, artifact, policy, signer).all { it.matches(FixtureChallenge.DIGEST) } &&
                FixtureIdentity.permittedSignerDigest(signer) && profile == selectedProfile && launch == selectedLaunch && fixture.matches(FixtureChallenge.CONTRACT_ID) && version in 1..1000)
            AppOwnedFixture(digest, profile, artifact, fixture, version, policy, signer, launch)
        } catch (_: Throwable) { null }
    }
    fun matches(challenge: FixtureChallenge) = challenge.configurationDigest == digest &&
        challenge.profileDigest == profileDigest && challenge.artifactDigest == artifactDigest &&
        challenge.fixtureId == fixtureId && challenge.fixtureVersion == fixtureVersion &&
        challenge.policyDigest == policyDigest && challenge.launchConfigurationDigest == launchConfigurationDigest
}

internal fun challengeJSON(c: FixtureChallenge) = JSONObject().apply {
    put("schemaVersion", 1); put("attemptId", c.attemptId); put("nonce", c.nonce); put("sessionId", c.sessionId)
    put("deviceScopeDigest", c.deviceScopeDigest); put("projectDigest", c.projectDigest); put("buildId", c.buildId)
    put("applicationId", c.applicationId); put("artifactDigest", c.artifactDigest); put("profileDigest", c.profileDigest)
    put("fixtureId", c.fixtureId); put("fixtureVersion", c.fixtureVersion); put("policyDigest", c.policyDigest)
    put("configurationDigest", c.configurationDigest); put("launchConfigurationDigest", c.launchConfigurationDigest ?: JSONObject.NULL)
    put("hostIncarnation", c.hostIncarnation); put("helperIncarnation", c.helperIncarnation)
    put("providerIncarnation", c.providerIncarnation); put("ownershipGeneration", c.ownershipGeneration)
}
internal fun parseChallenge(value: JSONObject): FixtureChallenge {
    require(value.opt("schemaVersion") is Int && value.getInt("schemaVersion") == 1)
    val c = FixtureChallenge(value.getString("attemptId"), value.getString("nonce"), value.getString("sessionId"),
        value.getString("deviceScopeDigest"), value.getString("projectDigest"), value.getString("buildId"),
        value.getString("applicationId"), value.getString("artifactDigest"), value.getString("profileDigest"),
        value.getString("fixtureId"), value.opt("fixtureVersion") as? Int ?: error("version"),
        value.getString("policyDigest"), value.getString("configurationDigest"),
        if (value.isNull("launchConfigurationDigest")) null else value.getString("launchConfigurationDigest"),
        value.getString("hostIncarnation"), value.getString("helperIncarnation"), value.getString("providerIncarnation"),
        when (val n = value.opt("ownershipGeneration")) { is Int -> n.toLong(); is Long -> n; else -> error("generation") })
    require(c.valid() && fixtureCanonical(value) == fixtureCanonical(challengeJSON(c)))
    return c
}
internal fun bindingJSON(b: FixtureOperationBinding) = JSONObject().put("operationId", b.operationId)
    .put("allocationId", b.allocationId).put("generation", b.generation).put("idempotencyKey", b.idempotencyKey)
    .put("payloadDigest", b.payloadDigest).put("recipeId", b.recipeId).put("operation", b.operation)
internal fun parseBinding(value: JSONObject): FixtureOperationBinding {
    val b = FixtureOperationBinding(value.getString("operationId"), value.getString("allocationId"),
        when (val n = value.opt("generation")) { is Int -> n.toLong(); is Long -> n; else -> error("generation") },
        value.getString("idempotencyKey"), value.getString("payloadDigest"), value.getString("recipeId"), value.getString("operation"))
    require(b.valid() && fixtureCanonical(value) == fixtureCanonical(bindingJSON(b)))
    return b
}

/** One process-owned attempt; death and unbind never constitute app-side quiescence. */
internal class AppOwnedFixtureClient(private val context: Context, private val selection: AppOwnedFixture,
    private val targetPackage: String) {
    private val lock = Any()
    private val transport = Executors.newSingleThreadExecutor()
    private var endpoint: IBinder? = null
    private var connection: ServiceConnection? = null
    private var targetUid: Int? = null
    private var targetInstall: Triple<Int, Long, String>? = null
    private var challenge: FixtureChallenge? = null
    private val requests = linkedMapOf<FixtureOperation, FixtureOperationBinding>()
    private val receipts = linkedMapOf<FixtureOperation, JSONObject>()
    private val inflight = mutableSetOf<FixtureOperation>()
    private var cancelled = false
    private var cancelTerminal = false
    private var invalidated = false
    private var activated = false
    private var quiescent = false
    private var bindingLatch: CountDownLatch? = null

    fun identityValid(): Boolean { return try {
        val uid = FixtureIdentity.verifiedUid(context, targetPackage) ?: return false
        if (FixtureIdentity.verifiedUid(context, FixtureIdentity.HELPER_PACKAGE, selection.helperSignerDigest) != android.os.Process.myUid()) return false
        val component = ComponentName(targetPackage, FixtureEndpoint.SERVICE_CLASS)
        val service = context.packageManager.getServiceInfo(component, 0)
        if (service.packageName != targetPackage || service.name != FixtureEndpoint.SERVICE_CLASS ||
            service.processName != targetPackage || !service.enabled || !service.exported ||
            (service.flags and ServiceInfo.FLAG_ISOLATED_PROCESS) != 0) return false
        val before = context.packageManager.getPackageInfo(targetPackage, 0)
        val app = before.applicationInfo ?: return false
        if (app.uid != uid || service.applicationInfo.uid != uid) return false
        val snapshot = Triple(uid, before.lastUpdateTime, app.sourceDir)
        val apk = File(app.sourceDir)
        if (!apk.isFile || apk.length() !in 1..(1024L * 1024 * 1024)) return false
        val hash = MessageDigest.getInstance("SHA-256")
        apk.inputStream().use { input -> val buffer = ByteArray(65536); while (true) {
            val size = input.read(buffer); if (size < 0) break; hash.update(buffer, 0, size)
        } }
        if (hash.digest().joinToString("") { "%02x".format(it.toInt() and 255) } != selection.artifactDigest) return false
        val after = context.packageManager.getPackageInfo(targetPackage, 0)
        val afterApp = after.applicationInfo ?: return false
        if (Triple(afterApp.uid, after.lastUpdateTime, afterApp.sourceDir) != snapshot) return false
        synchronized(lock) {
            if ((targetUid != null && targetUid != uid) || (targetInstall != null && targetInstall != snapshot)) return false
            targetUid = uid; targetInstall = snapshot
        }
        true
    } catch (_: Throwable) { false }
    }

    private fun bound(deadline: Long, authorized: () -> Boolean): IBinder? {
        synchronized(lock) { if (invalidated) return null; endpoint?.let { return it } }
        if (!authorized() || !identityValid()) return null
        val latch = CountDownLatch(1)
        val selected = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName, service: IBinder) {
                synchronized(lock) {
                    if (invalidated || name != ComponentName(targetPackage, FixtureEndpoint.SERVICE_CLASS)) { latch.countDown(); return }
                    endpoint = service
                    service.linkToDeath({ synchronized(lock) { invalidated = true; endpoint = null; quiescent = false } }, 0)
                    latch.countDown()
                }
            }
            override fun onServiceDisconnected(name: ComponentName) { synchronized(lock) { invalidated = true; endpoint = null; quiescent = false; latch.countDown() } }
            override fun onBindingDied(name: ComponentName) = onServiceDisconnected(name)
            override fun onNullBinding(name: ComponentName) = onServiceDisconnected(name)
        }
        synchronized(lock) {
            if (connection == null) {
                connection = selected; bindingLatch = latch
                if (!context.bindService(Intent().setComponent(ComponentName(targetPackage, FixtureEndpoint.SERVICE_CLASS)), selected, Context.BIND_AUTO_CREATE)) {
                    invalidated = true; latch.countDown()
                }
            }
        }
        val waiting = synchronized(lock) { bindingLatch } ?: return null
        while (SystemClock.elapsedRealtime() < deadline && authorized()) {
            if (waiting.await(50, TimeUnit.MILLISECONDS)) break
        }
        return synchronized(lock) { if (!invalidated && authorized()) endpoint else null }
    }

    fun accept(phase: FixtureOperation, payload: JSONObject, deadline: Long, authorized: () -> Boolean): Boolean { return try {
        val control = phase == FixtureOperation.CANCEL
        require(fixtureExact(payload, if (control) setOf("challenge", "timeoutMs") else setOf("challenge", "operation", "timeoutMs")))
        val selected = parseChallenge(payload.getJSONObject("challenge"))
        require(selection.matches(selected) && authorized() && deadline > SystemClock.elapsedRealtime())
        val binding = if (control) synchronized(lock) { requests[FixtureOperation.PREPARE] ?: error("unprepared") }
            else parseBinding(payload.getJSONObject("operation"))
        if (!control) require(binding.operation == phase.name.lowercase())
        synchronized(lock) {
            require(!invalidated && (!activated || phase in setOf(FixtureOperation.CANCEL, FixtureOperation.CLEANUP)))
            if (challenge == null) { require(phase == FixtureOperation.PREPARE); challenge = selected }
            require(challenge == selected)
            val old = requests[phase]
            if (old != null) { require(old == binding); return true }
            if (phase != FixtureOperation.PREPARE) require(requests[FixtureOperation.PREPARE]?.let {
                it.allocationId == binding.allocationId && it.generation == binding.generation } == true)
            when (phase) {
                FixtureOperation.PREPARE -> require(requests.isEmpty())
                FixtureOperation.CHECK -> require(inflight.isEmpty() && receipts[FixtureOperation.PREPARE]?.optString("status") == "complete" && !cancelled)
                FixtureOperation.CANCEL -> cancelled = true
                FixtureOperation.CLEANUP -> require(inflight.isEmpty() && quiescent)
            }
            requests[phase] = binding; inflight.add(phase); quiescent = false
        }
        val callback = object : FixtureCallback() {
            override fun authenticateCaller(uid: Int): Boolean = uid == synchronized(lock) { targetUid } && identityValid()
            override fun terminal(operation: FixtureOperation, received: FixtureChallenge,
                receivedBinding: FixtureOperationBinding, state: FixtureTerminal) {
                // Kernel UID authenticated above on the Binder ingress thread, before any enqueue.
                synchronized(lock) {
                    if (invalidated || operation != phase || received != selected || receivedBinding != binding ||
                        challenge != selected || operation !in inflight) return
                    inflight.remove(operation)
                    if (operation == FixtureOperation.CANCEL) cancelTerminal = state == FixtureTerminal.CANCELLED
                    else {
                        val success = authorized() && SystemClock.elapsedRealtime() < deadline && !cancelled &&
                            ((operation in setOf(FixtureOperation.PREPARE, FixtureOperation.CHECK) && state == FixtureTerminal.READY) ||
                                (operation == FixtureOperation.CLEANUP && state == FixtureTerminal.CLEANED))
                        // Cleanup after cancellation is permitted under a fresh bounded cleanup authority.
                        val cleaned = operation == FixtureOperation.CLEANUP && state == FixtureTerminal.CLEANED && authorized() && SystemClock.elapsedRealtime() < deadline
                        receipts[operation] = JSONObject().put("challenge", challengeJSON(received)).put("operation", bindingJSON(binding))
                            .put("phase", operation.name.lowercase()).put("status", if (success || cleaned) "complete" else "failed").put("quiescent", true)
                    }
                    quiescent = inflight.isEmpty()
                }
            }
        }
        transport.execute {
            try {
                val remote = bound(deadline, authorized) ?: return@execute
                if (!authorized() || !identityValid() || SystemClock.elapsedRealtime() >= deadline) return@execute
                FixtureEndpoint.send(remote, phase, selected, binding, deadline, callback)
            } catch (_: Throwable) {
                // No synthetic failed/quiescent receipt: dispatch may have reached the application.
            }
        }
        true
    } catch (_: Throwable) { false }
    }

    fun activate(payload: JSONObject, authorized: () -> Boolean): Boolean = try {
        require(fixtureExact(payload, setOf("challenge", "timeoutMs")))
        val selected = parseChallenge(payload.getJSONObject("challenge"))
        require(authorized() && identityValid())
        synchronized(lock) {
            require(!invalidated && !activated && !cancelled && challenge == selected && quiescent && inflight.isEmpty() &&
                receipts[FixtureOperation.CHECK]?.optString("status") == "complete")
            activated = true
        }
        true
    } catch (_: Throwable) { false }

    fun status(): JSONObject {
        val identity = identityValid()
        return synchronized(lock) {
            JSONObject().put("appOwnedFixtureVersion", 1).put("appOwnedFixtureConfigurationDigest", selection.digest)
                .put("fixtureReceipts", JSONArray(if (identity && !invalidated) receipts.values.toList() else emptyList<JSONObject>()))
                .put("fixturePending", inflight.isNotEmpty()).put("fixtureQuiescent", identity && !invalidated && quiescent)
                .put("fixtureCancelled", identity && !invalidated && cancelTerminal)
        }
    }
    fun close() {
        synchronized(lock) { invalidated = true; quiescent = false; connection?.let { try { context.unbindService(it) } catch (_: Throwable) {} }; connection = null }
        // Disconnect does not imply app-owned work stopped. The host retains its quarantine.
        transport.shutdownNow()
    }
}
