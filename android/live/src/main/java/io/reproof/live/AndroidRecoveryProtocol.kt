package io.reproof.live

import java.security.MessageDigest

/** Private qualification protocol. No ordinary recovery capability or persisted authority. */
internal fun recoveryCanonical(value: Any?): String = when (value) {
    null -> "null"
    is Map<*, *> -> value.keys.map { it as String }.sorted().joinToString(",", "{", "}") {
        recoveryCanonical(it) + ":" + recoveryCanonical(value[it]) }
    is String -> { require(value.length <= 256 && value.all { it.code in 0x20..0x7e && it != '"' && it != '\\' }); "\"$value\"" }
    is Int, is Long, is Boolean -> value.toString()
    else -> error("invalid_recovery_value")
}
internal fun recoveryDigest(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes)
    .joinToString("") { "%02x".format(it.toInt() and 255) }
private val RECOVERY_ID = Regex("[a-z][a-z0-9_-]{0,63}")
private val RECOVERY_DIGEST = Regex("[0-9a-f]{64}")
private fun recoveryInteger(value: Any?): Long = when (value) { is Int -> value.toLong(); is Long -> value; else -> error("invalid_recovery_integer") }

internal data class AndroidRecoverySelection(val digest: String, val data: Map<String, Any?>) {
    val phase get() = data.getValue("phase") as String
    val priorGeneration get() = recoveryInteger(data.getValue("priorOwnershipGeneration"))
    companion object {
        fun parse(raw: ByteArray, value: Map<String, Any?>, digest: String): AndroidRecoverySelection {
            require(raw.size in 1..4096 && digest.matches(RECOVERY_DIGEST) && recoveryDigest(raw) == digest &&
                recoveryCanonical(value).toByteArray(Charsets.US_ASCII).contentEquals(raw))
            require(value.keys == setOf("schemaVersion", "kind", "phase", "attemptId", "sessionId", "deviceScopeDigest",
                "applicationId", "artifactDigest", "profileDigest", "helperArtifactDigest", "priorOwnershipGeneration"))
            require(value["schemaVersion"] == 1 && value["kind"] == "reproof-android-recovery-qualification" && value["phase"] in setOf("session", "probe"))
            for (key in listOf("attemptId", "sessionId", "applicationId")) require((value[key] as? String)?.matches(RECOVERY_ID) == true)
            for (key in listOf("deviceScopeDigest", "artifactDigest", "profileDigest", "helperArtifactDigest")) require((value[key] as? String)?.matches(RECOVERY_DIGEST) == true)
            require(recoveryInteger(value["priorOwnershipGeneration"]) in 1 until Int.MAX_VALUE.toLong())
            return AndroidRecoverySelection(digest, value.toMap())
        }
    }
}

internal data class AndroidRecoveryGrant(val data: Map<String, Any?>) {
    val purpose get() = data.getValue("purpose") as String
    val operationId get() = data.getValue("operationId") as String
    val sequence get() = recoveryInteger(data.getValue("sequence"))
    val deadline get() = recoveryInteger(data.getValue("nativeDeadlineMs"))
    companion object {
        fun parse(value: Map<String, Any?>, selection: AndroidRecoverySelection,
            host: String, helper: String, provider: String, native: String, now: Long): AndroidRecoveryGrant {
            require(value.keys == setOf("schemaVersion", "purpose", "operationId", "sequence", "configurationDigest", "attemptId",
                "sessionId", "priorOwnershipGeneration", "recoveryOwnershipGeneration", "hostIncarnation", "helperIncarnation",
                "providerIncarnation", "nativeIncarnation", "nativeClockId", "nativeDeadlineMs", "payloadDigest"))
            require(value["schemaVersion"] == 1 && value["purpose"] in setOf("old-retire", "old-exit", "fresh-probe", "probe-retire", "probe-exit"))
            require((value["operationId"] as? String)?.matches(RECOVERY_ID) == true && recoveryInteger(value["sequence"]) in 1..Int.MAX_VALUE.toLong())
            require(value["configurationDigest"] == selection.digest && value["attemptId"] == selection.data["attemptId"] && value["sessionId"] == selection.data["sessionId"])
            require(recoveryInteger(value["priorOwnershipGeneration"]) == selection.priorGeneration &&
                recoveryInteger(value["recoveryOwnershipGeneration"]) == selection.priorGeneration + 1)
            require(value["hostIncarnation"] == host && value["helperIncarnation"] == helper && value["providerIncarnation"] == provider &&
                value["nativeIncarnation"] == native && value["nativeClockId"] == "android-elapsed-realtime")
            val deadline = recoveryInteger(value["nativeDeadlineMs"])
            require(deadline > now && deadline - now <= 60_000)
            require(value["payloadDigest"] == recoveryDigest(recoveryCanonical(mapOf("configurationDigest" to selection.digest,
                "purpose" to value["purpose"])).toByteArray(Charsets.US_ASCII)))
            return AndroidRecoveryGrant(value.toMap())
        }
    }
}

