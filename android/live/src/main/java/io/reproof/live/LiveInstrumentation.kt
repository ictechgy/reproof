package io.reproloop.live

import android.accessibilityservice.AccessibilityService
import android.app.Instrumentation
import android.app.UiAutomation
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Point
import android.hardware.display.DisplayManager
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.text.InputType
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import io.reproloop.nativecommon.NativeAppProfile
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.LinkedHashMap
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.max
import kotlin.math.min
import kotlin.math.ceil

internal class NativeFrameBuffer(
    private val maxFrames: Int = 16,
    private val maxBytes: Int = 12 * 1024 * 1024,
) {
    data class Frame(val id: Long, val bytes: ByteArray)

    private val frames = java.util.ArrayDeque<Frame>()
    private var totalBytes = 0

    @Synchronized
    fun append(id: Long, bytes: ByteArray): Boolean {
        if (id <= 0L || bytes.isEmpty() || bytes.size > maxBytes) return false
        if (!frames.isEmpty() && id <= frames.peekLast().id) return false
        while (!frames.isEmpty() &&
            (frames.size >= maxFrames || totalBytes + bytes.size > maxBytes)) {
            totalBytes -= frames.removeFirst().bytes.size
        }
        frames.addLast(Frame(id, bytes.copyOf()))
        totalBytes += bytes.size
        return true
    }

    @Synchronized
    fun latest(): Frame? = frames.peekLast()?.copy(bytes = frames.peekLast().bytes.copyOf())

    @Synchronized
    fun after(cursor: Long): Frame? {
        val newest = frames.peekLast()?.id ?: return null
        if (cursor > newest) throw IllegalArgumentException("frame_cursor_ahead")
        val selected = frames.firstOrNull { it.id > cursor } ?: frames.peekLast()
        return selected?.copy(bytes = selected.bytes.copyOf())
    }
}

private fun screenshotIntervalMillis(maxFps: Int): Long =
    ceil(1000.0 / maxFps.toDouble()).toLong().coerceAtLeast(1L)

private fun evenFrameDimensions(width: Int, height: Int, maxWidth: Int): Pair<Int, Int>? {
    if (width < 1 || height < 1 || maxWidth < 2) return null
    val outputWidth = if (width > maxWidth) {
        maxWidth - maxWidth % 2
    } else if (width % 2 == 0) {
        width
    } else if (width + 1 <= maxWidth) {
        width + 1
    } else {
        width - 1
    }
    if (outputWidth < 2 || outputWidth > maxWidth || outputWidth % 2 != 0) return null
    val idealHeight = height.toDouble() * outputWidth.toDouble() / width.toDouble()
    val maxEvenHeight = Int.MAX_VALUE.toLong() - (Int.MAX_VALUE % 2)
    val outputHeight = (kotlin.math.round(idealHeight / 2.0) * 2.0)
        .toLong().coerceIn(2L, maxEvenHeight).toInt()
    return outputWidth to outputHeight
}

class LiveInstrumentation : Instrumentation() {
    companion object {
        private const val NATIVE_PROTOCOL_VERSION = 2
        private const val HELPER_VERSION = 2
        private const val NATIVE_CLOCK_ID = "android-elapsed-realtime"

        private fun exactInt(value: JSONObject, key: String): Int? =
            (value.opt(key) as? Int)

        private fun exactLong(value: JSONObject, key: String): Long? = when (val raw = value.opt(key)) {
            is Int -> raw.toLong()
            is Long -> raw
            else -> null
        }
    }
    private var config: LiveConfig? = null
    private var server: NativeHttpServer? = null
    private var automation: android.app.UiAutomation? = null
    private val screenshotExecutor = Executors.newSingleThreadExecutor()
    private val watchdogExecutor = Executors.newSingleThreadScheduledExecutor()
    private val inputLock = Any()
    private val captureLock = Any()
    private val pointers = LinkedHashMap<Int, PointerState>()
    private var pointerDownTime = 0L
    private var pointerLastInputTime = 0L
    @Volatile private var cleanupFailed = false
    @Volatile private var frameWidth = 0
    @Volatile private var frameHeight = 0
    @Volatile private var actualFrameWidth = 0
    @Volatile private var actualFrameHeight = 0
    @Volatile private var geometryVersion = 0
    private var lastGeometry = ""
    private var frameId = 0L
    private var screenshotRunning = AtomicBoolean(false)
    private val generalTargetReady = AtomicBoolean(false)
    private val nativeIncarnation = "native_" + UUID.randomUUID().toString().replace("-", "")
    private val effectGrant = ThreadLocal<AuthorityGrant?>()
    @Volatile private var cleanupGrant: AuthorityGrant? = null

    override fun onCreate(arguments: Bundle?) {
        config = LiveConfig.read(context.applicationContext)
        super.onCreate(arguments)
        start()
    }

    override fun onStart() {
        val liveConfig = config
        if (liveConfig == null) {
            finish(1, Bundle().apply { putString("error", "invalid_live_config") })
            return
        }
        val ui = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
        automation = ui
        val nativeServer = NativeHttpServer(
            config = liveConfig,
            statusProvider = { statusSnapshot() },
            observationProvider = { observeSample() },
            commandStop = { grant -> stopFromServer(grant) },
            nativeIncarnation = nativeIncarnation,
        )
        server = nativeServer
        nativeServer.start()
        startScreenshotLoop(liveConfig)
        watchdogExecutor.scheduleAtFixedRate({ watchdogPointers() }, 1, 1, TimeUnit.SECONDS)
        commandLoop()
    }

    private fun commandLoop() {
        val nativeServer = server ?: return
        while (!nativeServer.isStopped()) {
            val command = nativeServer.nextCommand() ?: break
            val result = execute(command)
            // Reset completes with a fresh frame here; general launch publishes
            // its initial frame inside launchGeneralTarget before acknowledging.
            val finalResult = if (command.action == "reset") {
                if (!captureFrame()) false to "frame_capture_failed" else result
            } else result
            nativeServer.ack(command.id, finalResult.first, finalResult.second, command.authority)
        }
        cleanupAndFinish()
    }

    private fun execute(command: LiveCommand): Pair<Boolean, String?> {
        effectGrant.set(command.authority)
        // Keep only the last authenticated envelope for narrow pointer
        // cancellation. It can never authorize a new application effect once
        // the command thread's effectGrant is cleared.
        cleanupGrant = command.authority
        if (!effectAuthorized()) { effectGrant.remove(); return false to "authority_expired" }
        if (config?.profileDigest != null && requiresGeometry(command) && !targetAppVisible()) {
            synchronized(inputLock) { if (!cancelPointersUnsafe()) cleanupFailed = true }
            effectGrant.remove()
            return false to "target_unavailable"
        }
        if (requiresGeometry(command) && !geometryMatches(command.geometryVersion)) {
            effectGrant.remove()
            return false to "stale_geometry"
        }
        val selectedProfile = config?.profile
        if (command.action !in setOf("report_capture") &&
            selectedProfile != null && command.action !in selectedProfile.actions) {
            effectGrant.remove()
            return false to "unsupported_action"
        }
        return try { when (command.action) {
            "tap" -> highLevelTap(command.payload)
            "long_press" -> highLevelLongPress(command.payload)
            "swipe" -> highLevelSwipe(command.payload)
            "text" -> insertText(command.payload)
            "home" -> pressHome()
            "reset" -> resetTarget(command.payload)
            "launch" -> launchGeneralTarget(command.payload)
            "terminate" -> terminateGeneralTarget(command.payload)
            // This is an authenticated host-only operation. It is intentionally
            // omitted from the advertised action list: the helper clicks the
            // sample's fixed Report resource while retaining UiAutomation.
            "report_capture" -> reportCapture()
            "pointer" -> rawPointer(command.payload)
            else -> false to "unsupported_action"
        } } catch (_: Throwable) {
            false to "input_failed"
        } finally { effectGrant.remove() }
    }

