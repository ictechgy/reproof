package io.reproloop.autotrace

import android.app.Activity

/** Release-source implementation: instrumentation calls compile to no-ops. */
object ReproAuto {
    fun start(activity: Activity) = Unit

    fun stop(activity: Activity) = Unit

    fun beforeTap(activity: Activity, target: String, siteId: String): Long = 0L

    fun threw(token: Long) = Unit

    fun afterTap(token: Long) = Unit

    fun export() = false
}
