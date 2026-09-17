package io.reproloop.autotrace

import android.app.Activity
import org.json.JSONArray

/** Debug-only Views observations for the explicitly configured app and source sites. */
object ReproAuto {
    private val sites: Map<String, String> by lazy {
        val result = LinkedHashMap<String, String>()
        val values = JSONArray(ReproConfig.SITES_JSON)
        require(values.length() in 1..64)
        for (index in 0 until values.length()) {
            val site = values.getJSONObject(index)
            val id = site.getString("id")
            val target = site.getString("target")
            require(result.put(id, target) == null)
        }
        result
    }

    @JvmStatic
    fun start(activity: Activity) {
        if (activity.packageName != ReproConfig.APPLICATION_ID) return
        ReproAppLogs.startObservation(activity, ReproConfig.PROFILE_DIGEST, sites.values.toSet())
    }

    @JvmStatic
    fun stop(activity: Activity) {
        // Application lifecycle callbacks publish destruction and background state.
    }

    @JvmStatic
    fun beforeTap(activity: Activity, target: String, siteId: String): Long {
        if (sites[siteId] != target || activity.packageName != ReproConfig.APPLICATION_ID) return 0L
        return ReproAppLogs.tapBegan(activity, target)
    }

    @JvmStatic
    fun threw(token: Long) = ReproAppLogs.tapThrew(token)

    @JvmStatic
    fun afterTap(token: Long) = ReproAppLogs.tapReturned(token)
}