    private fun effectAuthorized(cleanupOnly: Boolean = false): Boolean {
        val liveConfig = config ?: return false
        if (liveConfig.protocolVersion == null) return true
        // startScreenshotLoop sets this once before command admission. Shared
        // stop clears it irreversibly before waiting for a gesture lock, so an
        // older command cannot cross another effect boundary while stop waits.
        if (!cleanupOnly && !screenshotRunning.get()) return false
        val current = effectGrant.get()
        val grant = current ?: (if (cleanupOnly) cleanupGrant else null) ?: return false
        val binding = grant.protocolVersion == NATIVE_PROTOCOL_VERSION &&
            grant.helperIncarnation == liveConfig.helperIncarnation &&
            grant.hostIncarnation == liveConfig.hostIncarnation &&
            grant.providerIncarnation == liveConfig.providerIncarnation &&
            grant.nativeIncarnation == nativeIncarnation &&
            grant.nativeClockId == NATIVE_CLOCK_ID
        // A current host cleanup operation is still deadline-bounded.  Only
        // the autonomous safety path may reuse the last exact binding after
        // expiry, and that path can inject ACTION_CANCEL only.
        return binding && (SystemClock.elapsedRealtime() < grant.nativeDeadlineMs ||
            (cleanupOnly && current == null))
    }

    private fun targetAppVisible(): Boolean = try {
        automation?.rootInActiveWindow?.packageName?.toString() == config?.targetPackage
    } catch (_: Throwable) { false }

    private fun requiresGeometry(command: LiveCommand): Boolean = when (command.action) {
        "tap", "long_press", "swipe", "text" -> true
        "pointer" -> command.payload.optString("phase", "") != "cancel"
        else -> false
    }

    private fun geometryMatches(version: Int?): Boolean {
        if (version == null || version <= 0 || version != geometryVersion || actualFrameWidth <= 0 || actualFrameHeight <= 0) return false
        val manager = context.getSystemService(Context.DISPLAY_SERVICE) as? DisplayManager ?: return false
        val display = manager.getDisplay(android.view.Display.DEFAULT_DISPLAY) ?: return false
        val size = Point()
        display.getRealSize(size)
        return size.x == actualFrameWidth && size.y == actualFrameHeight
    }

    private fun highLevelTap(payload: JSONObject): Pair<Boolean, String?> = synchronized(inputLock) {
        if (pointers.isNotEmpty()) return@synchronized false to "raw_pointer_active"
        val point = point(payload, "x", "y") ?: return@synchronized false to "invalid_bounds"
        if (!pointerDownUnsafe(0, point.first, point.second)) return@synchronized false to "inject_failed"
        SystemClock.sleep(30)
        if (!pointerUpUnsafe(0)) false to "inject_failed" else true to null
    }

    private fun highLevelLongPress(payload: JSONObject): Pair<Boolean, String?> = synchronized(inputLock) {
        if (pointers.isNotEmpty()) return@synchronized false to "raw_pointer_active"
        val point = point(payload, "x", "y") ?: return@synchronized false to "invalid_bounds"
        val duration = duration(payload) ?: return@synchronized false to "invalid_duration"
        if (!pointerDownUnsafe(0, point.first, point.second)) return@synchronized false to "inject_failed"
        SystemClock.sleep(duration)
        if (!pointerUpUnsafe(0)) false to "inject_failed" else true to null
    }

    private fun highLevelSwipe(payload: JSONObject): Pair<Boolean, String?> = synchronized(inputLock) {
        if (pointers.isNotEmpty()) return@synchronized false to "raw_pointer_active"
        val from = point(payload, "fromX", "fromY") ?: return@synchronized false to "invalid_bounds"
        val to = point(payload, "toX", "toY") ?: return@synchronized false to "invalid_bounds"
        val duration = duration(payload) ?: return@synchronized false to "invalid_duration"
        if (!pointerDownUnsafe(0, from.first, from.second)) return@synchronized false to "inject_failed"
        val half = duration / 2
        SystemClock.sleep(half)
        if (!pointerMoveUnsafe(0, to.first, to.second)) {
            cancelPointersUnsafe()
            return@synchronized false to "inject_failed"
        }
        SystemClock.sleep(duration - half)
        if (!pointerUpUnsafe(0)) false to "inject_failed" else true to null
    }

    private fun rawPointer(payload: JSONObject): Pair<Boolean, String?> = synchronized(inputLock) {
        val phase = payload.optString("phase", "")
        if (phase == "cancel") {
            return@synchronized if (cancelPointersUnsafe()) true to null else false to "cancel_failed"
        }
        val pointerId = exactInt(payload, "pointerId") ?: return@synchronized false to "invalid_pointer_id"
        if (pointerId !in 0..4) return@synchronized false to "invalid_pointer_id"
        val point = point(payload, "x", "y") ?: return@synchronized false to "invalid_bounds"
        when (phase) {
            "down" -> if (pointers.containsKey(pointerId)) false to "pointer_exists" else if (pointerDownUnsafe(pointerId, point.first, point.second)) true to null else false to "inject_failed"
            "move" -> if (!pointers.containsKey(pointerId)) false to "pointer_missing" else if (pointerMoveUnsafe(pointerId, point.first, point.second)) true to null else false to "inject_failed"
            "up" -> if (!pointers.containsKey(pointerId)) false to "pointer_missing" else if (pointerMoveUnsafe(pointerId, point.first, point.second) && pointerUpUnsafe(pointerId)) true to null else false to "inject_failed"
            else -> false to "invalid_pointer_phase"
        }
    }

    private fun pointerDownUnsafe(id: Int, x: Double, y: Double): Boolean {
        if (pointers.size >= 5 || frameWidth <= 0 || frameHeight <= 0) return false
        val now = SystemClock.uptimeMillis()
        if (pointers.isEmpty()) {
            pointerDownTime = now
        }
        val next = LinkedHashMap(pointers)
        next[id] = PointerState(id, x, y)
        val index = next.keys.indexOf(id)
        val action = if (pointers.isEmpty()) MotionEvent.ACTION_DOWN else MotionEvent.ACTION_POINTER_DOWN or (index shl MotionEvent.ACTION_POINTER_INDEX_SHIFT)
        if (!injectMotion(next.values.toList(), action, pointerDownTime, now)) return false
        pointers.clear(); pointers.putAll(next)
        pointerLastInputTime = now
        return true
    }

