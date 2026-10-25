package io.reproof.sdk

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** Same-directory publication. Each call owns its temporary; the last successful rename wins. */
internal fun atomicWriteFile(
    destination: File,
    bytes: ByteArray,
    write: (FileOutputStream, ByteArray) -> Unit = { output, content -> output.write(content) },
) {
    val temporary = File.createTempFile(".${destination.name}.", ".tmp", destination.absoluteFile.parentFile)
    var published = false
    try {
        FileOutputStream(temporary).use { output ->
            write(output, bytes)
            output.fd.sync()
        }
        if (!temporary.renameTo(destination)) {
            throw IllegalStateException("Could not publish ${destination.name}")
        }
        published = true
    } finally {
        // After rename the old path is no longer ours; never delete a path reused by another call.
        if (!published) {
            try {
                temporary.delete()
            } catch (_: SecurityException) {
                // Best-effort cleanup must not hide the original write or publication failure.
            }
        }
    }
}

/** Explicit trusted policy. Identifiers are semantic IDs, never user content.
 * Invalid policy construction throws a fixed programmer-error message.
 */
class ReproPrivacyConfig(
    val fixtureId: String = "default",
    val fixtureVersion: Int = 1,
    safeTextTargets: Set<String> = setOf("name", "count"),
    safeTextValues: Set<String> = setOf("", "QA", "Test"),
    numericTargets: Set<String> = setOf("count"),
    allowedStartScreens: Set<String> = setOf("main"),
    tapTargets: Set<String> = setOf("add", "list", "bottom", "next", "back"),
    scrollTargets: Map<String, Set<String>> = mapOf("list" to setOf("bottom")),
    val backTarget: String = "back",
    val reportTarget: String = "report",
    val maxDurationMs: Long = ReproRecorder.DEFAULT_MAX_DURATION_MS,
    val maxBytes: Long = ReproRecorder.DEFAULT_MAX_BYTES,
) {
    val safeTextTargets = identifiers(safeTextTargets)
    val safeTextValues: Set<String> = immutableSet(safeTextValues).also {
        requirePolicy(it.size <= 64 && it.all { value -> value.length <= 64 && value.none { c -> c.isISOControl() } })
    }
    val numericTargets = identifiers(numericTargets)
    val allowedStartScreens = identifiers(allowedStartScreens)
    val tapTargets = identifiers(tapTargets)
    val scrollTargets: Map<String, Set<String>> = java.util.Collections.unmodifiableMap(
        LinkedHashMap<String, Set<String>>().also { copy ->
            requirePolicy(scrollTargets.size <= 16)
            scrollTargets.forEach { (container, ends) ->
                requirePolicy(copy.size < 16 && identifier(container) && container !in copy)
                copy[container] = identifiers(ends)
            }
        },
    )

    init {
        requirePolicy(identifier(fixtureId) && fixtureVersion in 1..1000)
        requirePolicy(identifier(backTarget) && identifier(reportTarget))
        requirePolicy(maxDurationMs in 1..ReproRecorder.DEFAULT_MAX_DURATION_MS)
        requirePolicy(maxBytes in 8192..ReproRecorder.DEFAULT_MAX_BYTES)
        requirePolicy(this.allowedStartScreens.isNotEmpty())
    }

    fun allowsTap(target: String): Boolean = target.length <= 64 && target in tapTargets
    fun allowsReplace(target: String, value: String): Boolean =
        target.length <= 64 && value.length <= 64 && target in safeTextTargets &&
            target != "password" && value in safeTextValues
    fun allowsScroll(container: String, direction: String, target: String?): Boolean =
        container.length <= 64 && direction.length <= 8 && (target == null || target.length <= 64) &&
            container in scrollTargets && direction in setOf("forward", "backward") &&
            (target == null || target in scrollTargets.getValue(container))
    fun allowsBack(target: String): Boolean = target == backTarget

    fun admits(state: ReproStartState): Boolean =
        state.screen in allowedStartScreens && state.nodes.keys == safeTextTargets + numericTargets &&
            startStateStorageReserve(state) <= maxBytes && state.nodes.all { (target, value) ->
            (target in numericTargets && NUMERIC.matches(value)) ||
                (target in safeTextTargets && target !in numericTargets && allowsReplace(target, value))
        }

    companion object {
        private val ID = Regex("[a-z][a-z0-9_]{0,63}")
        private val NUMERIC = Regex("[0-9]{1,9}")
        internal fun identifier(value: String): Boolean = value.length <= 64 && ID.matches(value) &&
            listOf("password", "secret", "token", "email", "phone").none { value.contains(it) }
        internal fun requirePolicy(condition: Boolean) {
            require(condition) { "Invalid recorder privacy configuration" }
        }
        private fun identifiers(values: Set<String>): Set<String> {
            val copy = immutableSet(values)
            requirePolicy(copy.all { identifier(it) })
            return copy
        }
        private fun immutableSet(values: Set<String>): Set<String> {
            requirePolicy(values.size <= 64)
            val copy = LinkedHashSet<String>()
            var count = 0
            for (value in values) {
                requirePolicy(count++ < 64 && value.length <= 64)
                copy.add(value)
            }
            return java.util.Collections.unmodifiableSet(copy)
        }
    }
}

