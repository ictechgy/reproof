package io.reproof.sdk

import io.reproof.fixture.FixtureChallenge
import io.reproof.fixture.FixtureOperationBinding
import io.reproof.fixture.FixtureIdentity
import org.junit.Assert.*
import org.junit.Test

/** JVM-only contract tests. These do not qualify Android Binder origin or device behavior. */
class ReproofFixtureContractTest {
    private fun challenge() = FixtureChallenge(
        "attempt_1", "a".repeat(64), "session_1", "b".repeat(64), "c".repeat(64), "build_1", "app_1",
        "d".repeat(64), "e".repeat(64), "fixture_1", 1, "f".repeat(64), "1".repeat(64), null,
        "host_1", "helper_1", "provider_1", 1,
    )
    @Test fun knownPublicAospSignerCannotBeGrantedHelperPrivilege() {
        assertFalse(FixtureIdentity.permittedSignerDigest("a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc"))
        assertFalse(FixtureIdentity.permittedSignerDigest("not-a-digest"))
        // A syntactically permitted pin still requires runtime current-certificate/UID verification.
        assertTrue(FixtureIdentity.permittedSignerDigest("a".repeat(64)))
    }
    @Test fun closedChallengeRequiresBoundedIdentifiersAndDigests() {
        val c = challenge()
        assertTrue(c.valid())
        assertFalse(c.copy(nonce = "a".repeat(63)).valid())
        assertFalse(c.copy(fixtureId = "../file").valid())
        assertFalse(c.copy(applicationId = "app;command").valid())
        assertFalse(c.copy(fixtureVersion = 0).valid())
        assertFalse(c.copy(ownershipGeneration = 0).valid())
        assertFalse(c.copy(profileDigest = "F".repeat(64)).valid())
        assertFalse(c.copy(launchConfigurationDigest = "not-a-digest").valid())
        assertFalse(c.copy(sessionId = "s".repeat(129)).valid())
    }
    @Test fun everyBindingFieldParticipatesInEquality() {
        val c = challenge()
        assertNotEquals(c, c.copy(nonce = "2".repeat(64)))
        assertNotEquals(c, c.copy(buildId = "build_2"))
        assertNotEquals(c, c.copy(profileDigest = "2".repeat(64)))
        assertNotEquals(c, c.copy(fixtureVersion = 2))
        assertNotEquals(c, c.copy(policyDigest = "2".repeat(64)))
        assertNotEquals(c, c.copy(providerIncarnation = "provider_2"))
        assertNotEquals(c, c.copy(ownershipGeneration = 2))
    }
    @Test fun operationFencingHasNoArbitraryOperationOrPayload() {
        val binding = FixtureOperationBinding("operation_1", "allocation_1", 1, "a".repeat(64),
            "b".repeat(64), "seed_state", "prepare")
        assertTrue(binding.valid())
        assertFalse(binding.copy(operation = "execute").valid())
        assertFalse(binding.copy(recipeId = "file:///tmp/receipt").valid())
        assertFalse(binding.copy(generation = 0).valid())
        assertFalse(binding.copy(payloadDigest = "{}").valid())
        assertNotEquals(binding, binding.copy(allocationId = "allocation_2"))
        assertNotEquals(binding, binding.copy(idempotencyKey = "c".repeat(64)))
    }
    @Test fun cancellationAndDeadlineNeverRestoreAnAttempt() {
        var now = 10L
        val attempt = ReproofFixtureAttempt(challenge(), 20, clock = { now })
        assertFalse(attempt.cancelled)
        attempt.ensureActive()
        attempt.cancel()
        assertTrue(attempt.cancelled)
        try { attempt.ensureActive(); fail("cancelled handler admitted") } catch (_: IllegalStateException) {}
        now = 0
        assertTrue(attempt.cancelled)
        val deadline = ReproofFixtureAttempt(challenge(), 20, clock = { now })
        now = 20
        assertTrue(deadline.cancelled)
        try { deadline.ensureActive(); fail("expired handler admitted") } catch (_: IllegalStateException) {}
    }
}
