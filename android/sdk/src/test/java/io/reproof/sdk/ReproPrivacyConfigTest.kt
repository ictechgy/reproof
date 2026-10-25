package io.reproof.sdk

import android.content.Context
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.io.IOException
import java.nio.file.Files

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

    @Suppress("unused")
    private fun exportCompatibility(recorder: ReproRecorder) {
        val accepted: Boolean = recorder.freezeAndExport()
        recorder.freezeAndExport { capture: File -> capture.name }
        val latest: File = recorder.capturePath()
    }
}

/** Tests the production export orchestration with synthetic files, not Android JSON or fsync. */
class ReproSessionExportTest {
    private fun withExportRoot(test: (File) -> Unit) {
        val root = Files.createTempDirectory("repro-session-export-").toFile()
        try { test(root) } finally { root.deleteRecursively() }
    }

    @Test fun retainedCallbackKeepsItsCaptureAndMetadataAfterLaterPublication() = withExportRoot { root ->
        val published = File(root, "capture.json")
        val callbacks = mutableListOf<File>()
        val order = mutableListOf<String>()
        val callerThread = Thread.currentThread()
        for (id in listOf("first", "second")) {
            val directory = File(root, id).apply { check(mkdirs()) }
            publishSessionExport(
                sessionDirectory = directory,
                writeCapture = { destination ->
                    assertSame(callerThread, Thread.currentThread())
                    assertEquals(File(directory, "capture.json"), destination)
                    destination.writeText("capture:$id")
                    published.writeText(destination.readText())
                    order += "$id:capture"
                },
                finalizeMetadata = { destination ->
                    assertSame(callerThread, Thread.currentThread())
                    assertEquals(File(directory, "session.json"), destination)
                    destination.writeText("metadata:$id")
                    order += "$id:metadata"
                },
                callback = { capture ->
                    assertSame(callerThread, Thread.currentThread())
                    assertEquals("capture:$id", capture.readText())
                    assertEquals("metadata:$id", File(capture.parentFile, "session.json").readText())
                    callbacks += capture
                    order += "$id:callback"
                },
            )
        }
        assertEquals(listOf("first:capture", "first:metadata", "first:callback",
            "second:capture", "second:metadata", "second:callback"), order)
        assertEquals(2, callbacks.size)
        assertEquals(File(root, "first/capture.json"), callbacks[0])
        assertEquals(File(root, "second/capture.json"), callbacks[1])
        assertNotEquals(published, callbacks[0])
        assertEquals("capture:second", published.readText())
        // Delayed consumption of the first callback must not read the second export.
        assertEquals("capture:first", callbacks[0].readText())
        assertEquals("metadata:first", File(callbacks[0].parentFile, "session.json").readText())
        assertEquals("metadata:second", File(callbacks[1].parentFile, "session.json").readText())
        assertFalse(File(root, "session.json").exists())
    }

    @Test fun newerPublicationBeforeFirstCallbackCannotReplaceItsSessionPair() = withExportRoot { root ->
        val published = File(root, "capture.json")
        val first = File(root, "first").apply { check(mkdirs()) }
        val second = File(root, "second").apply { check(mkdirs()) }
        val callbacks = mutableListOf<File>()
        publishSessionExport(first,
            writeCapture = { destination ->
                destination.writeText("capture:first")
                published.writeText(destination.readText())
            },
            finalizeMetadata = { destination ->
                destination.writeText("metadata:first")
                // Deterministic interleaving: the latest slot changes before callback A runs.
                publishSessionExport(second,
                    writeCapture = { newer ->
                        newer.writeText("capture:second")
                        published.writeText(newer.readText())
                    },
                    finalizeMetadata = { it.writeText("metadata:second") },
                    callback = { callbacks += it },
                )
            },
            callback = { capture ->
                assertEquals("capture:second", published.readText())
                assertEquals("capture:first", capture.readText())
                assertEquals("metadata:first", File(capture.parentFile, "session.json").readText())
                callbacks += capture
            },
        )
        assertEquals(listOf(File(second, "capture.json"), File(first, "capture.json")), callbacks)
    }