/** Flat, bounded start state. The recorder additionally applies its explicit policy. */
class ReproStartState(val screen: String, nodes: Map<String, String>) {
    val nodes: Map<String, String>
    init {
        ReproPrivacyConfig.requirePolicy(ReproPrivacyConfig.identifier(screen) && nodes.size <= 128)
        val copy = LinkedHashMap<String, String>()
        nodes.forEach { (key, value) ->
            ReproPrivacyConfig.requirePolicy(copy.size < 128 && ReproPrivacyConfig.identifier(key) &&
                value.length <= 64 && key !in copy)
            copy[key] = value
        }
        this.nodes = java.util.Collections.unmodifiableMap(copy)
    }
}

private class RecorderAdmission(val policy: ReproPrivacyConfig, val state: ReproStartState)

// Conservative UTF-8 / JSON escaping allowance, including metadata, capture copies and temporaries.
private fun startStateStorageReserve(state: ReproStartState): Long =
    8192L + (state.screen.length * 6L + state.nodes.entries.sumOf { (key, value) ->
        (key.length + value.length) * 6L + 16L
    }) * 8L

/** Pure admission boundary shared by the JSON adapter and JVM canary tests. */
internal fun admitStartState(
    configuration: ReproPrivacyConfig,
    screen: Any?,
    nodeCount: Int,
    entries: Sequence<Pair<String, Any?>>,
): ReproStartState? = try {
    ReproPrivacyConfig.requirePolicy(screen is String && nodeCount in 0..128)
    val copy = LinkedHashMap<String, String>()
    for ((key, rawValue) in entries) {
        ReproPrivacyConfig.requirePolicy(copy.size < nodeCount && ReproPrivacyConfig.identifier(key) && key !in copy)
        ReproPrivacyConfig.requirePolicy(rawValue is String)
        ReproPrivacyConfig.requirePolicy(ReproPrivacyConfig.identifier(key) && (rawValue as String).length <= 64)
        copy[key] = rawValue as String
    }
    ReproPrivacyConfig.requirePolicy(copy.size == nodeCount)
    val state = ReproStartState(screen as String, copy)
    if (configuration.admits(state)) state else null
} catch (_: IllegalArgumentException) {
    null
}

/** Adapter never stringifies raw input or coerces arbitrary node objects. */
private fun admitLegacy(policy: () -> ReproPrivacyConfig, raw: JSONObject): RecorderAdmission? = try {
    val trusted = policy()
    ReproPrivacyConfig.requirePolicy(raw.length() == 2 && raw.has("screen") && raw.has("nodes"))
    val nodes = raw.opt("nodes") as? JSONObject
    ReproPrivacyConfig.requirePolicy(nodes != null)
    val state = admitStartState(trusted, raw.opt("screen"), nodes!!.length(),
        nodes.keys().asSequence().map { key -> key to nodes.opt(key) })
    if (state != null) RecorderAdmission(trusted, state) else null
} catch (_: IllegalArgumentException) {
    null
}

