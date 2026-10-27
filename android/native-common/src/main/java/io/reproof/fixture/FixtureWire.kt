package io.reproof.fixture

import android.os.Binder
import android.os.IBinder
import android.os.Parcel

/** Closed, versioned IPC. No file names, commands, arbitrary bundles or serialized objects. */
data class FixtureChallenge(
    val attemptId: String, val nonce: String, val sessionId: String,
    val deviceScopeDigest: String, val projectDigest: String, val buildId: String, val applicationId: String,
    val artifactDigest: String, val profileDigest: String, val fixtureId: String,
    val fixtureVersion: Int, val policyDigest: String, val configurationDigest: String,
    val launchConfigurationDigest: String?, val hostIncarnation: String,
    val helperIncarnation: String, val providerIncarnation: String, val ownershipGeneration: Long,
) {
    fun valid(): Boolean = listOf(attemptId, buildId, applicationId, fixtureId).all { it.matches(CONTRACT_ID) } &&
        listOf(sessionId, hostIncarnation, helperIncarnation, providerIncarnation).all { it.matches(ID) } &&
        listOf(nonce, deviceScopeDigest, projectDigest, artifactDigest, profileDigest,
            policyDigest, configurationDigest).all { it.matches(DIGEST) } &&
        (launchConfigurationDigest == null || launchConfigurationDigest.matches(DIGEST)) &&
        fixtureVersion in 1..1000 && ownershipGeneration > 0

    fun write(out: Parcel) {
        require(valid())
        out.writeInt(1)
        listOf(attemptId, nonce, sessionId, deviceScopeDigest, projectDigest, buildId, applicationId,
            artifactDigest, profileDigest, fixtureId).forEach(out::writeString)
        out.writeInt(fixtureVersion)
        listOf(policyDigest, configurationDigest, launchConfigurationDigest, hostIncarnation,
            helperIncarnation, providerIncarnation).forEach(out::writeString)
        out.writeLong(ownershipGeneration)
    }
    companion object {
        val CONTRACT_ID = Regex("[a-z][a-z0-9_]{0,63}")
        val ID = Regex("[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
        val DIGEST = Regex("[0-9a-f]{64}")
        fun read(input: Parcel): FixtureChallenge {
            require(input.dataAvail() <= 8192 && input.readInt() == 1)
            fun string(): String = requireNotNull(input.readString()).also { require(it.length <= 128) }
            val prefix = (1..10).map { string() }
            val version = input.readInt()
            val policy = string(); val configuration = string()
            val launch = input.readString().also { require(it == null || it.length <= 64) }
            return FixtureChallenge(prefix[0], prefix[1], prefix[2], prefix[3], prefix[4], prefix[5],
                prefix[6], prefix[7], prefix[8], prefix[9], version, policy, configuration, launch,
                string(), string(), string(), input.readLong()).also { require(it.valid()) }
        }
    }
}

data class FixtureOperationBinding(val operationId: String, val allocationId: String, val generation: Long,
    val idempotencyKey: String, val payloadDigest: String, val recipeId: String, val operation: String) {
    fun valid() = operationId.matches(FixtureChallenge.CONTRACT_ID) && allocationId.matches(FixtureChallenge.CONTRACT_ID) && generation > 0 &&
        idempotencyKey.matches(FixtureChallenge.DIGEST) && payloadDigest.matches(FixtureChallenge.DIGEST) &&
        recipeId.matches(FixtureChallenge.CONTRACT_ID) && operation in setOf("prepare", "check", "cleanup")
    fun write(out: Parcel) { require(valid()); out.writeString(operationId); out.writeString(allocationId); out.writeLong(generation)
        out.writeString(idempotencyKey); out.writeString(payloadDigest); out.writeString(recipeId); out.writeString(operation) }
    companion object {
        fun read(input: Parcel) = FixtureOperationBinding(requireNotNull(input.readString()), requireNotNull(input.readString()), input.readLong(),
            requireNotNull(input.readString()), requireNotNull(input.readString()), requireNotNull(input.readString()), requireNotNull(input.readString()))
            .also { require(it.valid()) }
    }
}

enum class FixtureOperation { PREPARE, CHECK, CANCEL, CLEANUP }
enum class FixtureTerminal { READY, FAILED, CANCELLED, CLEANED }

/** Authentication runs on the incoming Binder thread, before data decode or dispatch. */
abstract class FixtureEndpoint : Binder() {
    protected abstract fun authenticateCaller(uid: Int): Boolean
    protected abstract fun request(operation: FixtureOperation, challenge: FixtureChallenge, binding: FixtureOperationBinding,
        deadlineMs: Long, callback: IBinder)
    override fun onTransact(code: Int, data: Parcel, reply: Parcel?, flags: Int): Boolean {
        if (code != IBinder.FIRST_CALL_TRANSACTION || !authenticateCaller(getCallingUid())) return false
        data.enforceInterface(DESCRIPTOR)
        require(data.dataAvail() <= 8192)
        val operation = FixtureOperation.values().getOrNull(data.readInt()) ?: return false
        val challenge = FixtureChallenge.read(data)
        val binding = FixtureOperationBinding.read(data)
        val deadline = data.readLong()
        val callback = data.readStrongBinder() ?: return false
        require(data.dataAvail() == 0 && deadline > 0)
        request(operation, challenge, binding, deadline, callback)
        reply?.writeNoException()
        return true
    }
    companion object {
        const val DESCRIPTOR = "io.reproof.fixture.prepare.v1"
        const val SERVICE_CLASS = "io.reproof.sdk.ReproofFixtureService"
        fun send(endpoint: IBinder, operation: FixtureOperation, challenge: FixtureChallenge, binding: FixtureOperationBinding,
            deadlineMs: Long, callback: IBinder): Boolean {
            val data = Parcel.obtain(); val reply = Parcel.obtain()
            return try {
                data.writeInterfaceToken(DESCRIPTOR); data.writeInt(operation.ordinal)
                challenge.write(data); binding.write(data); data.writeLong(deadlineMs); data.writeStrongBinder(callback)
                if (!endpoint.transact(IBinder.FIRST_CALL_TRANSACTION, data, reply, 0)) false
                else { reply.readException(); true }
            } finally { data.recycle(); reply.recycle() }
        }
    }
}

abstract class FixtureCallback : Binder() {
    protected abstract fun authenticateCaller(uid: Int): Boolean
    protected abstract fun terminal(operation: FixtureOperation, challenge: FixtureChallenge, binding: FixtureOperationBinding, state: FixtureTerminal)
    override fun onTransact(code: Int, data: Parcel, reply: Parcel?, flags: Int): Boolean {
        if (code != IBinder.FIRST_CALL_TRANSACTION || !authenticateCaller(getCallingUid())) return false
        data.enforceInterface(DESCRIPTOR)
        require(data.dataAvail() <= 8192)
        val operation = FixtureOperation.values().getOrNull(data.readInt()) ?: return false
        val challenge = FixtureChallenge.read(data)
        val binding = FixtureOperationBinding.read(data)
        val state = FixtureTerminal.values().getOrNull(data.readInt()) ?: return false
        require(data.dataAvail() == 0)
        terminal(operation, challenge, binding, state)
        reply?.writeNoException()
        return true
    }
    companion object {
        const val DESCRIPTOR = "io.reproof.fixture.terminal.v1"
        fun send(callback: IBinder, operation: FixtureOperation, challenge: FixtureChallenge, binding: FixtureOperationBinding, state: FixtureTerminal) {
            val data = Parcel.obtain(); val reply = Parcel.obtain()
            try {
                data.writeInterfaceToken(DESCRIPTOR); data.writeInt(operation.ordinal)
                challenge.write(data); binding.write(data); data.writeInt(state.ordinal)
                check(callback.transact(IBinder.FIRST_CALL_TRANSACTION, data, reply, 0)); reply.readException()
            } finally { data.recycle(); reply.recycle() }
        }
    }
}
