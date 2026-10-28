package io.reproof.live

import java.io.IOException
import java.io.OutputStream

/** Fixed resource limits, not a bound on UiAutomation or Android's own allocations. */
internal object CaptureLimits {
    const val OUTPUT_MAX_AXIS = 8192
    const val OUTPUT_PIXELS = 8_294_400L
    const val RAW_PIXELS = 16_777_216L
    const val FRAME_BYTES = 3 * 1024 * 1024

    fun pixelsAllowed(width: Int, height: Int, limit: Long): Boolean =
        width > 0 && height > 0 && width.toLong() * height.toLong() <= limit

    fun rawAllowed(width: Int, height: Int): Boolean = pixelsAllowed(width, height, RAW_PIXELS)
    fun outputAllowed(width: Int, height: Int): Boolean =
        width <= OUTPUT_MAX_AXIS && height <= OUTPUT_MAX_AXIS && pixelsAllowed(width, height, OUTPUT_PIXELS)
}

internal data class CapturePolicy private constructor(val maxWidth: Int) {
    fun fields(): Map<String, Int> = mapOf("schemaVersion" to 1, "maxWidth" to maxWidth,
        "pixelLimit" to CaptureLimits.OUTPUT_PIXELS.toInt(), "frameBytes" to CaptureLimits.FRAME_BYTES)

    companion object {
        private val widths = setOf(480, 720, 960, 1280, 1440, 1920)
        fun parse(raw: Map<String, Any?>, shared: Boolean, general: Boolean): CapturePolicy? {
            if (!shared || !general || raw.keys != setOf("schemaVersion", "maxWidth", "pixelLimit", "frameBytes")) return null
            if (raw.values.any { it !is Int }) return null
            if (raw["schemaVersion"] != 1 || raw["pixelLimit"] != CaptureLimits.OUTPUT_PIXELS.toInt() ||
                raw["frameBytes"] != CaptureLimits.FRAME_BYTES) return null
            val width = raw["maxWidth"] as Int
            return if (width in widths) CapturePolicy(width) else null
        }
    }
}

/** Fixed-size backing storage: never grow an encoder buffer past the frame cap. */
internal class BoundedJpegOutputStream(private val maximum: Int = CaptureLimits.FRAME_BYTES) : OutputStream() {
    init { require(maximum in 1..CaptureLimits.FRAME_BYTES) }
    private val bytes = ByteArray(maximum)
    private var count = 0
    var failed = false
        private set

    private fun reserve(length: Int) {
        if (failed || length < 0 || length > maximum - count) {
            failed = true
            throw IOException("frame_too_large")
        }
    }

    override fun write(value: Int) {
        reserve(1)
        bytes[count++] = value.toByte()
    }

    override fun write(source: ByteArray, offset: Int, length: Int) {
        if (offset < 0 || length < 0 || offset > source.size || length > source.size - offset) {
            failed = true
            throw IndexOutOfBoundsException("invalid_frame_slice")
        }
        reserve(length)
        source.copyInto(bytes, count, offset, offset + length)
        count += length
    }

    fun toByteArray(): ByteArray {
        if (failed) throw IOException("frame_too_large")
        return bytes.copyOf(count)
    }
}
