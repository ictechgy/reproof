package io.reproof.sdk

import android.content.Context
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

/** Pure JVM tests: deliberately never instantiate Context, JSONObject or a recorder. */
class ReproPrivacyConfigTest {
    private fun state(name: String = "", count: String = "0") =
        ReproStartState("main", mapOf("name" to name, "count" to count))

    @Test fun defaultSampleRemainsAdmitted() {
        val policy = ReproPrivacyConfig()
        assertTrue(policy.admits(state()))
        assertTrue(policy.admits(state("QA", "999999999")))
        assertTrue(policy.allowsTap("add"))
        assertTrue(policy.allowsReplace("name", "Test"))
        assertTrue(policy.allowsScroll("list", "forward", "bottom"))
        assertTrue(policy.allowsBack("back"))
    }

    @Test fun actionPoliciesDoNotAuthorizeEachOther() {
        val policy = ReproPrivacyConfig(tapTargets = setOf("add"))
        for (target in listOf("name", "count", "list", "bottom", "back", "report")) {
            assertFalse(policy.allowsTap(target))
        }
        assertFalse(policy.allowsReplace("add", "QA"))
        assertFalse(policy.allowsScroll("add", "forward", null))
        assertFalse(policy.allowsScroll("list", "up", "bottom"))
        assertFalse(policy.allowsScroll("list", "forward", "name"))
        assertFalse(policy.allowsBack("add"))
        assertFalse(policy.allowsTap("a".repeat(65)))
        assertFalse(policy.allowsReplace("name", "x".repeat(65)))
        assertFalse(policy.allowsScroll("list", "forward", "x".repeat(65)))
    }

    @Test fun unapprovedStartStateNeverReachesWriteCallback() {
        val policy = ReproPrivacyConfig()
        var serialized = false
        var written = false
        val canary = object {
            override fun toString(): String { serialized = true; error("Canary must not be coerced") }
        }
        val candidates = listOf<Any?>(canary, mapOf("private" to canary), listOf(canary), null,
            "UNAPPROVED_CANARY", "x".repeat(65))
        for (candidate in candidates) {
            admitStartState(policy, "main", 2,
                sequenceOf("name" to candidate, "count" to "0"))?.let { written = true }
        }
        assertFalse(serialized)
        assertFalse(written)
        assertNull(admitStartState(policy, canary, 2, sequenceOf("name" to "", "count" to "0")))
        assertFalse(serialized)
    }

    @Test fun admissionRejectsMissingExtraDuplicateAndOversizedState() {
        val policy = ReproPrivacyConfig()
        assertFalse(policy.admits(ReproStartState("other", state().nodes)))
        assertFalse(policy.admits(ReproStartState("main", mapOf("name" to ""))))
        assertFalse(policy.admits(ReproStartState("main", state().nodes + ("unknown" to ""))))
        assertNull(admitStartState(policy, "main", 2, sequenceOf("name" to "", "name" to "QA")))
        assertNull(admitStartState(policy, "main", 1, sequenceOf("name" to "", "count" to "0")))
        var iterated = false
        val entries = sequence<Pair<String, Any?>> { iterated = true; yield("name" to "") }
        assertNull(admitStartState(policy, "main", 129, entries))
        assertFalse(iterated)
        assertFalse(policy.admits(state(count = "1000000000")))
        assertFalse(policy.admits(state(count = "-1")))
        assertFalse(policy.admits(state(count = "1.0")))
        assertFalse(policy.admits(state(count = "QA")))
        assertFalse(ReproPrivacyConfig(maxBytes = 8192).admits(state()))
    }