    @Test fun captureFailureSkipsMetadataAndCallback() = withExportRoot { root ->
        val failure = IOException("synthetic capture failure")
        var captureWrites = 0
        var metadataWrites = 0
        var callbacks = 0
        try {
            publishSessionExport(root,
                writeCapture = { destination ->
                    captureWrites += 1
                    destination.writeText("partial capture")
                    throw failure
                },
                finalizeMetadata = { metadataWrites += 1 },
                callback = { callbacks += 1 },
            )
            fail("Expected the original capture failure")
        } catch (error: IOException) { assertSame(failure, error) }
        assertEquals(1, captureWrites)
        assertEquals(0, metadataWrites)
        assertEquals(0, callbacks)
    }

    @Test fun metadataFailureSkipsCallback() = withExportRoot { root ->
        val failure = IOException("synthetic metadata failure")
        var captureWrites = 0
        var metadataWrites = 0
        var callbacks = 0
        try {
            publishSessionExport(root,
                writeCapture = { destination ->
                    captureWrites += 1
                    destination.writeText("capture")
                },
                finalizeMetadata = { destination ->
                    metadataWrites += 1
                    assertTrue(File(destination.parentFile, "capture.json").isFile)
                    destination.writeText("incomplete metadata")
                    throw failure
                },
                callback = { callbacks += 1 },
            )
            fail("Expected the original metadata failure")
        } catch (error: IOException) { assertSame(failure, error) }
        assertEquals(1, captureWrites)
        assertEquals(1, metadataWrites)
        assertEquals(0, callbacks)
    }

    @Test fun callbackFailurePropagatesOnceAfterBothWriters() = withExportRoot { root ->
        val failure = IllegalStateException("synthetic callback failure")
        val order = mutableListOf<String>()
        try {
            publishSessionExport(root,
                writeCapture = { order += "capture" },
                finalizeMetadata = { order += "metadata" },
                callback = { order += "callback"; throw failure },
            )
            fail("Expected the original callback failure")
        } catch (error: IllegalStateException) { assertSame(failure, error) }
        assertEquals(listOf("capture", "metadata", "callback"), order)
    }
}

/** Tests the actual file publication helper; no Android Context, JSON or recorder is used. */
class ReproAtomicWriteTest {
    private fun withPublicationRoot(test: (File) -> Unit) {
        val root = Files.createTempDirectory("repro-atomic-write-").toFile()
        try { test(root) } finally { root.deleteRecursively() }
    }

    @Test fun defaultWriterCreatesAndReplacesEachRecorderDestination() = withPublicationRoot { root ->
        for (name in listOf("capture.json", "current-session", "session.json")) {
            val destination = File(root, name)
            val first = byteArrayOf(0, 1, 127, -1)
            atomicWriteFile(destination, first)
            assertArrayEquals(first, destination.readBytes())
            atomicWriteFile(destination, ByteArray(0))
            assertEquals(0L, destination.length())
        }
        assertEquals(setOf("capture.json", "current-session", "session.json"),
            root.listFiles()!!.map { it.name }.toSet())
    }

    @Test fun overlappingWritersPublishOnlyTheirOwnBytes() = withPublicationRoot { root ->
        val destination = File(root, "capture.json").apply { writeText("previous") }
        lateinit var firstTemporary: File
        lateinit var secondTemporary: File
        atomicWriteFile(destination, "first".toByteArray()) { firstOutput, firstBytes ->
            firstOutput.write(firstBytes)
            firstTemporary = root.listFiles()!!.single { it != destination }
            assertEquals(root, firstTemporary.parentFile)
            // B completes while A's write is still open. No threads or timing assumptions.
            atomicWriteFile(destination, "second".toByteArray()) { secondOutput, secondBytes ->
                secondOutput.write(secondBytes)
                secondTemporary = root.listFiles()!!.single { it != destination && it != firstTemporary }
                assertNotEquals(firstTemporary, secondTemporary)
                assertEquals("first", firstTemporary.readText())
                assertEquals("second", secondTemporary.readText())
                assertEquals("previous", destination.readText())
            }
            assertEquals("second", destination.readText())
            assertFalse(secondTemporary.exists())
            assertEquals("first", firstTemporary.readText())
        }
        // A publishes last, even though B started later. There is no session-order guarantee.
        assertEquals("first", destination.readText())
        assertFalse(firstTemporary.exists())
        assertEquals(listOf(destination), root.listFiles()!!.toList())
    }

