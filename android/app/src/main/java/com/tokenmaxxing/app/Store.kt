package com.tokenmaxxing.app

import android.content.Context
import org.json.JSONArray

/**
 * Credentials and the last usage snapshot, in the app's private storage
 * (the same trust level as the desktop's credential files).
 */
object Store {
    private const val PREFS = "tm"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    @Synchronized
    fun creds(ctx: Context): List<Cred> {
        val raw = prefs(ctx).getString("creds", null) ?: return emptyList()
        return try {
            val a = JSONArray(raw)
            (0 until a.length()).map { Cred.from(a.getJSONObject(it)) }
        } catch (e: Exception) {
            emptyList()
        }
    }

    @Synchronized
    fun saveCreds(ctx: Context, list: List<Cred>) {
        val a = JSONArray().apply { list.forEach { put(it.toJson()) } }
        // commit(): a rotated refresh token must be on disk before anything else uses it.
        prefs(ctx).edit().putString("creds", a.toString()).commit()
    }

    @Synchronized
    fun updateCred(ctx: Context, id: String, edit: (Cred) -> Cred) {
        saveCreds(ctx, creds(ctx).map { if (it.id == id) edit(it) else it })
    }

    @Synchronized
    fun usage(ctx: Context): List<Usage> {
        val raw = prefs(ctx).getString("usage", null) ?: return emptyList()
        return try {
            val a = JSONArray(raw)
            (0 until a.length()).map { Usage.from(a.getJSONObject(it)) }
        } catch (e: Exception) {
            emptyList()
        }
    }

    @Synchronized
    fun saveUsage(ctx: Context, list: List<Usage>) {
        val a = JSONArray().apply { list.forEach { put(it.toJson()) } }
        prefs(ctx).edit().putString("usage", a.toString()).apply()
    }

    fun lastFetch(ctx: Context): Long = prefs(ctx).getLong("lastFetch", 0)
    fun setLastFetch(ctx: Context, t: Long) = prefs(ctx).edit().putLong("lastFetch", t).apply()

    /** Wall-clock deadline from a 429's Retry-After. */
    fun rateLimitUntil(ctx: Context): Long = prefs(ctx).getLong("rateLimitUntil", 0)
    fun setRateLimitUntil(ctx: Context, t: Long) = prefs(ctx).edit().putLong("rateLimitUntil", t).apply()

    /** Account shown on the small widget sizes; blank = the first account. */
    fun widgetAccount(ctx: Context): String = prefs(ctx).getString("widgetAccount", "") ?: ""
    fun setWidgetAccount(ctx: Context, id: String) = prefs(ctx).edit().putString("widgetAccount", id).apply()

    /** PKCE verifier survives the trip to the browser even if Android kills the app. */
    fun pendingVerifier(ctx: Context): String = prefs(ctx).getString("pkce", "") ?: ""
    fun setPendingVerifier(ctx: Context, v: String) = prefs(ctx).edit().putString("pkce", v).commit()

    fun wasNotified(ctx: Context, key: String) = prefs(ctx).getBoolean("n:$key", false)
    fun markNotified(ctx: Context, key: String) = prefs(ctx).edit().putBoolean("n:$key", true).apply()
}