    @Test fun policyAndStateDefensivelyCopyEveryCollection() {
        val text = mutableSetOf("name")
        val values = mutableSetOf("")
        val numeric = mutableSetOf("count")
        val screens = mutableSetOf("main")
        val taps = mutableSetOf("add")
        val ends = mutableSetOf("bottom")
        val scroll = mutableMapOf("list" to ends)
        val nodes = mutableMapOf("name" to "", "count" to "0")
        val policy = ReproPrivacyConfig(safeTextTargets = text, safeTextValues = values,
            numericTargets = numeric, allowedStartScreens = screens, tapTargets = taps,
            scrollTargets = scroll)
        val start = ReproStartState("main", nodes)
        text.add("private"); values.add("UNAPPROVED_CANARY"); numeric.add("unknown")
        screens.add("other"); taps.add("name"); ends.add("name"); scroll.clear()
        nodes["name"] = "UNAPPROVED_CANARY"
        assertTrue(policy.admits(start))
        assertEquals("", start.nodes["name"])
        assertFalse(policy.allowsReplace("name", "UNAPPROVED_CANARY"))
        assertFalse(policy.allowsReplace("private", ""))
        assertFalse(policy.allowsTap("name"))
        assertFalse(policy.allowsScroll("list", "forward", "name"))
        assertTrue(policy.allowsScroll("list", "forward", "bottom"))
        try {
            (policy.tapTargets as MutableSet<String>).add("name")
            fail("Policy collection must be immutable")
        } catch (_: UnsupportedOperationException) { }
        try {
            (start.nodes as MutableMap<String, String>)["name"] = "QA"
            fail("Start state must be immutable")
        } catch (_: UnsupportedOperationException) { }
    }

    @Test fun explicitInstrumenterPolicySupportsGeneralProfile() {
        val policy = ReproPrivacyConfig(fixtureId = "inventory", safeTextTargets = setOf("label"),
            numericTargets = setOf("quantity"), allowedStartScreens = setOf("warehouse"),
            tapTargets = setOf("commit"), scrollTargets = emptyMap(), backTarget = "return")
        assertTrue(policy.admits(ReproStartState("warehouse", mapOf("label" to "QA", "quantity" to "12"))))
        assertFalse(policy.admits(ReproStartState("main", mapOf("label" to "QA", "quantity" to "12"))))
        assertFalse(ReproPrivacyConfig().admits(ReproStartState("warehouse", mapOf("label" to "QA"))))
    }

    @Test fun configurationBoundsUseFixedErrorsWithoutEcho() {
        val invalid = listOf<() -> Any>(
            { ReproPrivacyConfig(fixtureId = "PRIVATE_CANARY") },
            { ReproPrivacyConfig(fixtureId = "a".repeat(65)) },
            { ReproPrivacyConfig(fixtureVersion = 0) },
            { ReproPrivacyConfig(fixtureVersion = 1001) },
            { ReproPrivacyConfig(safeTextTargets = (0..64).map { "id_$it" }.toSet()) },
            { ReproPrivacyConfig(safeTextValues = setOf("x".repeat(65))) },
            { ReproPrivacyConfig(safeTextValues = setOf("\n")) },
            { ReproPrivacyConfig(numericTargets = setOf("password")) },
            { ReproPrivacyConfig(allowedStartScreens = emptySet()) },
            { ReproPrivacyConfig(scrollTargets = (0..16).associate { "id_$it" to setOf("end") }) },
            { ReproPrivacyConfig(maxDurationMs = 0) },
            { ReproPrivacyConfig(maxDurationMs = ReproRecorder.DEFAULT_MAX_DURATION_MS + 1) },
            { ReproPrivacyConfig(maxBytes = ReproRecorder.DEFAULT_MAX_BYTES + 1) },
            { ReproStartState("main", (0..128).associate { "id_$it" to "" }) },
        )
        for (create in invalid) {
            try { create(); fail("Expected rejection") }
            catch (error: IllegalArgumentException) {
                assertEquals("Invalid recorder privacy configuration", error.message)
            }
        }
    }

    // Compile-time compatibility only. Never invoked by a JVM test: real JSON adapter needs Android.
    @Suppress("unused")
    private fun constructorCompatibility(context: Context, raw: JSONObject) {
        ReproRecorder(context = context, fixtureId = "default", startState = raw)
        ReproRecorder(context = context, fixtureId = "inventory", fixtureVersion = 1,
            startState = raw, safeTextTargets = setOf("label"), numericTargets = setOf("quantity"),
            allowedStartScreens = setOf("warehouse"), tapTargets = setOf("commit"),
            scrollTargets = emptyMap(), backTarget = "return")
        ReproRecorder(context, ReproPrivacyConfig(), state())
    }
}