    @Test fun partialWriteFailurePreservesDestinationAndUnrelatedTemporary() = withPublicationRoot { root ->
        val destination = File(root, "capture.json").apply { writeText("previous") }
        val unrelated = File(root, "capture.json.tmp").apply { writeText("not ours") }
        val failure = IOException("synthetic partial write failure")
        try {
            atomicWriteFile(destination, "replacement".toByteArray()) { output, bytes ->
                output.write(bytes, 0, 3)
                throw failure
            }
            fail("Expected the original write failure")
        } catch (error: IOException) { assertSame(failure, error) }
        assertEquals("previous", destination.readText())
        assertEquals("not ours", unrelated.readText())
        assertEquals(setOf(destination, unrelated), root.listFiles()!!.toSet())
    }

    @Test fun failedOverlappingWriterCannotRemoveAnotherWritersTemporary() = withPublicationRoot { root ->
        val destination = File(root, "current-session").apply { writeText("previous") }
        val failure = IOException("synthetic overlapping write failure")
        atomicWriteFile(destination, "first".toByteArray()) { firstOutput, firstBytes ->
            firstOutput.write(firstBytes)
            val firstTemporary = root.listFiles()!!.single { it != destination }
            try {
                atomicWriteFile(destination, "second".toByteArray()) { output, bytes ->
                    output.write(bytes)
                    assertEquals(3, root.listFiles()!!.size)
                    throw failure
                }
                fail("Expected the second writer to fail")
            } catch (error: IOException) { assertSame(failure, error) }
            assertEquals("previous", destination.readText())
            assertEquals("first", firstTemporary.readText())
            assertEquals(setOf(destination, firstTemporary), root.listFiles()!!.toSet())
        }
        assertEquals("first", destination.readText())
        assertEquals(listOf(destination), root.listFiles()!!.toList())
    }

    @Test fun syncFailureDoesNotPublishAndCleansOwnedTemporary() = withPublicationRoot { root ->
        val destination = File(root, "capture.json").apply { writeText("previous") }
        try {
            atomicWriteFile(destination, "replacement".toByteArray()) { output, bytes ->
                output.write(bytes)
                // Exercise the helper's actual fd.sync failure, not a replacement sync callback.
                output.close()
            }
            fail("Expected syncing the closed descriptor to fail")
        } catch (_: IOException) { }
        assertEquals("previous", destination.readText())
        assertEquals(listOf(destination), root.listFiles()!!.toList())
    }

    @Test fun renameFailurePreservesDestinationAndCleansOnlyOwnedTemporary() = withPublicationRoot { root ->
        val destination = File(root, "capture.json").apply { check(mkdir()) }
        val existing = File(destination, "keep").apply { writeText("previous") }
        val unrelated = File(root, "capture.json.tmp").apply { writeText("not ours") }
        try {
            // A file cannot replace a nonempty directory: exercise the real renameTo failure.
            atomicWriteFile(destination, "replacement".toByteArray())
            fail("Expected publication to fail")
        } catch (error: IllegalStateException) {
            assertEquals("Could not publish capture.json", error.message)
        }
        assertTrue(destination.isDirectory)
        assertEquals("previous", existing.readText())
        assertEquals("not ours", unrelated.readText())
        assertEquals(setOf(destination, unrelated), root.listFiles()!!.toSet())
    }

    @Test fun temporaryCreationFailureNeverCallsWriterOrChangesParent() = withPublicationRoot { root ->
        val blockedParent = File(root, "not-a-directory").apply { writeText("previous") }
        var called = false
        try {
            atomicWriteFile(File(blockedParent, "capture.json"), "replacement".toByteArray()) { _, _ ->
                called = true
            }
            fail("Expected temporary creation to fail")
        } catch (_: IOException) { }
        assertFalse(called)
        assertEquals("previous", blockedParent.readText())
        assertEquals(listOf(blockedParent), root.listFiles()!!.toList())
    }
}
