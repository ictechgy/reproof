package io.reproloop.driver

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.app.Instrumentation
import android.app.UiAutomation
import android.graphics.Rect
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import io.reproloop.nativecommon.NativeAppProfile
import org.json.JSONArray
import org.json.JSONObject

/**
 * Dependency-free semantic driver. It talks to the target app only through the
 * platform accessibility bridge and never falls back to screen coordinates.
 */
class DriverInstrumentation : Instrumentation() {
    private var commandArgs: Bundle = Bundle()
    private var accessibilityConfigured = false
    private var configuredProfile: NativeAppProfile? = null
    private var configuredProfileDigest: String? = null

    override fun onCreate(arguments: Bundle?) {
        commandArgs = arguments ?: Bundle()
        super.onCreate(commandArgs)
        // Instrumentation instances are created by ActivityThread; explicitly
        // enter the runner lifecycle so onStart executes for `am instrument`.
        start()
    }

    override fun onStart() {
        val result = try {
            val op = args().getString("op") ?: "observe"
            val packageName = args().getString("package")
                ?: error("package is required")
            loadProfile(packageName)
            val operation = when (op) {
                "observe" -> observe(packageName, args().getString("target"))
                "tap" -> tap(packageName, requiredTarget())
                "replace" -> replace(packageName, requiredTarget(), args().getString("value") ?: "")
                "scroll_to" -> scrollTo(
                    packageName = packageName,
                    containerTarget = args().getString("container") ?: requiredTarget(),
                    direction = args().getString("direction") ?: "forward",
                    target = args().getString("target")?.takeIf { it.isNotEmpty() },
                )
                "back" -> back(packageName, requiredTarget())
                else -> error("unsupported op: $op")
            }
            withProfileMetadata(operation)
        } catch (exception: Exception) {
            withProfileMetadata(JSONObject().put("ok", false).put("error", exception.message ?: "driver error"))
        }
        finish(0, Bundle().apply { putString("result", result.toString()) })
    }

    private fun loadProfile(packageName: String) {
        val raw = args().getString("app_profile")
        val suppliedDigest = args().getString("profile_digest")
        val profile = if (raw == null) {
            require(suppliedDigest == null) { "profile_digest requires app_profile" }
            NativeAppProfile.legacy()
        } else {
            require(raw.length <= 16 * 1024) { "app_profile is too large" }
            require(!suppliedDigest.isNullOrEmpty()) { "profile_digest is required" }
            NativeAppProfile.parse(JSONObject(raw), suppliedDigest)
        }
        require(packageName == profile.packageName) { "package differs from app profile" }
        configuredProfile = profile
        configuredProfileDigest = suppliedDigest
    }

    private fun withProfileMetadata(value: JSONObject): JSONObject = value
        .put("profileDigest", configuredProfileDigest ?: JSONObject.NULL)
        .put("nativeDigest", configuredProfile?.nativeDigest ?: JSONObject.NULL)

    private fun profile(): NativeAppProfile = configuredProfile ?: error("profile is not configured")

    private fun textTargets(): Set<String> = profile().targets.text

    private fun allowedIds(): Set<String> = profile().targets.all

    private fun requiredTarget(): String = args().getString("target")?.takeIf { it.isNotBlank() }
        ?: error("target is required")

    private fun args(): Bundle = commandArgs

    private fun observe(packageName: String, requestedTarget: String?): JSONObject {
        if (!requestedTarget.isNullOrEmpty() && requestedTarget !in allowedIds()) {
            error("observe target is not allowlisted: $requestedTarget")
        }
        val root = rootFor(packageName)
        val nodes = JSONArray()
        walk(root) { node ->
            val id = entryName(node.viewIdResourceName, packageName)
            if (id != null && id in allowedIds() &&
                (requestedTarget.isNullOrEmpty() || requestedTarget == id)) {
                nodes.put(nodeJson(node, id))
            }
        }
        return JSONObject().put("ok", true).put("nodes", nodes)
    }

    private fun tap(packageName: String, target: String): JSONObject {
        if (target !in profile().targets.tap && target != profile().targets.report) {
            error("tap target is not allowlisted: $target")
        }
        val node = uniqueVisible(rootFor(packageName), packageName, target)
        if (!node.isClickable) error("target is not clickable: $target")
        if (!node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            error("tap failed: $target")
        }
        return JSONObject().put("ok", true).put("target", target)
    }

