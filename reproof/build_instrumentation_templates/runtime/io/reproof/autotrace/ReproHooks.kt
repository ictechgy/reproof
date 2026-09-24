package io.reproof.autotrace

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.ApplicationInfo
import android.view.View
import java.util.Collections
import java.util.IdentityHashMap

/**
 * Small debug-only adapter used by build-time bytecode instrumentation.
 *
 * The adapter owns the listener wrapper, while ReproAuto owns recording state.
 * All adapter work is best effort so a recorder/configuration failure cannot
 * alter application callback behaviour.
 */
object ReproHooks {
    private const val RECORD_MODE = ReproConfig.RECORD_MODE
    private const val MAX_CONTEXT_DEPTH = 16

    @JvmStatic
    fun start(activity: Activity) {
        try {
            ReproAuto.start(activity)
        } catch (_: Throwable) {
            // Instrumentation must not make an Activity lifecycle call fail.
        }
    }

    @JvmStatic
    fun stop(activity: Activity) {
        try {
            ReproAuto.stop(activity)
        } catch (_: Throwable) {
            // Instrumentation must not make an Activity lifecycle call fail.
        }
    }

    /** Install the product listener, wrapping it only for a recordable debug Activity. */
    @JvmStatic
    fun install(view: View, listener: View.OnClickListener?, siteId: String) {
        if (listener == null) {
            // Preserve Android's listener-clearing semantics exactly.
            view.setOnClickListener(null)
            return
        }

        val registrationActivity = try {
            activityFrom(view.context)
        } catch (_: Throwable) {
            null
        }
        val installed = if (shouldWrap(registrationActivity)) {
            RecordingListener(listener, siteId)
        } else {
            listener
        }

        // Do not catch this call: a product View implementation may reject the
        // registration, and that original failure must propagate unchanged.
        view.setOnClickListener(installed)
    }

    private fun shouldWrap(activity: Activity?): Boolean {
        // An unavailable registration context may become available on callback;
        // retain the wrapper so valid later context is not silently skipped.
        if (activity == null) return true
        return try {
            val applicationInfo = activity.applicationInfo
            (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0 &&
                activity.intent?.getStringExtra("repro_mode") == RECORD_MODE
        } catch (_: Throwable) {
            true
        }
    }

    private class RecordingListener(
        private val delegate: View.OnClickListener,
        private val siteId: String,
    ) : View.OnClickListener {
        override fun onClick(callbackView: View) {
            val activity = try {
                activityFrom(callbackView.context)
            } catch (_: Throwable) {
                null
            }
            val recordMode = activity?.let {
                try {
                    val applicationInfo = it.applicationInfo
                    (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0 &&
                        it.intent?.getStringExtra("repro_mode") == RECORD_MODE
                } catch (_: Throwable) {
                    null
                }
            }

            var token = 0L
            if (activity != null && recordMode != false) {
                val target = try {
                    callbackView.resources.getResourceEntryName(callbackView.id)
                } catch (_: Throwable) {
                    null
                }
                if (target != null) {
                    token = try {
                        ReproAuto.beforeTap(activity, target, siteId)
                    } catch (_: Throwable) {
                        0L
                    }
                }
            }

            try {
                delegate.onClick(callbackView)
            } catch (failure: Throwable) {
                if (token != 0L) {
                    try {
                        ReproAuto.threw(token)
                    } catch (_: Throwable) {
                        // Preserve the product exception and its identity.
                    }
                }
                throw failure
            } finally {
                if (token != 0L) {
                    try {
                        ReproAuto.afterTap(token)
                    } catch (_: Throwable) {
                        // Recording cleanup is never allowed to mask a callback.
                    }
                }
            }
        }
    }

    /** Resolve only bounded ContextWrapper chains; no reflection or global state. */
    private fun activityFrom(context: Context?): Activity? {
        var current = context
        var depth = 0
        val seen = Collections.newSetFromMap(IdentityHashMap<Context, Boolean>())
        while (current != null && depth++ < MAX_CONTEXT_DEPTH && seen.add(current)) {
            if (current is Activity) return current
            current = (current as? ContextWrapper)?.baseContext
        }
        return null
    }
}
