package io.reproof.autotrace

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Explicit, shell-authorized export entry point for a debug recording. */
class AutoExportReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_EXPORT_CAPTURE) {
            resultCode = RESULT_REJECTED
            return
        }
        // BroadcastReceiver callbacks are delivered on the main thread. The
        // runtime additionally checks this before touching Activity state.
        resultCode = if (ReproAuto.export()) RESULT_ACCEPTED else RESULT_REJECTED
    }

    companion object {
        const val ACTION_EXPORT_CAPTURE = "io.reproof.EXPORT_CAPTURE"
        const val RESULT_ACCEPTED = 0
        const val RESULT_REJECTED = 1
    }
}
