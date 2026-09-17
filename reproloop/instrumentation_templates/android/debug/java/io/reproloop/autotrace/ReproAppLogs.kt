package io.reproloop.autotrace

import android.app.Activity
import android.app.Application
import android.os.Bundle
import android.view.View
import android.view.ViewTreeObserver
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.Collections
import java.util.IdentityHashMap
import java.util.LinkedHashMap
import java.util.UUID
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** Debug-only, bounded automatic lifecycle, screen, and tap observation log. */
object ReproAppLogs {
    private const val MAX_EVENTS = 2_000
    private const val MAX_JSON_BYTES = 1024L * 1024L
    private const val MAX_ELAPSED_MS = 1_800_000L
    private const val MAX_SCREEN_TARGETS = 64
    private const val MAX_CONTEXT_VALUE = 128
    private val UUID_PATTERN = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    private val DIGEST_PATTERN = Regex("[0-9a-f]{64}")
    private val NAME_PATTERN = Regex("[A-Za-z0-9_.-]{1,$MAX_CONTEXT_VALUE}")
    private val lock = Any()
    private val executor: ExecutorService = Executors.newSingleThreadExecutor { task ->
        Thread(task, "repro-app-log").apply { isDaemon = true }
    }
    private var active: Session? = null
    private var registeredApplication: Application? = null
    private var nextObservationToken = -1L

    private val lifecycleCallbacks = object : Application.ActivityLifecycleCallbacks {
        override fun onActivityCreated(activity: Activity, state: Bundle?) = dispatch(activity) { it.created(activity) }
        override fun onActivityStarted(activity: Activity) = dispatch(activity) { it.started(activity) }
        override fun onActivityResumed(activity: Activity) = dispatch(activity) { it.resumed(activity) }
        override fun onActivityPaused(activity: Activity) = dispatch(activity) { it.paused(activity) }
        override fun onActivityStopped(activity: Activity) = dispatch(activity) { it.stopped(activity) }
        override fun onActivitySaveInstanceState(activity: Activity, state: Bundle) = dispatch(activity) { it.saved(activity) }
        override fun onActivityDestroyed(activity: Activity) = dispatch(activity) { it.destroyed(activity) }
    }

    @JvmStatic
    fun start(
        activity: Activity,
        profileDigest: String,
        fixtureId: String,
        fixtureVersion: Int,
        tapTargets: Set<String>,
    ) {
        try {
            if (!ReproConfig.APP_LOGS_ENABLED || !recordable(activity, "record")) return
            val intent = activity.intent ?: return
            if (intent.getStringExtra("fixture_id") != fixtureId ||
                intent.getIntExtra("fixture_version", Int.MIN_VALUE) != fixtureVersion) return
            startSession(activity, profileDigest, tapTargets, "record")
        } catch (_: Throwable) {
            // Automatic observation is optional and never changes product behavior.
        }
    }

    @JvmStatic
    fun startObservation(activity: Activity, profileDigest: String, tapTargets: Set<String>) {
        try {
            if (!ReproConfig.APP_LOGS_ENABLED || !recordable(activity, "observe") ||
                activity.intent?.getStringExtra("repro_observation_profile") != profileDigest) return
            startSession(activity, profileDigest, tapTargets, "observe")
        } catch (_: Throwable) {
            // A rejected observation session cannot affect the application.
        }
    }

    private fun startSession(activity: Activity, profileDigest: String, tapTargets: Set<String>, mode: String) {
        val intent = activity.intent ?: return
        val runId = intent.getStringExtra("repro_log_run_id") ?: return
        if (!UUID_PATTERN.matches(runId) || !DIGEST_PATTERN.matches(profileDigest)) return
        val screenTargets = readScreenTargets()
        val application = activity.application ?: return
        var session: Session?
        synchronized(lock) {
            val current = active
            if (current != null && current.runId == runId && current.profileDigest == profileDigest &&
                current.application === application && current.mode == mode) {
                session = current
            } else {
                active = null
                current?.detach()
                session = Session(
                    application = application,
                    applicationId = activity.packageName,
                    runId = runId,
                    profileDigest = profileDigest,
                    mode = mode,
                    screenTargets = screenTargets,
                    tapTargets = tapTargets,
                )
                if (!session!!.initialize()) {
                    session = null
                } else {
                    active = session
                }
            }
        }
        val current = session ?: return
        register(application)
        synchronized(lock) {
            if (active === current && current.isEnabled() && belongsToRun(activity, current)) {
                current.created(activity)
            }
        }
    }

