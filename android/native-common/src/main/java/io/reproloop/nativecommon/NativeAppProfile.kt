package io.reproloop.nativecommon

import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.util.Locale

/**
 * The deliberately small, executable part of an administrator-selected app
 * profile. Build and edit policy never crosses the device boundary.
 */
class NativeAppProfile private constructor(
    val packageName: String,
    val activity: String,
    val fixtureId: String,
    val fixtureVersion: Int,
    val startState: JSONObject,
    val targets: Targets,
    val applicationId: String?,
    val general: Boolean,
    val actions: Set<String>,
    val observations: Set<String>,
    private val descriptor: JSONObject,
    val profileDigest: String?,
) {
    val nativeDigest: String = sha256(canonical(descriptor))

    val componentName: String
        get() {
            val qualified = when {
                activity.startsWith(".") -> packageName + activity
                activity.contains(".") -> activity
                else -> "$packageName.$activity"
            }
            val short = if (qualified.startsWith("$packageName.")) qualified.removePrefix(packageName) else qualified
            return "$packageName/$short"
        }

    data class Targets(
        val tap: Set<String>,
        val text: Set<String>,
        val numeric: Set<String>,
        val scroll: Map<String, Set<String>>,
        val back: String,
        val report: String?,
    ) {
        val all: Set<String>
            get() = linkedSetOf<String>().apply {
                addAll(tap)
                addAll(text)
                addAll(numeric)
                addAll(scroll.keys)
                scroll.values.forEach { addAll(it) }
                add(back)
                report?.let { add(it) }
            }
    }

    fun descriptorCopy(): JSONObject = JSONObject(descriptor.toString())

    companion object {
        private val PACKAGE = Regex("[a-zA-Z][a-zA-Z0-9_]*(?:\\.[a-zA-Z][a-zA-Z0-9_]*)+")
        private val ACTIVITY = Regex("\\.?[A-Za-z_][A-Za-z0-9_]*(?:\\.[A-Za-z_][A-Za-z0-9_]*)*")
        private val ID = Regex("[a-z][a-z0-9_]{0,63}")
        private val NUMERIC = Regex("[0-9]{1,9}")
        private val SAFE_TEXT = setOf("", "QA", "Test")
        private val GENERAL_ACTIONS = setOf("tap", "long_press", "swipe", "text", "home", "pointer", "launch", "terminate")
        private val GENERAL_OBSERVATIONS = setOf("pixels", "accessibility", "logs")

        /** Parse and validate only the native descriptor sent in live-config. */
        fun parse(value: JSONObject, profileDigest: String? = null): NativeAppProfile {
            if (value.has("schemaVersion")) return parseGeneral(value, profileDigest)
            requireKeys(value, setOf("package", "activity", "fixture", "startState", "targets"))
            val packageName = value.getString("package")
            require(PACKAGE.matches(packageName) && packageName !in setOf("io.reproloop.live", "io.reproloop.driver")) {
                "invalid profile package"
            }
            val activity = value.getString("activity")
            require(ACTIVITY.matches(activity)) { "invalid profile activity" }

            val fixture = value.getJSONObject("fixture")
            requireKeys(fixture, setOf("id", "version", "inputs"))
            val fixtureId = fixture.getString("id")
            require(identifier(fixtureId)) { "invalid profile fixture" }
            val fixtureVersion = fixture.opt("version") as? Int
                ?: throw IllegalArgumentException("invalid profile fixture")
            require(fixtureVersion in 1..1000 && fixture.getJSONObject("inputs").length() == 0) {
                "invalid profile fixture"
            }

            val targetsObject = value.getJSONObject("targets")
            requireKeys(targetsObject, setOf("tap", "text", "numeric", "scroll", "back", "report"))
            val tap = identifiers(targetsObject.getJSONArray("tap"), "tap")
            val text = identifiers(targetsObject.getJSONArray("text"), "text")
            val numeric = identifiers(targetsObject.getJSONArray("numeric"), "numeric")
            require(numeric.isNotEmpty() && text.intersect(numeric).isEmpty()) {
                "invalid profile observation targets"
            }
            val scrollObject = targetsObject.getJSONObject("scroll")
            require(scrollObject.length() <= 16) { "too many profile scroll targets" }
            val scroll = linkedMapOf<String, Set<String>>()
            val scrollKeys = scrollObject.keys()
            while (scrollKeys.hasNext()) {
                val container = scrollKeys.next()
                require(identifier(container)) { "invalid profile scroll container" }
                val ends = identifiers(scrollObject.getJSONArray(container), "scroll end")
                require(ends.isNotEmpty()) { "missing profile scroll end target" }
                scroll[container] = ends
            }
            val back = targetsObject.getString("back")
            val report = if (targetsObject.isNull("report")) null else targetsObject.getString("report")
            require(identifier(back) && (report == null || identifier(report))) { "invalid profile action target" }

            val start = value.getJSONObject("startState")
            requireKeys(start, setOf("screen", "nodes"))
            val screen = start.getString("screen")
            require(identifier(screen)) { "invalid profile start screen" }
            val nodes = start.getJSONObject("nodes")
            require(nodes.length() > 0) { "missing profile start state" }
            require(nodes.keys().asSequence().toSet() == text + numeric) {
                "profile start state must specify every observation target"
            }
            val nodeKeys = nodes.keys()
            while (nodeKeys.hasNext()) {
                val target = nodeKeys.next()
                require(identifier(target)) { "invalid profile start target" }
                val state = nodes.getString(target)
                require(
                    (target in text && state in SAFE_TEXT) ||
                        (target in numeric && NUMERIC.matches(state)),
                ) { "profile start value outside observation policy" }
            }

            if (profileDigest != null) {
                require(profileDigest.matches(Regex("[0-9a-fA-F]{64}"))) { "invalid profile digest" }
            }
            return NativeAppProfile(
                packageName = packageName,
                activity = activity,
                fixtureId = fixtureId,
                fixtureVersion = fixtureVersion,
                startState = JSONObject(start.toString()),
                targets = Targets(tap, text, numeric, scroll, back, report),
                applicationId = null,
                general = false,
                actions = setOf("tap", "long_press", "swipe", "text", "home", "reset", "pointer"),
                observations = setOf("pixels", "accessibility"),
                descriptor = JSONObject(value.toString()),
                profileDigest = profileDigest,
            )
        }

        private fun parseGeneral(value: JSONObject, profileDigest: String?): NativeAppProfile {
            requireKeys(value, setOf("schemaVersion", "package", "activity", "applicationId",
                "actions", "locatorTargets", "observations"))
            require(value.opt("schemaVersion") is Int && value.getInt("schemaVersion") == 2) {
                "invalid general profile version"
            }
            val packageName = value.getString("package")
            val activity = value.getString("activity")
            val applicationId = value.getString("applicationId")
            require(PACKAGE.matches(packageName) && packageName !in setOf("io.reproloop.live", "io.reproloop.driver") &&
                ACTIVITY.matches(activity) && identifier(applicationId)) { "invalid general target" }
            val actions = stringSet(value.getJSONArray("actions"), GENERAL_ACTIONS, 16, "general action")
            require(actions.isNotEmpty()) { "missing general actions" }
            val locatorTargets = identifiers(value.getJSONArray("locatorTargets"), "locator")
            val observations = stringSet(value.getJSONArray("observations"), GENERAL_OBSERVATIONS, 3, "observation")
            require((locatorTargets.isEmpty() || "accessibility" in observations) &&
                ("pixels" in observations)) { "unsupported general observations" }
            require(profileDigest?.matches(Regex("[0-9a-f]{64}")) == true) { "invalid profile digest" }
            val targets = Targets(locatorTargets, locatorTargets, emptySet(), emptyMap(),
                locatorTargets.firstOrNull() ?: "root", null)
            return NativeAppProfile(
                packageName, activity, "general", 1, JSONObject(), targets,
                applicationId, true, actions, observations,
                JSONObject(value.toString()), profileDigest,
            )
        }

        /** The only descriptor accepted when the host did not opt into a profile. */
        fun legacy(): NativeAppProfile = parse(
            JSONObject()
                .put("package", "io.reproloop.sample")
                .put("activity", ".MainActivity")
                .put("fixture", JSONObject().put("id", "default").put("version", 1).put("inputs", JSONObject()))
                .put("startState", JSONObject().put("screen", "main").put("nodes", JSONObject().put("count", "0").put("name", "")))
                .put("targets", JSONObject()
                    .put("tap", JSONArray(listOf("add", "next", "back", "bottom")))
                    .put("text", JSONArray(listOf("name")))
                    .put("numeric", JSONArray(listOf("count")))
                    .put("scroll", JSONObject().put("list", JSONArray(listOf("bottom"))))
                    .put("back", "back")
                    .put("report", "report")),
        )

        private fun identifiers(values: JSONArray, label: String): Set<String> {
            val limit = if (label == "scroll end") 32 else 64
            require(values.length() <= limit) { "too many profile $label targets" }
            val result = linkedSetOf<String>()
            for (index in 0 until values.length()) {
                val target = values.getString(index)
                require(identifier(target)) { "invalid profile $label target" }
                require(result.add(target)) { "duplicate profile $label target" }
            }
            return result
        }

        private fun stringSet(values: JSONArray, allowed: Set<String>, limit: Int, label: String): Set<String> {
            require(values.length() <= limit) { "too many $label values" }
            val result = linkedSetOf<String>()
            for (index in 0 until values.length()) {
                val item = values.getString(index)
                require(item in allowed && result.add(item)) { "invalid or duplicate $label" }
            }
            return result
        }

        private fun identifier(value: String): Boolean = ID.matches(value) &&
            listOf("password", "secret", "token", "email", "phone").none { value.contains(it) }

        private fun requireKeys(value: JSONObject, expected: Set<String>) {
            val actual = value.keys().asSequence().toSet()
            require(actual == expected) { "unsupported profile fields" }
        }

        /** Python's digest() uses sorted keys and compact UTF-8 JSON. */
        private fun canonical(value: Any?): String = when (value) {
            null, JSONObject.NULL -> "null"
            is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(
                prefix = "{", postfix = "}", separator = ",",
            ) { key -> "${JSONObject.quote(key)}:${canonical(value.get(key))}" }
            is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") {
                canonical(value.get(it))
            }
            is String -> JSONObject.quote(value)
            is Boolean -> value.toString()
            is Number -> value.toString()
            else -> error("unsupported profile JSON value")
        }

        private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { byte -> String.format(Locale.US, "%02x", byte.toInt() and 0xff) }
    }
}
