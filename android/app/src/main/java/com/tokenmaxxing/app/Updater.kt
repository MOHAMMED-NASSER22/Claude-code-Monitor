package com.tokenmaxxing.app

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray

/**
 * Android builds are released on the same GitHub repo as the Windows overlay, tagged
 * `android-vX.Y` and never marked "latest" (so the desktop updater keeps its own track).
 */
object Updater {
    private const val RELEASES = "https://api.github.com/repos/MOHAMMED-NASSER22/Claude-code-Monitor/releases?per_page=30"

    data class Release(val version: String, val url: String)

    private fun parse(v: String): List<Int> =
        Regex("(\\d+)\\.(\\d+)").find(v)?.groupValues?.drop(1)?.map { it.toInt() } ?: listOf(0, 0)

    private fun newer(a: String, b: String): Boolean {
        val (x, y) = parse(a) to parse(b)
        return x[0] > y[0] || (x[0] == y[0] && x[1] > y[1])
    }

    suspend fun check(): Release? = withContext(Dispatchers.IO) {
        val r = Api.request("GET", RELEASES, mapOf("Accept" to "application/vnd.github+json"))
        if (r.status != 200) return@withContext null
        val a = try { JSONArray(r.body) } catch (e: Exception) { return@withContext null }
        for (i in 0 until a.length()) {
            val rel = a.optJSONObject(i) ?: continue
            val tag = rel.optString("tag_name")
            if (!tag.startsWith("android-v") || rel.optBoolean("draft") || rel.optBoolean("prerelease")) continue
            val ver = tag.removePrefix("android-v")
            return@withContext if (newer(ver, BuildConfig.VERSION_NAME))
                Release(ver, rel.optString("html_url")) else null
        }
        null
    }
}