/** Ordered export boundary. Runs on the caller's thread and propagates every failure. */
internal fun publishSessionExport(
    sessionDirectory: File,
    writeCapture: (File) -> Unit,
    finalizeMetadata: (File) -> Unit,
    callback: (File) -> Unit,
) {
    val capture = File(sessionDirectory, "capture.json")
    writeCapture(capture)
    finalizeMetadata(File(sessionDirectory, "session.json"))
    callback(capture)
}

/**
 * Small, opt-in recorder for an annotated app session.
 *
 * The recorder intentionally knows nothing about view coordinates. Callers annotate
 * semantic actions with resource entry names, which makes the capture portable
 * across device sizes. All writes are ordered on one background executor.
 */
class ReproRecorder private constructor(
    context: Context,
    private val admission: RecorderAdmission?,
) {
    constructor(context: Context, configuration: ReproPrivacyConfig, startState: ReproStartState) :
        this(context, if (configuration.admits(startState)) RecorderAdmission(configuration, startState) else null)

    /** Source-compatible legacy API. Invalid input disables capture before any side effects. */
    @JvmOverloads
    constructor(
        context: Context,
        fixtureId: String = "default",
        fixtureVersion: Int = 1,
        startState: JSONObject,
        safeTextTargets: Set<String> = setOf("name", "count"),
        safeTextValues: Set<String> = setOf("", "QA", "Test"),
        maxDurationMs: Long = DEFAULT_MAX_DURATION_MS,
        maxBytes: Long = DEFAULT_MAX_BYTES,
        tapTargets: Set<String> = setOf("add", "list", "bottom", "next", "back"),
        scrollTargets: Map<String, Set<String>> = mapOf("list" to setOf("bottom")),
        backTarget: String = "back",
        reportTarget: String = "report",
        numericTargets: Set<String> = setOf("count"),
        allowedStartScreens: Set<String> = setOf("main"),
    ) : this(context, admitLegacy({
        ReproPrivacyConfig(fixtureId, fixtureVersion, safeTextTargets, safeTextValues,
            numericTargets, allowedStartScreens, tapTargets, scrollTargets, backTarget,
            reportTarget, maxDurationMs, maxBytes)
    }, startState))

    /** False means rejected configuration; no recording or filesystem activity occurs. */
    val isEnabled: Boolean = admission != null
    private val policy get() = admission!!.policy
    private val fixtureId get() = policy.fixtureId
    private val fixtureVersion get() = policy.fixtureVersion
    private val maxDurationMs get() = policy.maxDurationMs
    private val maxBytes get() = policy.maxBytes
    private val appContext by lazy { context.applicationContext }
    private val lock = Any()
    private val executor: ExecutorService by lazy {
        Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "repro-recorder").apply { isDaemon = true }
        }
    }
    private val sessionId = if (isEnabled) UUID.randomUUID().toString() else ""
    private val startedAtMs = if (isEnabled) System.currentTimeMillis() else 0L
    private val startedElapsedMs = if (isEnabled) android.os.SystemClock.elapsedRealtime() else 0L
    private val sessionDirectory by lazy { File(appContext.filesDir, "repro/$sessionId") }
    private val eventsFile by lazy { File(sessionDirectory, "events.jsonl") }
    private val publishedCaptureFile by lazy { File(appContext.filesDir, "repro/capture.json") }
    private val currentSessionFile by lazy { File(appContext.filesDir, "repro/current-session") }
    private val metadataFile by lazy { File(sessionDirectory, "session.json") }
    private val events = ArrayList<RecordedEvent>()
    private val startStateJson by lazy {
        val nodes = JSONObject()
        val state = admission!!.state
        state.nodes.forEach { (key, value) -> nodes.put(key, value) }
        JSONObject().put("screen", state.screen).put("nodes", nodes)
    }
    private var nextSequence = 1
    // Reserve for JSONL, final capture copies, and atomic temporary files.
    private var bytesReserved = admission?.state?.let { startStateStorageReserve(it) } ?: 0L
    private var accepting = isEnabled
    private var frozen = false
    private var truncated = false
    private var lostEvents = false
    private var unsupported = false

    init {
        if (isEnabled) executor.execute {
            try {
                check(sessionDirectory.mkdirs() || sessionDirectory.isDirectory)
                publishedCaptureFile.parentFile?.mkdirs()
                writeMetadata(finalized = false, incomplete = true)
                // Publish the active recorder identity only after its initial
                // metadata exists. The host uses this atomically replaced
                // pointer to reject captures from a later app process.
                atomicWrite(currentSessionFile, sessionId.toByteArray(StandardCharsets.UTF_8))
            } catch (_: Throwable) {
                markWriteFailure()
            }
        }
    }

    /** Returns false when the event is rejected by privacy or recording limits. */
    fun recordTap(target: String): Boolean {
        if (!isEnabled) return false
        if (!policy.allowsTap(target)) { invalidateSession(); return false }
        return record(target, "tap", JSONObject())
    }

    /** Records only allowlisted text fields and values. */
    fun recordReplace(target: String, value: String): Boolean {
        if (!isEnabled) return false
        if (!policy.allowsReplace(target, value)) {
            invalidateSession()
            return false
        }
        return record(target, "replace", JSONObject().put("value", value))
    }

    fun recordScrollTo(container: String, direction: String, target: String? = null): Boolean {
        if (!isEnabled) return false
        if (!policy.allowsScroll(container, direction, target)) {
            invalidateSession()
            return false
        }
        // The target belongs in the event target; parameters stay exact and stable.
        val parameters = JSONObject()
            .put("container", container)
            .put("direction", direction)
        return record(target ?: container, "scroll_to", parameters)
    }

    /** Returns the last accepted event sequence, or zero before the first event. */
    fun lastEventSequence(): Int = synchronized(lock) { nextSequence - 1 }

    fun recordBack(): Boolean {
        if (!isEnabled) return false
        return record(policy.backTarget, "back", JSONObject())
    }

    /** Invalidates a session after an unrecorded lifecycle transition. No raw reason is stored. */
    fun markIncomplete() = invalidateSession()

    /**
     * Freezes the session immediately, then writes a durable capture in order.
     * The callback runs on the recorder executor and never on the main thread.
     * It receives this session's capture.json after its sibling session.json is finalized,
     * not the replaceable latest-capture path returned by capturePath().
     */
    fun freezeAndExport(callback: (File) -> Unit = {}): Boolean {
        synchronized(lock) {
            if (!isEnabled || frozen) return false
            if (android.os.SystemClock.elapsedRealtime() - startedElapsedMs > maxDurationMs) truncated = true
            frozen = true
            accepting = false
            try {
                executor.execute {
                    try {
                        publishSessionExport(
                            sessionDirectory = sessionDirectory,
                            writeCapture = ::writeCapture,
                            finalizeMetadata = { destination ->
                                val incomplete = synchronized(lock) { lostEvents || unsupported }
                                writeMetadata(finalized = true, incomplete = incomplete, destination = destination)
                            },
                            callback = callback,
                        )
                    } catch (_: Throwable) {
                        // Metadata remains incomplete when final publication fails.
                        markWriteFailure()
                    }
                }
            } catch (_: Throwable) {
                markWriteFailure()
            }
        }
        return true
    }

    /** Legacy latest-published path; another session may replace it after a report completes. */
    fun capturePath(): File {
        check(isEnabled) { "Recorder is disabled" }
        return publishedCaptureFile
    }

    /** Stops accepting events without claiming that a final capture exists. */
    fun close() {
        synchronized(lock) {
            accepting = false
        }
        if (isEnabled) executor.shutdown()
    }

    private fun record(target: String, action: String, parameters: JSONObject): Boolean {
        synchronized(lock) {
            if (!accepting || frozen) return false
            if (android.os.SystemClock.elapsedRealtime() - startedElapsedMs > maxDurationMs) {
                truncated = true
                accepting = false
                return false
            }
            val event = RecordedEvent(
                id = "e$nextSequence",
                sequence = nextSequence,
                elapsedMs = android.os.SystemClock.elapsedRealtime() - startedElapsedMs,
                action = action,
                target = target,
                parameters = JSONObject(parameters.toString()),
            )
            val line = (event.toJson().toString() + "\n").toByteArray(StandardCharsets.UTF_8)
            // Four copies conservatively cover JSONL, session capture, published
            // capture, and atomic temporary files.
            val reservedForEvent = line.size.toLong() * EVENT_STORAGE_RESERVE_MULTIPLIER
            if (bytesReserved + reservedForEvent > maxBytes) {
                truncated = true
                accepting = false
                return false
            }
            nextSequence += 1
            bytesReserved += reservedForEvent
            events += event
            try {
                // Queue while holding the same lock as the sequence update. The
                // export task can therefore never overtake an accepted event.
                executor.execute { appendLine(line) }
            } catch (_: Throwable) {
                markWriteFailureLocked()
            }
            return true
        }
    }

    private fun appendLine(line: ByteArray) {
        try {
            FileOutputStream(eventsFile, true).use { output ->
                output.write(line)
                output.flush()
            }
        } catch (_: Throwable) {
            markWriteFailure()
        }
    }

    private fun writeCapture(destination: File) {
        val snapshot: List<RecordedEvent>
        val wasTruncated: Boolean
        val wasLost: Boolean
        synchronized(lock) {
            snapshot = events.toList()
            wasTruncated = truncated
            wasLost = lostEvents
        }
        val eventArray = JSONArray()
        snapshot.forEach { event -> eventArray.put(event.toJson()) }
        val capture = JSONObject()
            .put("schemaVersion", 1)
            .put("sessionId", sessionId)
            .put("fixture", JSONObject()
                .put("id", fixtureId)
                .put("version", fixtureVersion)
                .put("inputs", JSONObject()))
            .put("startState", startStateJson)
            .put("events", eventArray)
            .put("truncated", wasTruncated)
            .put("lostEvents", wasLost)
            .put("endSequence", snapshot.lastOrNull()?.sequence ?: 0)
            .put("startedAtMs", startedAtMs)
        val bytes = capture.toString().toByteArray(StandardCharsets.UTF_8)
        atomicWrite(destination, bytes)
        atomicWrite(publishedCaptureFile, bytes)
    }

    private fun writeMetadata(finalized: Boolean, incomplete: Boolean, destination: File = metadataFile) {
        val metadata = JSONObject()
            .put("schemaVersion", 1)
            .put("sessionId", sessionId)
            .put("fixture", JSONObject().put("id", fixtureId).put("version", fixtureVersion))
            .put("startState", startStateJson)
            .put("finalized", finalized)
            .put("incomplete", incomplete)
            .put("lostEvents", synchronized(lock) { lostEvents })
            .put("unsupported", synchronized(lock) { unsupported })
        atomicWrite(destination, metadata.toString(2).toByteArray(StandardCharsets.UTF_8))
    }

    private fun atomicWrite(destination: File, bytes: ByteArray) {
        atomicWriteFile(destination, bytes)
    }

    private fun invalidateSession() {
        synchronized(lock) { invalidateSessionLocked() }
    }

    private fun invalidateSessionLocked() {
        accepting = false
        unsupported = true
        lostEvents = true
    }

    private fun markWriteFailure() {
        synchronized(lock) { markWriteFailureLocked() }
    }

    private fun markWriteFailureLocked() {
        lostEvents = true
    }

    private data class RecordedEvent(
        val id: String,
        val sequence: Int,
        val elapsedMs: Long,
        val action: String,
        val target: String,
        val parameters: JSONObject,
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("id", id)
            .put("seq", sequence)
            .put("elapsedMs", elapsedMs)
            .put("action", action)
            .put("target", target)
            .put("parameters", parameters)
    }

    companion object {
        const val DEFAULT_MAX_DURATION_MS = 10 * 60 * 1000L
        const val DEFAULT_MAX_BYTES = 20L * 1024L * 1024L
        private const val EVENT_STORAGE_RESERVE_MULTIPLIER = 4L
    }
}