/** Real admission/terminal state used by the helper; deliberately Android-free for executable race tests. */
internal class AndroidRecoveryProtocol(val selection: AndroidRecoverySelection, private val clock: () -> Long) {
    private var state = if (selection.phase == "session") "running" else "probe-ready"
    private var watermark = 0L
    private val requests = linkedMapOf<String, AndroidRecoveryGrant>()
    private val terminals = linkedMapOf<String, Map<String, Any?>>()
    private var active: String? = null
    @Synchronized fun normalAllowed() = selection.phase == "session" && state == "running"
    @Synchronized fun probing() = state == "probing"
    @Synchronized fun retiring() = state in setOf("retiring", "retired", "exiting", "failed")
    @Synchronized fun admit(grant: AndroidRecoveryGrant): Boolean {
        requests[grant.operationId]?.let { require(it == grant); return false }
        require(clock() < grant.deadline && grant.sequence > watermark && active == null && requests.size < 3)
        val next = when (grant.purpose) {
            "old-retire" -> { require(selection.phase == "session" && state == "running"); "retiring" }
            "fresh-probe" -> { require(selection.phase == "probe" && state == "probe-ready"); "probing" }
            "probe-retire" -> { require(selection.phase == "probe" && state == "probed"); "retiring" }
            "old-exit" -> { require(selection.phase == "session" && state == "retired"); "exiting" }
            "probe-exit" -> { require(selection.phase == "probe" && state == "retired"); "exiting" }
            else -> error("invalid_recovery_purpose")
        }
        watermark = grant.sequence; requests[grant.operationId] = grant; active = grant.operationId; state = next
        return true
    }
    @Synchronized fun complete(grant: AndroidRecoveryGrant, process: Map<String, Any?>, evidence: Map<String, Any?>): Boolean {
        if (requests[grant.operationId] != grant || active != grant.operationId || terminals.containsKey(grant.operationId)) return false
        if (clock() >= grant.deadline || state == "failed") { fail(grant); return false }
        require(process.keys == setOf("pid", "startTicks") && recoveryInteger(process["pid"]) in 1..Int.MAX_VALUE.toLong() &&
            (process["startTicks"] as? String)?.matches(Regex("[1-9][0-9]{0,19}")) == true)
        val kind = when (grant.purpose) {
            "old-retire", "probe-retire" -> {
                val flags = setOf("normalAdmissionClosed", "commandQueueDrained", "inflightCompleted", "pointersCancelled", "captureWorkersStopped", "inspectionMediaDrained", "publicationClosed")
                require(evidence.keys == flags + setOf("admissionWatermark", "detachedCommandId") && flags.all { evidence[it] == true })
                require(recoveryInteger(evidence["admissionWatermark"]) >= 0)
                require(evidence["detachedCommandId"] == null || evidence["detachedCommandId"] is String)
                state = "retired"; "retirement"
            }
            "fresh-probe" -> {
                require(evidence.keys == setOf("nativeFrameId", "frameSha256", "frameBytes", "width", "height", "geometryVersion", "targetForeground", "captureStartMs", "captureEndMs"))
                require(evidence["targetForeground"] == true && (evidence["frameSha256"] as? String)?.matches(RECOVERY_DIGEST) == true)
                require(recoveryInteger(evidence["nativeFrameId"]) > 0 && recoveryInteger(evidence["geometryVersion"]) > 0 &&
                    recoveryInteger(evidence["frameBytes"]) in 9..(3L * 1024 * 1024 + 4104))
                require(listOf("width", "height").all { recoveryInteger(evidence[it]) in 2..8192 && recoveryInteger(evidence[it]) % 2 == 0L })
                require(recoveryInteger(evidence["captureStartMs"]) >= 0 && recoveryInteger(evidence["captureEndMs"]) >= recoveryInteger(evidence["captureStartMs"]) &&
                    recoveryInteger(evidence["captureEndMs"]) <= clock())
                state = "probed"; "probe-frame"
            }
            else -> { require(evidence.isEmpty()); "exit-accepted" }
        }
        val completed = clock()
        if (completed >= grant.deadline) { fail(grant); return false }
        terminals[grant.operationId] = mapOf("pending" to false, "receipt" to mapOf("schemaVersion" to 1,
            "status" to "complete", "grant" to grant.data, "nativeProcessIdentity" to process.toMap(),
            "completedNativeTimeMs" to completed, "kind" to kind, "evidence" to evidence.toMap()))
        active = null
        return true
    }
    @Synchronized fun fail(grant: AndroidRecoveryGrant) {
        if (requests[grant.operationId] == grant && !terminals.containsKey(grant.operationId)) {
            state = "failed"; active = null
            terminals[grant.operationId] = mapOf("pending" to false, "status" to "unknown", "operationId" to grant.operationId)
        }
    }
    @Synchronized fun status(id: String): Map<String, Any?>? {
        val grant = requests[id] ?: return null
        if (active == id && clock() >= grant.deadline) fail(grant)
        return terminals[id] ?: mapOf("pending" to true, "operationId" to id)
    }
}

