package io.reproloop.sample

/**
 * Deliberately isolated fixture logic. The host repair agent may edit this file
 * and then rebuild the buggy variant without touching capture or driver code.
 */
object CounterLogic {
    fun increment(): Int = if (BuildConfig.BUGGY) 2 else 1
}