    private fun pointerMoveUnsafe(id: Int, x: Double, y: Double): Boolean {
        val next = LinkedHashMap(pointers)
        next[id] = PointerState(id, x, y)
        val eventTime = SystemClock.uptimeMillis()
        if (!injectMotion(next.values.toList(), MotionEvent.ACTION_MOVE, pointerDownTime, eventTime)) return false
        pointers.clear(); pointers.putAll(next)
        pointerLastInputTime = eventTime
        return true
    }

    private fun pointerUpUnsafe(id: Int): Boolean {
        val current = pointers[id] ?: return false
        val values = pointers.values.toList()
        val index = values.indexOfFirst { it.id == current.id }
        val action = if (values.size == 1) MotionEvent.ACTION_UP else MotionEvent.ACTION_POINTER_UP or (index shl MotionEvent.ACTION_POINTER_INDEX_SHIFT)
        if (!injectMotion(values, action, pointerDownTime, SystemClock.uptimeMillis())) return false
        pointers.remove(id)
        pointerLastInputTime = SystemClock.uptimeMillis()
        if (pointers.isEmpty()) {
            pointerDownTime = 0L
            pointerLastInputTime = 0L
        }
        return true
    }

    private fun cancelPointersUnsafe(): Boolean {
        if (pointers.isEmpty()) return true
        if (!injectMotion(pointers.values.toList(), MotionEvent.ACTION_CANCEL, pointerDownTime, SystemClock.uptimeMillis(), cleanupOnly = true)) return false
        pointers.clear()
        pointerDownTime = 0L
        pointerLastInputTime = 0L
        return true
    }

    private fun injectMotion(values: List<PointerState>, action: Int, downTime: Long, eventTime: Long, cleanupOnly: Boolean = false): Boolean {
        if (values.isEmpty() || !effectAuthorized(cleanupOnly)) return false
        val properties = Array(values.size) { index ->
            MotionEvent.PointerProperties().apply {
                id = values[index].id
                toolType = MotionEvent.TOOL_TYPE_FINGER
            }
        }
        val coords = Array(values.size) { index ->
            MotionEvent.PointerCoords().apply {
                x = (values[index].x * max(1, frameWidth - 1)).toFloat()
                y = (values[index].y * max(1, frameHeight - 1)).toFloat()
                pressure = 1f
                size = 1f
            }
        }
        val event = MotionEvent.obtain(downTime, eventTime, action, values.size, properties, coords, 0, 0, 1f, 1f, 0, 0, android.view.InputDevice.SOURCE_TOUCHSCREEN, 0)
        return try {
            automation?.injectInputEvent(event, true) == true
        } finally {
            event.recycle()
        }
    }