    /** Called before strict replay guards, so invalidation cannot lose tap observations. */
    @JvmStatic
    fun tapBegan(activity: Activity, target: String): Long {
        return try {
            synchronized(lock) {
                val session = active ?: return 0L
                if (!belongsToRun(activity, session)) return 0L
                if (!session.tapTargets.contains(target)) return 0L
                session.tapBegan(activity, target)
            }
        } catch (_: Throwable) {
            0L
        }
    }

    @JvmStatic
    fun tapReturned(token: Long) = tapFinished(token, "returned")

    @JvmStatic
    fun tapThrew(token: Long) = tapFinished(token, "threw")

    private fun tapFinished(token: Long, name: String) {
        if (token == 0L) return
        try {
            synchronized(lock) {
                val session = active ?: return
                session.tapFinished(token, name)
            }
        } catch (_: Throwable) {
            // The product callback has already completed; logging loss is isolated.
        }
    }

    private fun dispatch(activity: Activity, action: (Session) -> Unit) {
        try {
            synchronized(lock) {
                val session = active ?: return
                if (!belongsToRun(activity, session)) return
                action(session)
            }
        } catch (_: Throwable) {
            // Lifecycle callbacks must never become application failures.
            synchronized(lock) {
                active?.let { if (belongsToRun(activity, it)) it.markLost() }
            }
        }
    }

    private fun belongsToRun(activity: Activity, session: Session): Boolean = try {
        val runId = activity.intent?.getStringExtra("repro_log_run_id")
        activity.application === session.application && activity.packageName == session.applicationId &&
            (runId == null || runId == session.runId && (session.mode != "observe" ||
                recordable(activity, "observe") &&
                activity.intent?.getStringExtra("repro_observation_profile") == session.profileDigest))
    } catch (_: Throwable) {
        false
    }

    private fun register(application: Application) {
        synchronized(lock) {
            if (registeredApplication === application) return
            application.registerActivityLifecycleCallbacks(lifecycleCallbacks)
            registeredApplication = application
        }
    }

    private fun recordable(activity: Activity, mode: String): Boolean =
        activity.applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0 &&
            activity.intent?.getStringExtra("repro_mode") == mode

    private fun readScreenTargets(): Map<String, String> {
        val result = LinkedHashMap<String, String>()
        val values = JSONObject(ReproConfig.SCREEN_TARGETS_JSON)
        if (values.length() > MAX_SCREEN_TARGETS) return emptyMap()
        val keys = values.keys()
        while (keys.hasNext()) {
            val root = keys.next()
            val screen = values.opt(root) as? String ?: return emptyMap()
            if (!NAME_PATTERN.matches(root) || !NAME_PATTERN.matches(screen)) return emptyMap()
            result[root] = screen
        }
        return result
    }