/** Exit notification is gated on successful delivery of the terminal exit receipt, never a pending poll. */
internal class AndroidRecoveryExitDelivery {
    private val delivered = mutableSetOf<String>()
    private val callbacks = mutableMapOf<String, () -> Unit>()
    fun afterSuccessfulWrite(id: String, response: Map<String, Any?>) {
        val receipt = response["receipt"] as? Map<*, *> ?: return
        val grant = receipt["grant"] as? Map<*, *> ?: return
        if (response["pending"] != false || receipt["kind"] != "exit-accepted" || receipt["status"] != "complete" || grant["operationId"] != id) return
        val callback = synchronized(this) { delivered.add(id); callbacks.remove(id) }
        callback?.invoke()
    }
    fun onTerminalDelivery(id: String, callback: () -> Unit) {
        val ready = synchronized(this) { if (id in delivered) true else { callbacks[id] = callback; false } }
        if (ready) callback()
    }
}

/** Termination is observed, never inferred from an interrupt request or deadline. */
internal fun awaitRecoveryBarrier(deadline: Long, clock: () -> Long, authorized: () -> Boolean,
    ready: () -> Boolean, pause: () -> Unit): Boolean {
    while (clock() < deadline && authorized()) {
        if (ready()) return clock() < deadline && authorized()
        pause()
    }
    return false
}

/** Fixed /proc/self/stat parser; identity is unavailable rather than guessed on malformed input. */
internal fun parseRecoveryProcessStat(bytes: ByteArray, expectedPid: Int): Map<String, Any?> {
    require(bytes.size in 1..4096 && bytes.all { it.toInt() in 0..127 })
    val value = String(bytes, Charsets.US_ASCII)
    val pidEnd = value.indexOf(' ')
    require(pidEnd > 0 && value.substring(0, pidEnd).matches(Regex("[1-9][0-9]{0,9}")))
    val pid = value.substring(0, pidEnd).toInt()
    val commandEnd = value.lastIndexOf(')')
    require(pid == expectedPid && expectedPid > 0 && value.getOrNull(pidEnd + 1) == '(' &&
        commandEnd > pidEnd + 1 && value.getOrNull(commandEnd + 1) == ' ')
    val fields = value.substring(commandEnd + 2).trim().split(Regex("\\s+"))
    require(fields.size >= 20)
    val start = fields[19]
    require(start.matches(Regex("[1-9][0-9]{0,19}")))
    return mapOf("pid" to pid, "startTicks" to start)
}