    private fun insertText(payload: JSONObject): Pair<Boolean, String?> {
        val value = payload.optString("value", "")
        if (value.length > 256) return false to "invalid_text"
        val root = automation?.rootInActiveWindow ?: return false to "target_unavailable"
        val node = focusedEditable(root) ?: return false to "editable_required"
        if (node.isPassword || isPasswordInput(node.inputType)) return false to "secure_input"
        val existing = if (node.isShowingHintText) "" else node.text?.toString() ?: ""
        val rawStart = node.textSelectionStart
        val rawEnd = node.textSelectionEnd
        val start = if (rawStart < 0) existing.length else rawStart.coerceIn(0, existing.length)
        val end = if (rawEnd < 0) start else rawEnd.coerceIn(start, existing.length)
        val replacement = existing.substring(0, start) + value + existing.substring(end)
        if (replacement.length > 1024) return false to "text_too_long"
        val setArguments = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, replacement) }
        if (!effectAuthorized() || !node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, setArguments)) return false to "text_injection_failed"
        val selection = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, start + value.length)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, start + value.length)
        }
        if (!effectAuthorized() || !node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selection)) return false to "text_injection_failed"
        return true to null
    }

    private fun focusedEditable(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isFocused && node.isEditable && node.packageName?.toString() == config?.targetPackage) return node
        for (index in 0 until node.childCount) node.getChild(index)?.let { child -> focusedEditable(child)?.let { return it } }
        return null
    }

    private fun pressHome(): Pair<Boolean, String?> {
        synchronized(inputLock) { if (!cancelPointersUnsafe()) return false to "cancel_failed" }
        val now = SystemClock.uptimeMillis()
        val down = KeyEvent(now, now, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_HOME, 0)
        val up = KeyEvent(now, now + 10, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_HOME, 0)
        if (!effectAuthorized()) return false to "authority_expired"
        val downOK = automation?.injectInputEvent(down, true) == true
        val upAuthorized = effectAuthorized()
        val upOK = if (downOK && upAuthorized) automation?.injectInputEvent(up, true) == true else false
        val ok = downOK && upAuthorized && upOK
        return if (ok) true to null else false to "home_failed"
    }

    private fun resetTarget(payload: JSONObject): Pair<Boolean, String?> {
        val liveConfig = config ?: return false to "invalid_config"
        if (liveConfig.profile.general) return false to "unsupported_action"
        val logRunId = if (payload.has("appLogRunId")) payload.opt("appLogRunId") as? String else null
        if (payload.length() > (if (logRunId == null) 0 else 1) ||
            (payload.has("appLogRunId") && (logRunId == null || !liveConfig.recordSdk ||
                !logRunId.matches(Regex("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"))))) return false to "invalid_config"
        val logArguments = if (logRunId == null) "" else " --es repro_log_run_id $logRunId"
        val profile = liveConfig.profile
        synchronized(inputLock) { if (!cancelPointersUnsafe()) return false to "cancel_failed" }
        if (!effectAuthorized() || !runShellAndConsume("am force-stop ${profile.packageName}")) return false to "target_unavailable"
        if (liveConfig.recordSdk && (!effectAuthorized() || !runShellAndConsume("run-as ${profile.packageName} sh -c 'rm -f files/repro/capture.json files/repro/current-session'"))) return false to "target_unavailable"
        val mode = if (liveConfig.recordSdk) "record" else "replay"
        if (!effectAuthorized()) return false to "authority_expired"
        val startOutput = runShellAndRead(
            "am start -W -n ${profile.componentName} --es repro_mode $mode " +
                "--es fixture_id ${profile.fixtureId} --ei fixture_version ${profile.fixtureVersion}$logArguments",
        )
            ?: return false to "target_unavailable"
        if (!startOutput.contains("Status: ok") || !startOutput.contains("Activity: ${profile.componentName}")) return false to "target_unavailable"
        val deadline = SystemClock.uptimeMillis() + 10_000
        while (SystemClock.uptimeMillis() < deadline) {
            if (automation?.rootInActiveWindow?.packageName?.toString() == liveConfig.targetPackage) return true to null
            SystemClock.sleep(100)
        }
        return false to "target_unavailable"
    }

    private fun launchGeneralTarget(payload: JSONObject): Pair<Boolean, String?> {
        val liveConfig = config ?: return false to "invalid_config"
        val profile = liveConfig.profile
        val keys = payload.keys().asSequence().toSet()
        val logRunId = if (payload.has("appLogRunId")) payload.opt("appLogRunId") as? String else null
        val logProfile = if (payload.has("appLogProfileDigest")) payload.opt("appLogProfileDigest") as? String else null
        val wantsLogs = "logs" in profile.observations
        val expectedKeys = if (wantsLogs) setOf("applicationId", "appLogRunId", "appLogProfileDigest") else setOf("applicationId")
        if (!profile.general || keys != expectedKeys ||
            (wantsLogs && (logProfile == null || !logProfile.matches(Regex("[0-9a-f]{64}")))) ||
            (payload.has("appLogRunId") && (logRunId == null ||
                !logRunId.matches(Regex("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")))) ||
            payload.optString("applicationId") != profile.applicationId) return false to "invalid_config"
        synchronized(inputLock) { if (!cancelPointersUnsafe()) return false to "cancel_failed" }
        if (!effectAuthorized()) return false to "authority_expired"
        synchronized(captureLock) { generalTargetReady.set(false) }
        val logArgument = if (logRunId == null) "" else
            " --es repro_mode observe --es repro_log_run_id $logRunId --es repro_observation_profile $logProfile"
        val output = runShellAndRead("am start -S -W -n ${profile.componentName}$logArgument")
            ?: return false to "target_unavailable"
        if (!output.contains("Status: ok") || !output.contains("Activity: ${profile.componentName}")) {
            return false to "target_unavailable"
        }
        val deadline = SystemClock.uptimeMillis() + 10_000
        while (SystemClock.uptimeMillis() < deadline) {
            if (targetAppVisible()) {
                if (!effectAuthorized()) return false to "authority_expired"
                synchronized(captureLock) { generalTargetReady.set(true) }
                if (captureFrame()) return true to null
                synchronized(captureLock) { generalTargetReady.set(false) }
            }
            SystemClock.sleep(100)
        }
        return false to "target_unavailable"
    }

    private fun terminateGeneralTarget(payload: JSONObject): Pair<Boolean, String?> {
        val liveConfig = config ?: return false to "invalid_config"
        val profile = liveConfig.profile
        if (!profile.general || payload.keys().asSequence().toSet() != setOf("applicationId") ||
            payload.optString("applicationId") != profile.applicationId) return false to "invalid_config"
        synchronized(inputLock) { if (!cancelPointersUnsafe()) return false to "cancel_failed" }
        if (!effectAuthorized()) return false to "authority_expired"
        synchronized(captureLock) { generalTargetReady.set(false) }
        return if (runShellAndConsume("am force-stop ${profile.packageName}")) true to null
        else false to "target_unavailable"
    }

    private fun reportCapture(): Pair<Boolean, String?> {
        if (config?.recordSdk != true) return false to "unsupported_action"
        val profile = config?.profile ?: return false to "invalid_config"
        val reportTarget = profile.targets.report ?: return false to "unsupported_action"
        val root = try { automation?.rootInActiveWindow } catch (_: Throwable) { null }
            ?: return false to "target_unavailable"
        if (root.packageName?.toString() != profile.packageName) return false to "target_unavailable"
        val matches = try {
            root.findAccessibilityNodeInfosByViewId("${profile.packageName}:id/$reportTarget")
                .filter { it.isVisibleToUser && it.isEnabled && it.isClickable }
        } catch (_: Throwable) { return false to "target_unavailable" }
        if (matches.size != 1) {
            matches.forEach { it.recycle() }
            return false to "target_unavailable"
        }
        val clicked = try { effectAuthorized() && matches[0].performAction(AccessibilityNodeInfo.ACTION_CLICK) } catch (_: Throwable) { false }
        matches[0].recycle()
        return if (clicked) true to null else false to "target_unavailable"
    }

    private fun runShellAndConsume(command: String): Boolean = runShellAndRead(command) != null

    private fun runShellAndRead(command: String): String? {
        val descriptor = try { automation?.executeShellCommand(command) } catch (_: Throwable) { null } ?: return null
        val output = ByteArrayOutputStream()
        var oversized = false
        return try {
            ParcelFileDescriptor.AutoCloseInputStream(descriptor).use { input ->
                val buffer = ByteArray(1024)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    if (output.size() < 8192) {
                        val remaining = 8192 - output.size()
                        output.write(buffer, 0, min(count, remaining))
                    } else {
                        oversized = true
                    }
                }
            }
            if (oversized) null else output.toString(Charsets.UTF_8.name())
        } catch (_: Throwable) {
            null
        }
    }

    private fun targetFrameAllowed(liveConfig: LiveConfig): Boolean =
        (!liveConfig.profile.general || generalTargetReady.get()) &&
            (liveConfig.profileDigest == null || targetAppVisible())

    private fun captureFrame(): Boolean {
        return synchronized(captureLock) {
        val liveConfig = config ?: return false
        if (!targetFrameAllowed(liveConfig)) return false
        val captureStartMs = SystemClock.elapsedRealtime()
        val screenshot = try { automation?.takeScreenshot() } catch (_: Throwable) { null }
        val captureEndMs = SystemClock.elapsedRealtime()
        val original = screenshot ?: return false
        val nativeTiming = if (liveConfig.protocolVersion != null) JSONObject().apply {
            put("version", 1)
            put("nativeClockId", NATIVE_CLOCK_ID)
            put("nativeIncarnation", nativeIncarnation)
            put("captureStartMs", captureStartMs)
            put("captureEndMs", captureEndMs)
        } else null
        val originalWidth = original.width
        val originalHeight = original.height
        var bitmap = original
        try {
            if (!targetFrameAllowed(liveConfig)) return false
            val dimensions = evenFrameDimensions(bitmap.width, bitmap.height, liveConfig.maxWidth) ?: return false
            if (dimensions.first != bitmap.width || dimensions.second != bitmap.height) {
                val scaled = Bitmap.createScaledBitmap(bitmap, dimensions.first, dimensions.second, true)
                if (scaled !== bitmap) bitmap.recycle()
                bitmap = scaled
            }
            val output = ByteArrayOutputStream()
            if (!bitmap.compress(Bitmap.CompressFormat.JPEG, 60, output)) return false
            val jpeg = output.toByteArray()
            val width = bitmap.width
            val height = bitmap.height
            actualFrameWidth = originalWidth
            actualFrameHeight = originalHeight
            frameWidth = actualFrameWidth
            frameHeight = actualFrameHeight
            val orientation = if (actualFrameWidth >= actualFrameHeight) "landscape" else "portrait"
            val geometry = "$actualFrameWidth:$actualFrameHeight:$orientation"
            if (geometry != lastGeometry) {
                geometryVersion += 1
                lastGeometry = geometry
            }
            frameId += 1
            return server?.publishFrame(frameId, geometryVersion, width, height, orientation, System.currentTimeMillis(), nativeTiming, jpeg) == true
        } finally {
            bitmap.recycle()
        }
        }
    }

    private fun startScreenshotLoop(liveConfig: LiveConfig) {
        if (!screenshotRunning.compareAndSet(false, true)) return
        screenshotExecutor.execute {
            val interval = screenshotIntervalMillis(liveConfig.maxFps)
            while (screenshotRunning.get() && server?.isStopped() == false) {
                val started = SystemClock.uptimeMillis()
                captureFrame()
                val remaining = interval - (SystemClock.uptimeMillis() - started)
                if (remaining > 0) SystemClock.sleep(remaining)
            }
        }
    }

    private fun watchdogPointers() {
        synchronized(inputLock) {
            if (pointers.isNotEmpty() && SystemClock.uptimeMillis() - pointerLastInputTime > 10_000 && !cancelPointersUnsafe()) cleanupFailed = true
        }
    }

    private fun statusSnapshot(): JSONObject = JSONObject().apply {
        val liveConfig = config
        put("ready", server?.isReady() == true)
        put("stopped", server?.isStopped() == true)
        put("profileDigest", liveConfig?.profileDigest ?: JSONObject.NULL)
        put("nativeDigest", liveConfig?.profile?.nativeDigest ?: JSONObject.NULL)
        put("targetPackage", liveConfig?.targetPackage ?: JSONObject.NULL)
        put("generalProfile", liveConfig?.profile?.general == true)
        if (liveConfig?.protocolVersion != null) {
            put("protocolVersion", NATIVE_PROTOCOL_VERSION)
            put("helperVersion", HELPER_VERSION)
            put("helperIncarnation", liveConfig.helperIncarnation)
            put("hostIncarnation", liveConfig.hostIncarnation)
            put("providerIncarnation", liveConfig.providerIncarnation)
            put("nativeIncarnation", nativeIncarnation)
            put("nativeClockId", NATIVE_CLOCK_ID)
            put("nativeTimeMs", SystemClock.elapsedRealtime())
        }
        put("capabilities", JSONObject().apply {
            put("actions", org.json.JSONArray(liveConfig?.profile?.actions?.toList() ?: emptyList<String>()))
            put("inputMode", "continuous-pointer")
            put("maxPointers", 5)
            put("multitouch", true)
            put("media", "framed-jpeg")
            put("observation", if (liveConfig?.profile?.general == true) "registered-resource-ids" else "sample-ids")
            put("sdkCapture", liveConfig?.recordSdk == true)
            put("viewsObservationLaunchVersion", 2)
            put("nativeFrameBufferVersion", 1)
            if (liveConfig?.protocolVersion != null) put("nativeFrameTimingVersion", 1)
        })
        put("activePointerIds", org.json.JSONArray(synchronized(inputLock) { pointers.keys.toList() }))
    }

    private fun observeSample(): JSONObject {
        val profile = config?.profile ?: return statusSnapshot().put("ready", false).put("nodes", org.json.JSONArray())
        val root = try { automation?.rootInActiveWindow } catch (_: Throwable) { null }
        if (root?.packageName?.toString() != profile.packageName) {
            return statusSnapshot()
                .put("ready", false)
                .put("nodes", org.json.JSONArray())
        }
        val allowIds = profile.targets.all
        val rootBounds = android.graphics.Rect()
        root.getBoundsInScreen(rootBounds)
        val nodes = ArrayList<JSONObject>()
        var visited = 0
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 50 || visited >= 2000) return
            visited += 1
            val resource = node.viewIdResourceName
            val id = resource?.takeIf { it.startsWith("${profile.packageName}:id/") }?.substringAfterLast('/')
            if (id != null && id in allowIds) {
                val bounds = android.graphics.Rect()
                node.getBoundsInScreen(bounds)
                val value = JSONObject()
                    .put("id", id)
                    .put("bounds", JSONObject().put("left", bounds.left).put("top", bounds.top).put("right", bounds.right).put("bottom", bounds.bottom))
                    .put("visible", node.isVisibleToUser)
                    .put("enabled", node.isEnabled)
                    .put("clickable", node.isClickable)
                if (id in profile.targets.text) {
                    value.put("hasText", !node.isShowingHintText && !node.text.isNullOrEmpty())
                } else if (id in profile.targets.numeric) {
                    val text = node.text?.toString().orEmpty()
                    if (text.matches(Regex("\\d{1,9}"))) value.put("text", text)
                    else value.put("textValid", false)
                }
                nodes += value
            }
            if (depth == 50 || visited >= 2000) return
            for (index in 0 until node.childCount) {
                val child = node.getChild(index) ?: continue
                try { visit(child, depth + 1) } finally { child.recycle() }
                if (visited >= 2000) return
            }
        }
        try { visit(root, 0) } catch (_: Throwable) {
            return statusSnapshot()
                .put("ready", false)
                .put("nodes", org.json.JSONArray())
        }
        return statusSnapshot()
            .put("ready", true)
            .put("screenBounds", JSONObject().put("left", rootBounds.left)
                .put("top", rootBounds.top).put("right", rootBounds.right)
                .put("bottom", rootBounds.bottom))
            .put("nodes", org.json.JSONArray(nodes))
    }

    private fun stopFromServer(grant: AuthorityGrant?): Boolean {
        effectGrant.set(grant);cleanupGrant = grant
        if (config?.protocolVersion != null && !effectAuthorized(cleanupOnly = true)) {
            effectGrant.remove()
            return false
        }
        screenshotRunning.set(false)
        val clean = synchronized(inputLock) {
            val cancelled = cancelPointersUnsafe()
            if (!cancelled) cleanupFailed = true
            cancelled
        }
        effectGrant.remove()
        return clean
    }

    private fun cleanupAndFinish() {
        synchronized(inputLock) { if (!cancelPointersUnsafe()) cleanupFailed = true }
        screenshotRunning.set(false)
        watchdogExecutor.shutdownNow()
        screenshotExecutor.shutdownNow()
        server?.shutdown()
        finish(if (cleanupFailed) 1 else 0, Bundle())
    }

    private fun point(payload: JSONObject, xKey: String, yKey: String): Pair<Double, Double>? {
        val x = payload.optDouble(xKey, Double.NaN)
        val y = payload.optDouble(yKey, Double.NaN)
        return if (x.isFinite() && y.isFinite() && x in 0.0..1.0 && y in 0.0..1.0) x to y else null
    }

    private fun duration(payload: JSONObject): Long? {
        val value = payload.optDouble("durationMs", Double.NaN)
        return if (value.isFinite() && value in 50.0..3000.0) value.toLong() else null
    }

    private fun isPasswordInput(inputType: Int): Boolean {
        if ((inputType and InputType.TYPE_TEXT_VARIATION_PASSWORD) != 0 ||
            (inputType and InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD) != 0 ||
            (inputType and InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD) != 0 ||
            (inputType and InputType.TYPE_NUMBER_VARIATION_PASSWORD) != 0) return true
        return false
    }

    private data class PointerState(val id: Int, val x: Double, val y: Double)

    private data class LiveConfig(
        val token: String,
        val port: Int,
        val targetPackage: String,
        val maxFps: Int,
        val maxWidth: Int,
        val recordSdk: Boolean,
        val profile: NativeAppProfile,
        val profileDigest: String?,
        val protocolVersion: Int?,
        val helperVersion: Int?,
        val helperIncarnation: String?,
        val hostIncarnation: String?,
        val providerIncarnation: String?,
    ) {
        companion object {
            fun read(context: Context): LiveConfig? {
                val file = File(context.filesDir, "live-config.json")
                val data = try { file.readBytes() } catch (_: Throwable) { return null } finally { file.delete() }
                if (data.size > 16 * 1024) return null
                return try {
                    val objectValue = JSONObject(String(data, Charsets.UTF_8))
                    // Older replay-only hosts did not send recordSdk. Keep
                    // that configuration valid while rejecting all unknown
                    // fields; SDK capture remains opt-in.
                    val required = setOf("token", "port", "targetPackage", "maxFps", "maxWidth")
                    val authorityFields = setOf("protocolVersion", "helperVersion", "helperIncarnation", "hostIncarnation", "providerIncarnation")
                    val expected = required + setOf("recordSdk", "appProfile", "profileDigest") + authorityFields
                    val keys = objectValue.keys().asSequence().toSet()
                    if (!keys.containsAll(required) || !keys.all { it in expected }) return null
                    val token = objectValue.getString("token")
                    val port = exactInt(objectValue, "port") ?: return null
                    val targetPackage = objectValue.getString("targetPackage")
                    val maxFps = exactInt(objectValue, "maxFps") ?: return null
                    val maxWidth = exactInt(objectValue, "maxWidth") ?: return null
                    val recordSdk = if (objectValue.has("recordSdk")) {
                        objectValue.opt("recordSdk") as? Boolean ?: return null
                    } else false
                    val hasProfile = objectValue.has("appProfile")
                    val hasDigest = objectValue.has("profileDigest")
                    if (hasProfile != hasDigest) return null
                    val profileDigest = if (hasDigest) objectValue.getString("profileDigest") else null
                    val profile = if (hasProfile) {
                        NativeAppProfile.parse(objectValue.getJSONObject("appProfile"), profileDigest)
                    } else {
                        NativeAppProfile.legacy()
                    }
                    val hasAuthority = authorityFields.all { objectValue.has(it) }
                    if (authorityFields.any { objectValue.has(it) } != hasAuthority) return null
                    val protocolVersion = if (hasAuthority) exactInt(objectValue, "protocolVersion") ?: return null else null
                    val helperVersion = if (hasAuthority) exactInt(objectValue, "helperVersion") ?: return null else null
                    val helperIncarnation = if (hasAuthority) objectValue.getString("helperIncarnation") else null
                    val hostIncarnation = if (hasAuthority) objectValue.getString("hostIncarnation") else null
                    val providerIncarnation = if (hasAuthority) objectValue.getString("providerIncarnation") else null
                    val idPattern = Regex("[a-z][a-z0-9_-]{0,63}")
                    if (token.isEmpty() || token.length > 512 || port !in 1..65535 ||
                        targetPackage != profile.packageName ||
                        (!hasProfile && targetPackage != "io.reproloop.sample") ||
                        !targetPackage.matches(Regex("[A-Za-z0-9_.]+")) || maxFps !in 1..60 || maxWidth !in 1..960 ||
                        (hasAuthority && (protocolVersion != NATIVE_PROTOCOL_VERSION || helperVersion != HELPER_VERSION ||
                            helperIncarnation?.matches(idPattern) != true || hostIncarnation?.matches(idPattern) != true ||
                            providerIncarnation?.matches(idPattern) != true))) null
                    else LiveConfig(token, port, targetPackage, maxFps, maxWidth, recordSdk, profile, profileDigest,
                        protocolVersion, helperVersion, helperIncarnation, hostIncarnation, providerIncarnation)
                } catch (_: Throwable) { null }
            }
        }
    }

    private data class AuthorityGrant(
        val protocolVersion: Int, val operationId: String, val operationFingerprint: String,
        val payloadDigest: String, val projectId: String, val sessionId: String,
        val controllerId: String, val sequence: Int, val ownershipGeneration: Int,
        val hostIncarnation: String, val helperIncarnation: String,
        val providerIncarnation: String, val nativeIncarnation: String,
        val nativeClockId: String, val nativeDeadlineMs: Long,
    )

    private data class LiveCommand(val id: String, val action: String, val payload: JSONObject, val digest: ByteArray, val geometryVersion: Int?, val authority: AuthorityGrant?)

    private class NativeHttpServer(
        private val config: LiveConfig,
        private val statusProvider: () -> JSONObject,
        private val observationProvider: () -> JSONObject,
        private val commandStop: (AuthorityGrant?) -> Boolean,
        private val nativeIncarnation: String,
    ) {
        private companion object {
            const val MAX_FRAME_BYTES = 3 * 1024 * 1024
            const val MAX_FRAME_HEADER_BYTES = 4096
            const val MAX_FRAME_BUFFER_FRAMES = 16
            const val MAX_FRAME_BUFFER_BYTES = 12 * 1024 * 1024
        }
        private val stopped = AtomicBoolean(false)
        private val ready = AtomicBoolean(false)
        private val firstFrame = AtomicBoolean(false)
        private val activeClients = AtomicInteger(0)
        private val clientExecutor = ThreadPoolExecutor(8, 8, 30, TimeUnit.SECONDS, ArrayBlockingQueue(8))
        private val commandMonitor = Object()
        private var pending: LiveCommand? = null
        private var inflight: String? = null
        private val accepted = LinkedHashMap<String, ByteArray>()
        private val receipts = LinkedHashMap<String, Receipt>()
        private var sequenceWatermark = 0
        private var serverSocket: ServerSocket? = null
        private var latestFrame: ByteArray? = null
        private val frameBuffer = NativeFrameBuffer(MAX_FRAME_BUFFER_FRAMES, MAX_FRAME_BUFFER_BYTES)
        private var serverThread: Thread? = null

        fun start() {
            serverThread = Thread {
                try {
                    serverSocket = ServerSocket(config.port, 8, InetAddress.getByName("127.0.0.1"))
                    ready.set(true)
                    while (!stopped.get()) {
                        val socket = serverSocket?.accept() ?: break
                        if (activeClients.incrementAndGet() > 8) {
                            activeClients.decrementAndGet(); socket.close(); continue
                        }
                        try { clientExecutor.execute { handle(socket) } } catch (_: Throwable) { activeClients.decrementAndGet(); socket.close() }
                    }
                } catch (_: Throwable) {
                    ready.set(false)
                }
            }.apply { name = "repro-live-http"; isDaemon = true }
            serverThread?.start()
        }

        fun isReady(): Boolean = ready.get() && (config.profile.general || firstFrame.get())
        fun isStopped(): Boolean = stopped.get()
        fun requestStop() { stopped.set(true); synchronized(commandMonitor) { commandMonitor.notifyAll() }; try { serverSocket?.close() } catch (_: Throwable) {} }
        fun shutdown() { requestStop(); clientExecutor.shutdownNow() }

        fun nextCommand(): LiveCommand? = synchronized(commandMonitor) {
            while (pending == null && !stopped.get()) commandMonitor.wait(500)
            if (stopped.get()) return@synchronized null
            val command = pending ?: return@synchronized null
            pending = null; inflight = command.id; command
        }

        fun ack(id: String, ok: Boolean, error: String?, authority: AuthorityGrant?) = synchronized(commandMonitor) {
            receipts[id] = Receipt(id, ok, error?.take(64), "best-effort", authority)
            while (receipts.size > 64) receipts.remove(receipts.keys.first())
            if (inflight == id) inflight = null
            commandMonitor.notifyAll()
        }

        fun publishFrame(id: Long, geometryVersion: Int, width: Int, height: Int, orientation: String, capturedAt: Long, nativeTiming: JSONObject?, jpeg: ByteArray): Boolean {
            if (id <= 0L || jpeg.isEmpty() || jpeg.size > MAX_FRAME_BYTES) return false
            val json = JSONObject().apply {
                put("type", "frame"); put("id", id); put("nativeFrameId", id); put("geometryVersion", geometryVersion)
                put("width", width); put("height", height); put("orientation", orientation); put("capturedAt", capturedAt); put("mime", "image/jpeg")
                if (nativeTiming != null) put("nativeTiming", nativeTiming)
            }.toString().toByteArray(Charsets.UTF_8)
            if (json.isEmpty() || json.size > MAX_FRAME_HEADER_BYTES) return false
            val prefix = ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putInt(json.size).putInt(jpeg.size).array()
            val encoded = prefix + json + jpeg
            if (!frameBuffer.append(id, encoded)) return false
            synchronized(this) { latestFrame = encoded }
            firstFrame.set(true)
            return true
        }

        private fun handle(socket: Socket) {
            try {
                socket.soTimeout = 5000
                val request = readRequest(BufferedInputStream(socket.getInputStream())) ?: return
                if (!authorized(request.headers["authorization"])) { respond(socket, 401, "application/json", "{\"error\":\"unauthorized\"}".toByteArray()); return }
                route(socket, request)
            } catch (_: Throwable) {
                try { respond(socket, 400, "application/json", "{\"error\":\"bad_request\"}".toByteArray()) } catch (_: Throwable) {}
            } finally { try { socket.close() } catch (_: Throwable) {}; activeClients.decrementAndGet() }
        }

        private fun route(socket: Socket, request: Request) {
            when {
                request.method == "GET" && request.path == "/status" -> respond(socket, 200, "application/json", statusProvider().toString().toByteArray())
                request.method == "GET" && request.path == "/inspect" -> respond(socket, 200, "application/json", observationProvider().toString().toByteArray())
                request.method == "GET" && request.path == "/frame" -> {
                    val frame = synchronized(this) { latestFrame }
                    if (frame == null) respond(socket, 404, "application/json", "{\"error\":\"frame_unavailable\"}".toByteArray())
                    else respond(socket, 200, "application/x-repro-frame", frame)
                }
                request.method == "GET" && request.path.startsWith("/frames/after/") -> {
                    val rawCursor = request.path.removePrefix("/frames/after/")
                    val cursor = rawCursor.toLongOrNull()
                    if (cursor == null || rawCursor.isEmpty() || rawCursor.any { it !in '0'..'9' }) {
                        respond(socket, 400, "application/json", "{\"error\":\"invalid_frame_cursor\"}".toByteArray())
                    } else {
                        try {
                            val frame = frameBuffer.after(cursor)
                            if (frame == null) respond(socket, 404, "application/json", "{\"error\":\"frame_unavailable\"}".toByteArray())
                            else respond(socket, 200, "application/x-repro-frame", frame.bytes)
                        } catch (_: IllegalArgumentException) {
                            respond(socket, 409, "application/json", "{\"error\":\"frame_cursor_ahead\"}".toByteArray())
                        }
                    }
                }
                request.method == "POST" && request.path == "/command" -> command(socket, request.body)
                request.method == "GET" && request.path.startsWith("/ack/") -> ackResponse(socket, request.path.removePrefix("/ack/"))
                request.method == "POST" && request.path == "/stop" -> {
                    val parsed = try { JSONObject(String(request.body, Charsets.UTF_8)) } catch (_: Throwable) { null }
                    val grant = parsed?.optJSONObject("authority")?.let { parseAuthority(it) }
                    val keys = parsed?.keys()?.asSequence()?.toSet() ?: emptySet()
                    val valid = if (config.protocolVersion == null) keys.isEmpty()
                        else keys == setOf("authority") && grant != null && acceptAuthority(grant)
                    if (!valid) { respond(socket, 409, "application/json", "{\"error\":\"authority_rejected\"}".toByteArray()); return }
                    val clean = commandStop(grant)
                    val response = JSONObject().put("stopped", clean)
                    if (grant != null) response.put("authority", authorityJSON(grant))
                    respond(socket, 200, "application/json", response.toString().toByteArray())
                    requestStop()
                }
                else -> respond(socket, 404, "application/json", "{\"error\":\"not_found\"}".toByteArray())
            }
        }

        private fun command(socket: Socket, body: ByteArray) {
            val parsed = try { JSONObject(String(body, Charsets.UTF_8)) } catch (_: Throwable) { null }
            val id = parsed?.optString("id", "") ?: ""
            val action = parsed?.optString("action", "") ?: ""
            val payload = parsed?.optJSONObject("payload") ?: JSONObject()
            val geometryVersion = if (parsed?.has("geometryVersion") == true) exactInt(parsed, "geometryVersion") else null
            val authority = parsed?.optJSONObject("authority")?.let { parseAuthority(it) }
            val expectedKeys = setOf("id", "action", "payload") +
                (if (geometryVersion == null) emptySet() else setOf("geometryVersion")) +
                (if (config.protocolVersion == null) emptySet() else setOf("authority"))
            val actualKeys = parsed?.keys()?.asSequence()?.toSet() ?: emptySet()
            if (id.isEmpty() || id.length > 256 || action.isEmpty() || action.length > 64) { respond(socket, 400, "application/json", "{\"error\":\"invalid_command\"}".toByteArray()); return }
            if (geometryVersion != null && geometryVersion <= 0) { respond(socket, 400, "application/json", "{\"error\":\"invalid_geometry\"}".toByteArray()); return }
            if (actualKeys != expectedKeys || (config.protocolVersion != null &&
                    (authority == null || authority.operationId != id))) {
                respond(socket, 409, "application/json", "{\"error\":\"authority_rejected\"}".toByteArray()); return
            }
            val digest = MessageDigest.getInstance("SHA-256").digest(body)
            synchronized(commandMonitor) {
                val prior = accepted[id]
                if (prior != null) {
                    if (MessageDigest.isEqual(prior, digest)) respond(socket, 202, "application/json", "{\"accepted\":true}".toByteArray())
                    else respond(socket, 409, "application/json", "{\"error\":\"duplicate_command\"}".toByteArray())
                    return
                }
                if (pending != null || inflight != null || stopped.get()) { respond(socket, 409, "application/json", "{\"error\":\"busy\"}".toByteArray()); return }
                if (config.protocolVersion != null && !acceptAuthority(authority!!)) {
                    respond(socket, 409, "application/json", "{\"error\":\"authority_rejected\"}".toByteArray()); return
                }
                accepted[id] = digest
                while (accepted.size > 64) accepted.remove(accepted.keys.first())
                pending = LiveCommand(id, action, payload, digest, geometryVersion, authority)
                commandMonitor.notifyAll()
            }
            respond(socket, 202, "application/json", "{\"accepted\":true}".toByteArray())
        }

        private fun acceptAuthority(grant: AuthorityGrant): Boolean {
            if (grant.protocolVersion != NATIVE_PROTOCOL_VERSION ||
                grant.helperIncarnation != config.helperIncarnation ||
                grant.hostIncarnation != config.hostIncarnation ||
                grant.providerIncarnation != config.providerIncarnation ||
                grant.nativeIncarnation != nativeIncarnation ||
                grant.nativeClockId != NATIVE_CLOCK_ID || grant.sequence <= sequenceWatermark ||
                SystemClock.elapsedRealtime() >= grant.nativeDeadlineMs) return false
            sequenceWatermark = grant.sequence
            return true
        }

        private fun parseAuthority(value: JSONObject): AuthorityGrant? {
            val expected = setOf("protocolVersion", "operationId", "operationFingerprint", "payloadDigest",
                "projectId", "sessionId", "controllerId", "sequence", "ownershipGeneration",
                "hostIncarnation", "helperIncarnation", "providerIncarnation", "nativeIncarnation",
                "nativeClockId", "nativeDeadlineMs")
            if (value.keys().asSequence().toSet() != expected) return null
            val idPattern = Regex("[a-z][a-z0-9_-]{0,63}")
            val digestPattern = Regex("[0-9a-f]{64}")
            return try {
                val grant = AuthorityGrant(
                    exactInt(value, "protocolVersion") ?: return null, value.getString("operationId"),
                    value.getString("operationFingerprint"), value.getString("payloadDigest"),
                    value.getString("projectId"), value.getString("sessionId"),
                    value.getString("controllerId"), exactInt(value, "sequence") ?: return null,
                    exactInt(value, "ownershipGeneration") ?: return null, value.getString("hostIncarnation"),
                    value.getString("helperIncarnation"), value.getString("providerIncarnation"),
                    value.getString("nativeIncarnation"), value.getString("nativeClockId"),
                    exactLong(value, "nativeDeadlineMs") ?: return null,
                )
                if (grant.protocolVersion != NATIVE_PROTOCOL_VERSION || grant.sequence <= 0 ||
                    grant.ownershipGeneration <= 0 || grant.nativeDeadlineMs < 0 ||
                    !grant.operationFingerprint.matches(digestPattern) || !grant.payloadDigest.matches(digestPattern) ||
                    listOf(grant.operationId, grant.projectId, grant.sessionId, grant.controllerId,
                        grant.hostIncarnation, grant.helperIncarnation, grant.providerIncarnation,
                        grant.nativeIncarnation, grant.nativeClockId).any { !it.matches(idPattern) }) null else grant
            } catch (_: Throwable) { null }
        }

        private fun authorityJSON(grant: AuthorityGrant): JSONObject = JSONObject()
            .put("protocolVersion", grant.protocolVersion).put("operationId", grant.operationId)
            .put("operationFingerprint", grant.operationFingerprint).put("payloadDigest", grant.payloadDigest)
            .put("projectId", grant.projectId).put("sessionId", grant.sessionId)
            .put("controllerId", grant.controllerId).put("sequence", grant.sequence)
            .put("ownershipGeneration", grant.ownershipGeneration)
            .put("hostIncarnation", grant.hostIncarnation).put("helperIncarnation", grant.helperIncarnation)
            .put("providerIncarnation", grant.providerIncarnation).put("nativeIncarnation", grant.nativeIncarnation)
            .put("nativeClockId", grant.nativeClockId).put("nativeDeadlineMs", grant.nativeDeadlineMs)

        private fun ackResponse(socket: Socket, id: String) {
            synchronized(commandMonitor) {
                val receipt = receipts[id]
                if (receipt != null) {
                    val value = JSONObject().put("pending", false).put("id", receipt.id).put("ok", receipt.ok).put("timing", receipt.timing)
                    if (receipt.error != null) value.put("error", receipt.error)
                    if (receipt.authority != null) value.put("authority", authorityJSON(receipt.authority))
                    respond(socket, 200, "application/json", value.toString().toByteArray())
                } else respond(socket, 200, "application/json", "{\"pending\":true}".toByteArray())
            }
        }

        private fun authorized(value: String?): Boolean {
            if (value == null || !value.startsWith("Bearer ")) return false
            val expected = MessageDigest.getInstance("SHA-256").digest(config.token.toByteArray(Charsets.UTF_8))
            val provided = MessageDigest.getInstance("SHA-256").digest(value.substring(7).toByteArray(Charsets.UTF_8))
            return MessageDigest.isEqual(expected, provided)
        }

        private fun readRequest(input: BufferedInputStream): Request? {
            val header = ByteArrayOutputStream()
            var matched = 0
            while (header.size() <= 16 * 1024) {
                val byte = input.read()
                if (byte < 0) return null
                header.write(byte)
                matched = if ((matched == 0 && byte == '\r'.code) || (matched == 1 && byte == '\n'.code) || (matched == 2 && byte == '\r'.code) || (matched == 3 && byte == '\n'.code)) matched + 1 else if (byte == '\r'.code) 1 else 0
                if (matched == 4) break
            }
            if (matched != 4) throw IOException("headers_too_large")
            val text = header.toString(Charsets.UTF_8.name())
            val lines = text.removeSuffix("\r\n\r\n").split("\r\n")
            val first = lines.firstOrNull()?.split(" ") ?: throw IOException("request")
            if (first.size != 3 || first[2] != "HTTP/1.1") throw IOException("request")
            val headers = mutableMapOf<String, String>()
            for (line in lines.drop(1)) {
                val separator = line.indexOf(':'); if (separator <= 0) throw IOException("headers")
                val name = line.substring(0, separator).lowercase(); if (headers.containsKey(name)) throw IOException("headers")
                headers[name] = line.substring(separator + 1).trim()
            }
            if (headers.containsKey("transfer-encoding")) throw IOException("chunked")
            val length = headers["content-length"]?.toIntOrNull() ?: 0
            if (length < 0 || length > 16 * 1024 || (first[0] == "POST" && !headers.containsKey("content-length"))) throw IOException("body")
            if (first[0] == "GET" && length != 0) throw IOException("body")
            val body = ByteArray(length); var offset = 0
            while (offset < length) { val count = input.read(body, offset, length - offset); if (count <= 0) throw IOException("body"); offset += count }
            return Request(first[0], first[1], headers, body)
        }

        private fun respond(socket: Socket, status: Int, contentType: String, body: ByteArray) {
            val reason = when (status) { 200 -> "OK"; 202 -> "Accepted"; 400 -> "Bad Request"; 401 -> "Unauthorized"; 404 -> "Not Found"; 409 -> "Conflict"; else -> "Error" }
            val head = "HTTP/1.1 $status $reason\r\nContent-Type: $contentType\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray(Charsets.UTF_8)
            socket.getOutputStream().apply { write(head); write(body); flush() }
        }

        private data class Request(val method: String, val path: String, val headers: Map<String, String>, val body: ByteArray)
        private data class Receipt(val id: String, val ok: Boolean, val error: String?, val timing: String,
                                   val authority: AuthorityGrant?)
    }
}