    private class Session(
        val application: Application,
        val applicationId: String,
        val runId: String,
        val profileDigest: String,
        val mode: String,
        val screenTargets: Map<String, String>,
        val tapTargets: Set<String>,
    ) {
        val sessionId = UUID.randomUUID().toString()
        val startedAtMs = System.currentTimeMillis()
        private val startedAtNano = System.nanoTime()
        private val events = ArrayList<Event>()
        private var lastElapsed = 0L
        private val activities = IdentityHashMap<Activity, ActivityState>()
        private val startedActivities = Collections.newSetFromMap(IdentityHashMap<Activity, Boolean>())
        private val pendingTaps = LinkedHashMap<Long, PendingTap>()
        private var truncated = false
        private var lostEvents = false
        private var revision = 0L
        private var writtenRevision = 0L
        private var writeQueued = false
        private var retryAttempted = false
        private var enabled = true
        private var configurationTransition = false
        private val markerFile = File(application.filesDir, "repro/app-log-session.json")
        private val journalFile = File(application.filesDir, "repro/app-logs/$sessionId/app-log.json")

        fun initialize(): Boolean {
            synchronized(lock) { writeQueued = true }
            val marker = JSONObject()
                .put("schemaVersion", 1)
                .put("platform", "android")
                .put("applicationId", applicationId)
                .put("runId", runId)
                .put("sessionId", sessionId)
                .put("profileDigest", profileDigest)
                .put("startedAtMs", startedAtMs)
            return try {
                executor.execute {
                    val empty = journalJson(emptyList(), false, false, 0).toByteArray(StandardCharsets.UTF_8)
                    if (!atomicWrite(journalFile, empty) ||
                        !atomicWrite(markerFile, marker.toString().toByteArray(StandardCharsets.UTF_8))) {
                        synchronized(lock) {
                            enabled = false
                            if (active === this@Session) active = null
                            detachLocked()
                            lostEvents = true
                            revision++
                            writeQueued = false
                        }
                    } else {
                        persist()
                    }
                }
                true
            } catch (_: Throwable) {
                synchronized(lock) { writeQueued = false; lostEvents = true; revision++ }
                false
            }
        }

        fun isEnabled(): Boolean = enabled

        fun created(activity: Activity) {
            if (!enabled) return
            val state = activities[activity] ?: ActivityState(activity).also { activities[activity] = it }
            if (state.created) return
            state.created = true
            append("activity", componentId(activity.javaClass.name), "attached", null)
            append("activity", componentId(activity.javaClass.name), "created", null)
            state.observeScreens()
        }

        fun started(activity: Activity) {
            if (!enabled) return
            val state = activities[activity] ?: ActivityState(activity).also { activities[activity] = it }
            state.observeScreens()
            if (startedActivities.add(activity) && startedActivities.size == 1) {
                if (configurationTransition) {
                    configurationTransition = false
                } else {
                    append("application", "app", "foreground", null)
                }
            }
            if (!state.visible) {
                state.visible = true
                val id = componentId(activity.javaClass.name)
                append("activity", id, "appeared", id)
                state.updateScreens()
            }
            append("activity", componentId(activity.javaClass.name), "started", null)
        }

        fun resumed(activity: Activity) {
            if (!enabled) return
            append("activity", componentId(activity.javaClass.name), "resumed", null)
            activities[activity]?.updateScreens()
        }

        fun paused(activity: Activity) {
            if (enabled) append("activity", componentId(activity.javaClass.name), "paused", null)
        }

        fun stopped(activity: Activity) {
            if (!enabled) return
            activities[activity]?.let {
                it.updateScreens(forceHidden = true)
                if (it.visible) {
                    it.visible = false
                    val id = componentId(activity.javaClass.name)
                    append("activity", id, "disappeared", id)
                }
            }
            if (startedActivities.remove(activity) && startedActivities.isEmpty()) {
                val changingConfigurations = try {
                    activity.isChangingConfigurations
                } catch (_: Throwable) {
                    false
                }
                if (changingConfigurations) {
                    configurationTransition = true
                } else {
                    append("application", "app", "background", null)
                }
            }
            append("activity", componentId(activity.javaClass.name), "stopped", null)
        }

        fun saved(activity: Activity) {
            if (enabled) append("activity", componentId(activity.javaClass.name), "save_state", null)
        }

        fun destroyed(activity: Activity) {
            if (!enabled) return
            val state = activities.remove(activity)
            if (state?.visible == true) state.updateScreens(forceHidden = true)
            state?.dispose()
            if (state?.visible == true) {
                val id = componentId(activity.javaClass.name)
                append("activity", id, "disappeared", id)
            }
            startedActivities.remove(activity)
            append("activity", componentId(activity.javaClass.name), "destroyed", null)
        }

        fun tapBegan(activity: Activity, target: String): Long {
            if (!enabled) return 0L
            if (pendingTaps.size >= MAX_EVENTS || !append("activity", componentId(activity.javaClass.name), "began", target)) {
                return 0L
            }
            val token = nextObservationToken--
            pendingTaps[token] = PendingTap(componentId(activity.javaClass.name), target)
            return token
        }

        fun tapFinished(token: Long, name: String) {
            if (!enabled) return
            val pending = pendingTaps.remove(token) ?: return
            append("activity", pending.componentId, name, pending.target)
        }

        fun detach() {
            detachLocked()
        }

        private fun detachLocked() {
            activities.values.forEach { it.dispose() }
            activities.clear()
            startedActivities.clear()
            configurationTransition = false
            pendingTaps.clear()
        }

        private fun append(component: String, componentId: String, name: String, target: String?): Boolean {
            if (truncated) return false
            if (name !in EVENT_NAMES || (target != null && target.length > MAX_CONTEXT_VALUE)) return false
            if (events.size >= MAX_EVENTS) {
                truncated = true
                revision++
                retryAttempted = false
                schedulePersist()
                return false
            }
            val elapsed = (System.nanoTime() - startedAtNano) / 1_000_000L
            if (elapsed > MAX_ELAPSED_MS) {
                truncated = true
                revision++
                retryAttempted = false
                schedulePersist()
                return false
            }
            lastElapsed = maxOf(lastElapsed, elapsed.coerceAtLeast(0L))
            events += Event(events.size + 1, lastElapsed, eventType(name), component, componentId, name, target)
            revision++
            retryAttempted = false
            schedulePersist()
            return true
        }

        fun markLost() {
            if (lostEvents) return
            lostEvents = true
            revision++
            retryAttempted = false
            schedulePersist()
        }

        private fun schedulePersist() {
            if (writeQueued) return
            writeQueued = true
            try {
                executor.execute { persist() }
            } catch (_: Throwable) {
                writeQueued = false
                lostEvents = true
                revision++
            }
        }

        private fun persist() {
            val captured = synchronized(lock) {
                JournalSnapshot(events.toList(), truncated, lostEvents, revision)
            }
            val bytes = journalJson(captured.events, captured.truncated, captured.lostEvents, captured.events.size)
                .toByteArray(StandardCharsets.UTF_8)
            var success = bytes.size.toLong() <= MAX_JSON_BYTES && atomicWrite(journalFile, bytes)
            var resubmit = false
            synchronized(lock) {
                if (success) {
                    writtenRevision = captured.revision
                    retryAttempted = false
                    writeQueued = false
                    if (revision != writtenRevision) {
                        writeQueued = true
                        resubmit = true
                    }
                } else {
                    lostEvents = true
                    revision++
                    if (!retryAttempted) {
                        retryAttempted = true
                        writeQueued = true
                        resubmit = true
                    } else {
                        writeQueued = false
                    }
                }
            }
            if (resubmit) {
                try {
                    executor.execute { persist() }
                } catch (_: Throwable) {
                    synchronized(lock) { writeQueued = false; lostEvents = true; revision++ }
                }
            }
        }

        private fun journalJson(
            snapshotEvents: List<Event>,
            snapshotTruncated: Boolean,
            snapshotLostEvents: Boolean,
            endSequence: Int,
        ): String {
            val values = JSONObject()
                .put("schemaVersion", 1)
                .put("platform", "android")
                .put("applicationId", applicationId)
                .put("runId", runId)
                .put("sessionId", sessionId)
                .put("profileDigest", profileDigest)
                .put("startedAtMs", startedAtMs)
                .put("endSequence", endSequence)
                .put("truncated", snapshotTruncated)
                .put("lostEvents", snapshotLostEvents)
            val array = org.json.JSONArray()
            snapshotEvents.forEach { event ->
                array.put(JSONObject()
                    .put("seq", event.seq)
                    .put("elapsedMs", event.elapsedMs)
                    .put("type", event.type)
                    .put("name", event.name)
                    .put("component", event.component)
                    .put("componentId", event.componentId)
                    .put("target", event.target ?: JSONObject.NULL))
            }
            return values.put("events", array).toString()
        }

        private inner class ActivityState(private val activity: Activity) {
            var created = false
            var visible = false
            private val screens = LinkedHashMap<String, ScreenState>().apply {
                screenTargets.forEach { (root, name) -> put(root, ScreenState(name)) }
            }
            private var content: View? = null
            private var listener: ViewTreeObserver.OnGlobalLayoutListener? = null

            fun observeScreens() {
                if (screenTargets.isEmpty() || listener != null) return
                val root = try {
                    activity.findViewById<View>(android.R.id.content)
                } catch (_: Throwable) {
                    null
                } ?: return
                content = root
                val callback = ViewTreeObserver.OnGlobalLayoutListener {
                    try {
                        synchronized(lock) {
                            if (active === this@Session && enabled) updateScreens()
                        }
                    } catch (_: Throwable) {
                        synchronized(lock) {
                            if (active === this@Session) {
                                markLost()
                            }
                        }
                    }
                }
                listener = callback
                try {
                    root.viewTreeObserver.addOnGlobalLayoutListener(callback)
                } catch (_: Throwable) {
                    listener = null
                    markLost()
                }
            }

            fun updateScreens(forceHidden: Boolean = false) {
                if (screenTargets.isEmpty()) return
                screenTargets.forEach { (rootName, screenName) ->
                    val state = screens[rootName] ?: return@forEach
                    val id = resolveId(rootName)
                    val view = if (id == null) null else try { activity.findViewById<View>(id) } catch (_: Throwable) { null }
                    val previousView = state.view
                    if (view !== previousView && state.visible) {
                        append("view", state.componentId ?: componentId(activity.javaClass.name), "disappeared", screenName)
                        state.visible = false
                    }
                    if (view !== previousView) {
                        state.view = view
                        if (view != null) state.componentId = componentId(view.javaClass.name)
                    }
                    if (view == null) return@forEach
                    state.componentId = state.componentId ?: componentId(view.javaClass.name)
                    val shown = !forceHidden && visible && try {
                        view.visibility == View.VISIBLE && view.isShown
                    } catch (_: Throwable) {
                        false
                    }
                    if (state.visible != shown) {
                        state.visible = shown
                        append("view", state.componentId!!, if (shown) "appeared" else "disappeared", screenName)
                    }
                }
            }

            fun dispose() {
                val root = content
                val callback = listener
                if (root != null && callback != null) {
                    try {
                        root.viewTreeObserver.removeOnGlobalLayoutListener(callback)
                    } catch (_: Throwable) {
                        // Cleanup is best effort.
                    }
                }
                listener = null
                content = null
                screens.clear()
            }

            private fun resolveId(value: String): Int? {
                val numeric = value.toIntOrNull()
                if (numeric != null && numeric > 0) return numeric
                return try {
                    activity.resources.getIdentifier(value, "id", activity.packageName).takeIf { it > 0 }
                } catch (_: Throwable) {
                    null
                }
            }
        }

        private data class PendingTap(val componentId: String, val target: String)
        private class ScreenState(val name: String) {
            var view: View? = null
            var componentId: String? = null
            var visible = false
        }
    }