    private fun replace(packageName: String, target: String, value: String): JSONObject {
        if (target !in textTargets()) error("text target is not allowlisted: $target")
        if (value !in setOf("", "QA", "Test")) error("text value is not allowlisted")
        val node = uniqueVisible(rootFor(packageName), packageName, target)
        if (!node.isEditable) error("target is not editable: $target")
        // ACTION_FOCUS returns false when the field already owns focus on some devices.
        // SET_TEXT is the operation whose success matters; the host verifies the resulting value.
        if (!node.isFocused) node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, value)
        }
        if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
            error("replace failed: $target")
        }
        return JSONObject().put("ok", true).put("target", target)
    }

    private fun scrollTo(
        packageName: String,
        containerTarget: String,
        direction: String,
        target: String?,
    ): JSONObject {
        val ends = profile().targets.scroll[containerTarget]
            ?: error("scroll container is not allowlisted: $containerTarget")
        if (target != null && target !in ends) error("scroll target is not allowlisted: $target")
        val action = when (direction.lowercase()) {
            "forward", "down" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            "backward", "up" -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
            else -> error("direction must be forward or backward")
        }
        repeat(MAX_SCROLLS) { index ->
            val root = rootFor(packageName)
            if (target != null && findVisible(root, packageName, target).size == 1) {
                return JSONObject().put("ok", true).put("scrolls", index)
            }
            val container = uniqueVisible(root, packageName, containerTarget)
            if (!container.performAction(action)) error("scroll failed: $containerTarget")
            waitForUi()
        }
        if (target != null) error("target not visible after $MAX_SCROLLS scrolls: $target")
        return JSONObject().put("ok", true).put("scrolls", MAX_SCROLLS)
    }

    private fun back(packageName: String, target: String): JSONObject {
        if (target != profile().targets.back) error("back target is not allowlisted: $target")
        rootFor(packageName)
        if (!automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) {
            error("back failed")
        }
        return JSONObject().put("ok", true)
    }

    private fun rootFor(packageName: String): AccessibilityNodeInfo {
        configureAccessibility()
        val deadline = SystemClock.uptimeMillis() + ROOT_TIMEOUT_MS
        while (SystemClock.uptimeMillis() < deadline) {
            val root = automation.rootInActiveWindow
            if (root != null && root.packageName?.toString() == packageName) return root
            waitForUi()
        }
        error("no active window for package: $packageName")
    }

    private fun configureAccessibility() {
        if (accessibilityConfigured) return
        val info = automation.serviceInfo ?: return
        info.flags = info.flags or
            AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or
            AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
        automation.serviceInfo = info
        accessibilityConfigured = true
    }

    private fun uniqueVisible(root: AccessibilityNodeInfo, packageName: String, target: String): AccessibilityNodeInfo {
        val matches = findVisible(root, packageName, target)
        if (matches.size != 1) error("expected one visible target $target, found ${matches.size}")
        return matches.single()
    }

    private fun findVisible(root: AccessibilityNodeInfo, packageName: String, target: String): List<AccessibilityNodeInfo> {
        val matches = ArrayList<AccessibilityNodeInfo>()
        walk(root) { node ->
            if (node.isVisibleToUser && node.packageName?.toString() == packageName &&
                entryName(node.viewIdResourceName, packageName) == target
            ) matches += node
        }
        return matches
    }

    private fun walk(node: AccessibilityNodeInfo, visit: (AccessibilityNodeInfo) -> Unit) {
        visit(node)
        for (index in 0 until node.childCount) {
            node.getChild(index)?.let { child -> walk(child, visit) }
        }
    }

    private fun entryName(resourceName: String?, packageName: String): String? {
        val prefix = "$packageName:id/"
        return resourceName?.takeIf { it.startsWith(prefix) }?.removePrefix(prefix)
    }

    private fun nodeJson(node: AccessibilityNodeInfo, id: String): JSONObject {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val json = JSONObject()
            .put("id", id)
            .put("className", node.className?.toString() ?: "")
            .put("visible", node.isVisibleToUser)
            .put("enabled", node.isEnabled)
            .put("clickable", node.isClickable)
            .put("bounds", JSONObject()
                .put("left", bounds.left)
                .put("top", bounds.top)
                .put("right", bounds.right)
                .put("bottom", bounds.bottom))
        if (id in textTargets()) {
            // Accessibility exposes EditText hints through text when the field is empty.
            // A hint is UI metadata, not user input, and must not enter a capture.
            val text = if (node.isShowingHintText) "" else node.text?.toString() ?: ""
            val allowed = text in setOf("", "QA", "Test")
            json.put("text", if (allowed) text else "")
            json.put("redacted", !allowed)
        } else if (id in profile().targets.numeric) {
            val text = node.text?.toString() ?: ""
            val allowed = text.matches(Regex("[0-9]{1,9}"))
            json.put("text", if (allowed) text else "")
                .put("redacted", !allowed)
        }
        return json
    }

    private fun waitForUi() = SystemClock.sleep(UI_POLL_MS)

    private val automation: UiAutomation
        get() = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)

    companion object {
        private const val ROOT_TIMEOUT_MS = 5_000L
        private const val UI_POLL_MS = 50L
        private const val MAX_SCROLLS = 20
    }
}
