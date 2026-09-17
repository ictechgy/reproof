package io.reproloop.sdk

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Small, opt-in recorder for an annotated app session.
 *
 * The recorder intentionally knows nothing about view coordinates. Callers annotate
 * semantic actions with resource entry names, which makes the capture portable
 * across device sizes. All writes are ordered on one background executor.
 */
class ReproRecorder(
    context: Context,
    private val fixtureId: String = "default",
    private val fixtureVersion: Int = 1,
    startState: JSONObject,
    private val safeTextTargets: Set<String> = setOf("name", "count"),
    private val safeTextValues: Set<String> = setOf("", "QA", "Test"),
    private val maxDurationMs: Long = DEFAULT_MAX_DURATION_MS,
    private val maxBytes: Long = DEFAULT_MAX_BYTES,
    tapTargets: Set<String> = setOf("add", "list", "bottom", "next", "back"),
    scrollTargets: Map<String, Set<String>> = mapOf("list" to setOf("bottom")),
    private val backTarget: String = "back",
    private val reportTarget: String = "report",
) {
    private val appContext = context.applicationContext
    private val tapTargets = tapTargets.toSet()
    private val scrollTargets = scrollTargets.mapValues { (_, ends) -> ends.toSet() }
    private val lock = Any()
    private val executor: ExecutorService = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "repro-recorder").apply { isDaemon = true }
    }
    private val sessionId = UUID.randomUUID().toString()
    private val startedAtMs = System.currentTimeMillis()
    private val startedElapsedMs = android.os.SystemClock.elapsedRealtime()
    private val sessionDirectory = File(appContext.filesDir, "repro/$sessionId")
    private val eventsFile = File(sessionDirectory, "events.jsonl")
    private val captureFile = File(sessionDirectory, "capture.json")
    private val publishedCaptureFile = File(appContext.filesDir, "repro/capture.json")
    private val currentSessionFile = File(appContext.filesDir, "repro/current-session")
    private val metadataFile = File(sessionDirectory, "session.json")
    private val events = ArrayList<RecordedEvent>()
    private val startStateJson = JSONObject(startState.toString())
    private var nextSequence = 1
    // Reserve for JSONL, final capture copies, and atomic temporary files.
    private var bytesReserved = METADATA_RESERVE_BYTES
    private var accepting = true
    private var frozen = false
    private var truncated = false
    private var lostEvents = false
    private var unsupported = false

    init {
        executor.execute {
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
    fun recordTap(target: String): Boolean = record(target, "tap", JSONObject())

    /** Records only allowlisted text fields and values. */
    fun recordReplace(target: String, value: String): Boolean {
        if (!safeTextTargets.contains(target) || target.equals("password", ignoreCase = true)) {
            invalidateSession()
            return false
        }
        if (!safeTextValues.contains(value)) {
            // Reject before constructing an event so an unsafe value cannot reach memory or disk.
            invalidateSession()
            return false
        }
        return record(target, "replace", JSONObject().put("value", value))
    }

    fun recordScrollTo(container: String, direction: String, target: String? = null): Boolean {
        if (container !in scrollTargets || direction !in setOf("forward", "backward")) {
            invalidateSession()
            return false
        }
        if (target != null && target !in scrollTargets.getValue(container)) {
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

    fun recordBack(): Boolean = record(backTarget, "back", JSONObject())

    /** Invalidates a session after an unrecorded lifecycle transition. No raw reason is stored. */
    fun markIncomplete() = invalidateSession()

    /**
     * Freezes the session immediately, then writes a durable capture in order.
     * The callback runs on the recorder executor and never on the main thread.
     */
    fun freezeAndExport(callback: (File) -> Unit = {}): Boolean {
        synchronized(lock) {
            if (frozen) return false
            if (android.os.SystemClock.elapsedRealtime() - startedElapsedMs > maxDurationMs) truncated = true
            frozen = true
            accepting = false
            try {
                executor.execute {
                    try {
                        writeCapture()
                        val incomplete = synchronized(lock) { lostEvents || unsupported }
                        writeMetadata(finalized = true, incomplete = incomplete)
                        callback(publishedCaptureFile)
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

    /** Returns the latest published capture path, when a report has completed. */
    fun capturePath(): File = publishedCaptureFile

    /** Stops accepting events without claiming that a final capture exists. */
    fun close() {
        synchronized(lock) {
            accepting = false
        }
        executor.shutdown()
    }

    private fun record(target: String, action: String, parameters: JSONObject): Boolean {
        synchronized(lock) {
            if (!accepting || frozen) return false
            if (android.os.SystemClock.elapsedRealtime() - startedElapsedMs > maxDurationMs) {
                truncated = true
                accepting = false
                return false
            }
            if (!safeTarget(target)) {
                invalidateSessionLocked()
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

    private fun safeTarget(target: String): Boolean =
        safeTextTargets.contains(target) ||
            target in tapTargets ||
            target == backTarget || target == reportTarget ||
            scrollTargets.any { (container, ends) -> target == container || target in ends }

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

    private fun writeCapture() {
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
        atomicWrite(captureFile, bytes)
        atomicWrite(publishedCaptureFile, bytes)
    }

    private fun writeMetadata(finalized: Boolean, incomplete: Boolean) {
        val metadata = JSONObject()
            .put("schemaVersion", 1)
            .put("sessionId", sessionId)
            .put("fixture", JSONObject().put("id", fixtureId).put("version", fixtureVersion))
            .put("startState", startStateJson)
            .put("finalized", finalized)
            .put("incomplete", incomplete)
            .put("lostEvents", synchronized(lock) { lostEvents })
            .put("unsupported", synchronized(lock) { unsupported })
        atomicWrite(metadataFile, metadata.toString(2).toByteArray(StandardCharsets.UTF_8))
    }

    private fun atomicWrite(destination: File, bytes: ByteArray) {
        val temporary = File(destination.parentFile, "${destination.name}.tmp")
        FileOutputStream(temporary).use { output ->
            output.write(bytes)
            output.fd.sync()
        }
        if (!temporary.renameTo(destination)) {
            throw IllegalStateException("Could not publish ${destination.name}")
        }
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
        private const val METADATA_RESERVE_BYTES = 8L * 1024L
    }
}