    private data class Event(
        val seq: Int,
        val elapsedMs: Long,
        val type: String,
        val component: String,
        val componentId: String,
        val name: String,
        val target: String?,
    )

    private data class JournalSnapshot(
        val events: List<Event>,
        val truncated: Boolean,
        val lostEvents: Boolean,
        val revision: Long,
    )

    private fun atomicWrite(destination: File, bytes: ByteArray): Boolean {
        if (bytes.size.toLong() > MAX_JSON_BYTES) return false
        val parent = destination.parentFile ?: return false
        return try {
            if (!parent.exists() && !parent.mkdirs() && !parent.isDirectory) return false
            val temporary = File(parent, "${destination.name}.tmp.${System.nanoTime()}")
            FileOutputStream(temporary).use { output ->
                output.write(bytes)
                output.flush()
                output.fd.sync()
            }
            if (!temporary.renameTo(destination)) {
                temporary.delete()
                false
            } else true
        } catch (_: Throwable) {
            false
        }
    }

    private fun componentId(className: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(className.toByteArray(StandardCharsets.UTF_8))
        return "c" + digest.take(8).joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }

    private fun eventType(name: String): String = when (name) {
        "began", "returned", "threw" -> "click"
        "appeared", "disappeared" -> "screen"
        else -> "lifecycle"
    }

    private val EVENT_NAMES = setOf(
        "attached", "created", "started", "resumed", "paused", "stopped", "destroyed", "save_state",
        "foreground", "background", "active", "inactive", "connected", "disconnected", "termination_requested",
        "began", "returned", "threw", "appeared", "disappeared",
    )
}
