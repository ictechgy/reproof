package io.reproof.live

import android.net.Uri
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest

/** Only operator-selected, profile-bound QA extras. Not a fixture readiness proof. */
internal class ApprovedLaunch private constructor(val digest: String, val shellArguments: String) {
    companion object {
        private val keyPattern = Regex("QA_[A-Z][A-Z0-9_]{0,47}")
        private val secret = Regex("PASSWORD|PASSWD|TOKEN|SECRET|AUTH|CREDENTIAL|COOKIE|SESSION|PRIVATE|API_?KEY|ACCESS_?KEY")
        private val valuePattern = Regex("[A-Za-z0-9 ._:/?&=%+@-]{0,512}")
        private fun jsonString(value: String): String {
            require(value.all { it.code in 0x20..0x7e && it != '\\' && it != '"' })
            return "\"" + value + "\""
        }
        private fun canonical(value: Any): String = when (value) {
            is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(",", "{", "}") { jsonString(it) + ":" + canonical(value.get(it)) }
            is JSONArray -> (0 until value.length()).joinToString(",", "[", "]") { canonical(value.get(it)) }
            is String -> jsonString(value)
            is Boolean -> value.toString()
            is Int -> value.toString()
            is Long -> value.toString()
            else -> throw IllegalArgumentException("invalid_launch")
        }
        private fun exact(value: JSONObject, keys: Set<String>) = value.keys().asSequence().toSet() == keys
        private fun quote(value: String) = "'" + value.replace("'", "'\\''") + "'"
        fun parse(envelope: JSONObject, profileDigest: String?): ApprovedLaunch? {
            return try {
            if (!exact(envelope, setOf("configurationBase64", "configurationDigest"))) return null
            val encoded = envelope.opt("configurationBase64") as? String ?: return null
            val expected = envelope.opt("configurationDigest") as? String ?: return null
            if (encoded.length > 10924 || !expected.matches(Regex("[0-9a-f]{64}"))) return null
            val bytes = Base64.decode(encoded, Base64.NO_WRAP)
            if (bytes.isEmpty() || bytes.size > 8192 || Base64.encodeToString(bytes, Base64.NO_WRAP) != encoded || bytes.any { it.toInt() !in 0..127 }) return null
            val text = String(bytes, Charsets.US_ASCII)
            val actual = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }
            if (actual != expected) return null
            val config = JSONObject(text)
            // Comparing reconstructed canonical bytes rejects duplicates and alternate JSON encodings.
            if (canonical(config) != text || !exact(config, setOf("schemaVersion", "kind", "referenceId", "profileDigest", "extras"))) return null
            if (config.opt("schemaVersion") !is Int || config.getInt("schemaVersion") != 1 ||
                config.opt("kind") != "reproof-approved-android-launch" || config.opt("profileDigest") != profileDigest ||
                (config.opt("referenceId") as? String)?.matches(Regex("[a-z][a-z0-9_-]{0,63}")) != true) return null
            val extras = config.opt("extras") as? JSONArray ?: return null
            if (extras.length() !in 1..16) return null
            val seen = mutableSetOf<String>()
            val arguments = StringBuilder()
            for (index in 0 until extras.length()) {
                val extra = extras.opt(index) as? JSONObject ?: return null
                if (!exact(extra, setOf("key", "type", "value"))) return null
                val key = extra.opt("key") as? String ?: return null
                if (!key.matches(keyPattern) || secret.containsMatchIn(key) || !seen.add(key)) return null
                val value = extra.opt("value")
                val (flag, rendered) = when (extra.opt("type")) {
                    "string" -> {
                        if (value !is String || !value.matches(valuePattern)) return null
                        if (value.contains("://")) {
                            val url = Uri.parse(value)
                            if (url.scheme != "https" || url.host.isNullOrEmpty() || url.userInfo != null ||
                                url.query != null || url.fragment != null || url.port !in listOf(-1, 443) ||
                                '%' in value || ' ' in value) return null
                        }
                        "--es" to value
                    }
                    "boolean" -> { if (value !is Boolean) return null; "--ez" to value.toString() }
                    "integer" -> { if (value !is Int) return null; "--ei" to value.toString() }
                    else -> return null
                }
                arguments.append(" ").append(flag).append(" ").append(quote(key)).append(" ").append(quote(rendered))
            }
            ApprovedLaunch(actual, arguments.toString())
            } catch (_: Exception) { null }
        }
    }
}
