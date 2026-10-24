package io.reproof.autotrace

/** Inert test-only shape of the host-generated config. Runtime hooks are compiled, never invoked. */
internal object ReproConfig {
    const val PROFILE_JSON = "{}"
    const val PROFILE_DIGEST = "0000000000000000000000000000000000000000000000000000000000000000"
    const val SITES_JSON = "[]"
    const val APP_LOGS_ENABLED = false
    const val SCREEN_TARGETS_JSON = "{}"
}
