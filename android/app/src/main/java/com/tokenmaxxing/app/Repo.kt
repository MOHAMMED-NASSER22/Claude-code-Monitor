package com.tokenmaxxing.app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.glance.appwidget.updateAll
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.time.Instant
import java.time.OffsetDateTime
import java.util.UUID
import kotlin.math.roundToInt

object Repo {
    private const val MIN_GAP_MS = 60_000L            // usage API budget is ~6 calls / 5 min
    private const val PLAN_TTL_MS = 24 * 3600_000L
    const val NOTIFY_AT = 85
    private const val CHANNEL = "limits"

    // Refresh tokens rotate: only one refresh may run at a time, app and worker alike.
    private val lock = Mutex()

    private val _usage = MutableStateFlow<List<Usage>>(emptyList())
    val usage: StateFlow<List<Usage>> = _usage
    val busy = MutableStateFlow(false)

    fun load(ctx: Context) {
        _usage.value = Store.usage(ctx)
    }

    /** Fetch every account. Throttled unless [force]; never throws. */
    suspend fun refreshAll(ctx: Context, force: Boolean = false): List<Usage> = withContext(Dispatchers.IO) {
        lock.withLock {
            val now = System.currentTimeMillis()
            val cached = Store.usage(ctx)
            val creds = Store.creds(ctx)
            if (creds.isEmpty()) {
                Store.saveUsage(ctx, emptyList())
                _usage.value = emptyList()
                return@withLock emptyList()
            }
            val limited = Store.rateLimitUntil(ctx) > now
            val tooSoon = now - Store.lastFetch(ctx) < MIN_GAP_MS
            if (limited || (!force && tooSoon && cached.size == creds.size)) {
                _usage.value = cached
                return@withLock cached
            }
            busy.value = true
            try {
                val result = creds.map { c ->
                    fetchOne(ctx, c, cached.firstOrNull { it.id == c.id })
                }
                Store.saveUsage(ctx, result)
                Store.setLastFetch(ctx, now)
                _usage.value = result
                notifyIfNeeded(ctx, result)
                result
            } finally {
                busy.value = false
            }
        }.also { UsageWidget().updateAll(ctx) }
    }

    private fun fetchOne(ctx: Context, c0: Cred, prev: Usage?): Usage {
        if (c0.kind == KIND_CURSOR) return fetchCursor(ctx, c0, prev)
        var c = c0
        return try {
            c = ensureToken(ctx, c, force = false)
            var r = Api.request("GET", Api.USAGE_URL, Api.authHeaders(c.accessToken))
            if (r.status == 401 || r.status == 403) {
                c = ensureToken(ctx, c, force = true)
                r = Api.request("GET", Api.USAGE_URL, Api.authHeaders(c.accessToken))
            }
            if (r.status == 429) {
                val wait = if (r.retryAfterS > 0) r.retryAfterS else 300
                Store.setRateLimitUntil(ctx, System.currentTimeMillis() + wait * 1000)
                throw IllegalStateException("Rate limited, retry in ${wait}s")
            }
            val j = r.json()
            if (r.status != 200 || j == null) {
                throw IllegalStateException(
                    if (r.status == 0) "No connection" else "Usage API returned HTTP ${r.status}")
            }
            c = ensurePlan(ctx, c)
            val fh = j.optJSONObject("five_hour")
            val sd = j.optJSONObject("seven_day")
            val sReset = parseTime(fh?.opt("resets_at"))
            Usage(
                id = c.id, label = c.label, plan = c.plan, ok = true,
                active = sReset != null,
                session = Limit("SESSION", pct(fh?.opt("utilization")), sReset),
                weekly = Limit("WEEKLY", pct(sd?.opt("utilization")), parseTime(sd?.opt("resets_at"))),
                extra = extraLimits(j),
                fetchedAt = System.currentTimeMillis(),
            )
        } catch (e: Exception) {
            // Keep the last good numbers visible, flagged with the error.
            (prev ?: Usage(id = c.id, label = c.label, plan = c.plan, ok = false))
                .copy(label = c.label, ok = false, error = e.message ?: "Error")
        }
    }

    /** Cursor's two monthly pools: Auto + Composer (AUTO) and API. */
    private fun fetchCursor(ctx: Context, c0: Cred, prev: Usage?): Usage {
        var c = c0
        return try {
            var r = CursorApi.usage(c.uuid, c.accessToken)
            if (r.status == 401 || r.status == 403) {
                val t = CursorApi.refresh(c.refreshToken)
                    ?: throw IllegalStateException("Cursor sign-in expired. Add the Cursor account again.")
                c = c.copy(accessToken = t.getString("access_token"),
                           refreshToken = t.optString("refresh_token").ifBlank { c.refreshToken })
                Store.updateCred(ctx, c.id) { it.copy(accessToken = c.accessToken, refreshToken = c.refreshToken) }
                r = CursorApi.usage(c.uuid, c.accessToken)
            }
            val j = r.json()
            if (r.status != 200 || j == null) {
                throw IllegalStateException(
                    if (r.status == 0) "No connection" else "Cursor usage returned HTTP ${r.status}")
            }
            val plan = j.optJSONObject("planUsage") ?: JSONObject()
            val end = parseTime(j.opt("billingCycleEnd"))
            val start = parseTime(j.opt("billingCycleStart"))
            val cycle = if (end != null && start != null && end > start) ((end - start) / 60_000).toInt() else 0
            Usage(
                id = c.id, kind = KIND_CURSOR, label = c.label, plan = "", ok = true, active = true,
                session = Limit("AUTO", pct(plan.opt("autoPercentUsed")), end),
                weekly = Limit("API", pct(plan.opt("apiPercentUsed")), end),
                fetchedAt = System.currentTimeMillis(),
                cycleMin = cycle,
            )
        } catch (e: Exception) {
            (prev ?: Usage(id = c.id, kind = KIND_CURSOR, label = c.label, plan = "", ok = false))
                .copy(label = c.label, ok = false, error = e.message ?: "Error")
        }
    }

    /** Save the Cursor login that [CursorApi.poll] returned. Signing in again updates it in place. */
    suspend fun addCursorAccount(ctx: Context, res: JSONObject): Cred = withContext(Dispatchers.IO) {
        val token = res.getString("accessToken")
        val uid = CursorApi.userId(token, res.optString("authId"))
        require(uid.isNotBlank()) { "Cursor didn't return an account id. Try again." }
        Store.setPendingCursor(ctx, "")
        lock.withLock {
            val creds = Store.creds(ctx)
            val existing = creds.firstOrNull { it.kind == KIND_CURSOR && it.uuid == uid }
            val cred = Cred(
                id = existing?.id ?: UUID.randomUUID().toString(),
                kind = KIND_CURSOR,
                label = existing?.label ?: "Cursor",
                accessToken = token,
                refreshToken = res.optString("refreshToken"),
                expiresAt = 0,
                uuid = uid,
                notify = existing?.notify ?: false,
            )
            Store.saveCreds(ctx, if (existing != null) creds.map { if (it.id == cred.id) cred else it }
                                 else creds + cred)
            cred
        }
    }

    /** Refresh the access token when it is close to expiry (or [force]); persists rotation. */
    private fun ensureToken(ctx: Context, c: Cred, force: Boolean): Cred {
        if (!force && c.expiresAt - System.currentTimeMillis() > 5 * 60_000) return c
        val j = Api.refresh(c.refreshToken)
        val updated = c.copy(
            accessToken = j.getString("access_token"),
            refreshToken = j.optString("refresh_token").ifBlank { c.refreshToken },
            expiresAt = System.currentTimeMillis() + j.optLong("expires_in", 3600) * 1000,
        )
        Store.updateCred(ctx, c.id) { updated.copy(label = it.label, notify = it.notify) }
        return updated
    }

    private fun ensurePlan(ctx: Context, c: Cred): Cred {
        if (c.plan.isNotBlank() && System.currentTimeMillis() - c.planCheckedAt < PLAN_TTL_MS) return c
        val prof = Api.request("GET", Api.PROFILE_URL, Api.authHeaders(c.accessToken))
            .takeIf { it.status == 200 }?.json() ?: return c
        val updated = c.copy(plan = planFrom(prof), planCheckedAt = System.currentTimeMillis())
        Store.updateCred(ctx, c.id) { it.copy(plan = updated.plan, planCheckedAt = updated.planCheckedAt) }
        return updated
    }

    /** Finish the paste-the-code sign-in. Re-signing an existing account updates it in place. */
    suspend fun addAccount(ctx: Context, code: String): Cred = withContext(Dispatchers.IO) {
        val verifier = Store.pendingVerifier(ctx)
        require(verifier.isNotBlank()) { "Tap \"Open claude.ai\" first, then paste the code." }
        val tok = Api.exchange(code, verifier)
        Store.setPendingVerifier(ctx, "")
        val access = tok.getString("access_token")
        val prof = Api.request("GET", Api.PROFILE_URL, Api.authHeaders(access))
            .takeIf { it.status == 200 }?.json()
        val acct = prof?.optJSONObject("account")
        val uuid = acct?.optString("uuid").orEmpty()
        val name = listOf("display_name", "full_name", "email_address", "email")
            .firstNotNullOfOrNull { k -> acct?.optString(k)?.takeIf { it.isNotBlank() } }
            ?.substringBefore("@")?.substringBefore(" ") ?: "Claude"
        lock.withLock {
            val creds = Store.creds(ctx)
            val existing = creds.firstOrNull { uuid.isNotBlank() && it.uuid == uuid }
            val cred = Cred(
                id = existing?.id ?: UUID.randomUUID().toString(),
                label = existing?.label ?: name,
                accessToken = access,
                refreshToken = tok.optString("refresh_token"),
                expiresAt = System.currentTimeMillis() + tok.optLong("expires_in", 3600) * 1000,
                uuid = uuid,
                plan = prof?.let(::planFrom) ?: "",
                planCheckedAt = if (prof != null) System.currentTimeMillis() else 0,
                notify = existing?.notify ?: false,
            )
            Store.saveCreds(ctx, if (existing != null) creds.map { if (it.id == cred.id) cred else it }
                                 else creds + cred)
            cred
        }
    }

    suspend fun removeAccount(ctx: Context, id: String) {
        lock.withLock {
            Store.saveCreds(ctx, Store.creds(ctx).filterNot { it.id == id })
            val left = Store.usage(ctx).filterNot { it.id == id }
            Store.saveUsage(ctx, left)
            _usage.value = left
        }
        UsageWidget().updateAll(ctx)
    }

    suspend fun rename(ctx: Context, id: String, label: String) {
        lock.withLock {
            Store.updateCred(ctx, id) { it.copy(label = label) }
            val list = Store.usage(ctx).map { if (it.id == id) it.copy(label = label) else it }
            Store.saveUsage(ctx, list)
            _usage.value = list
        }
        UsageWidget().updateAll(ctx)
    }

    fun setNotify(ctx: Context, id: String, on: Boolean) = Store.updateCred(ctx, id) { it.copy(notify = on) }

    // ── parsing (mirrors the desktop overlay) ────────────────────────────────

    private fun pct(v: Any?): Int =
        ((v as? Number)?.toDouble() ?: v?.toString()?.toDoubleOrNull() ?: 0.0).roundToInt().coerceIn(0, 100)

    private fun parseTime(v: Any?): Long? = when (v) {
        null, JSONObject.NULL -> null
        is Number -> v.toDouble().let { if (it > 1e11) it.toLong() else (it * 1000).toLong() }
        else -> v.toString().let { s ->
            val n = s.toDoubleOrNull()          // Cursor sends epoch ms as a string
            if (n != null) (if (n > 1e11) n.toLong() else (n * 1000).toLong())
            else try { OffsetDateTime.parse(s).toInstant().toEpochMilli() }
            catch (e: Exception) { try { Instant.parse(s).toEpochMilli() } catch (e2: Exception) { null } }
        }
    }

    private fun limitLabel(lim: JSONObject): String = when (val kind = lim.optString("kind")) {
        "session" -> "SESSION"
        "weekly_all" -> "WEEKLY"
        else -> {
            val scope = lim.optJSONObject("scope")
            val names = listOf("model", "surface").mapNotNull {
                scope?.optJSONObject(it)?.optString("display_name")?.trim()?.takeIf { n -> n.isNotEmpty() }
            }
            names.joinToString(" ").ifBlank { kind.replace("_", " ") }.uppercase()
        }
    }

    /** Model-scoped windows beyond session + weekly (e.g. Max's FABLE weekly). */
    private fun extraLimits(j: JSONObject): List<Limit> {
        val limits = j.optJSONArray("limits")
        if (limits != null) {
            return (0 until limits.length()).mapNotNull { i ->
                val lim = limits.optJSONObject(i) ?: return@mapNotNull null
                if (lim.optString("kind") in setOf("session", "weekly_all")) return@mapNotNull null
                Limit(limitLabel(lim).take(12), pct(lim.opt("percent")), parseTime(lim.opt("resets_at")))
            }
        }
        return listOf("seven_day_opus" to "OPUS", "seven_day_sonnet" to "SONNET").mapNotNull { (k, label) ->
            j.optJSONObject(k)?.let { Limit(label, pct(it.opt("utilization")), parseTime(it.opt("resets_at"))) }
        }
    }

    private fun planFrom(prof: JSONObject): String {
        val acct = prof.optJSONObject("account") ?: JSONObject()
        val org = prof.optJSONObject("organization") ?: JSONObject()
        val tier = org.optString("rate_limit_tier").lowercase()
        val otype = org.optString("organization_type").lowercase()
        return when {
            "max" in otype || "max" in tier || acct.optBoolean("has_claude_max") ->
                Regex("(\\d+)x").find(tier)?.let { "MAX${it.groupValues[1]}x" } ?: "MAX"
            "pro" in otype || acct.optBoolean("has_claude_pro") -> "PRO"
            otype.isNotBlank() -> otype.replace("claude_", "").uppercase().take(6)
            else -> "FREE"
        }
    }

    // ── notifications ────────────────────────────────────────────────────────

    fun createChannel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Usage limits", NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = "When an account reaches $NOTIFY_AT% of a limit" })
    }

    private fun notifyIfNeeded(ctx: Context, list: List<Usage>) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) return
        val wanted = Store.creds(ctx).filter { it.notify }.map { it.id }.toSet()
        for (u in list) {
            if (!u.ok || u.id !in wanted) continue
            for (lim in listOf(u.session, u.weekly)) {
                if (lim.pct < NOTIFY_AT || lim.resetsAt == null) continue
                val key = "${u.id}:${lim.label}:${lim.resetsAt / 60_000}"   // once per window
                if (Store.wasNotified(ctx, key)) continue
                Store.markNotified(ctx, key)
                val open = PendingIntent.getActivity(
                    ctx, 0, Intent(ctx, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                val n = NotificationCompat.Builder(ctx, CHANNEL)
                    .setSmallIcon(R.drawable.ic_launcher_fg)
                    .setContentTitle("${u.label}: ${lim.label.lowercase()} at ${lim.pct}%")
                    .setContentText("Resets in ${fmtShort(minutesLeft(lim.resetsAt))}")
                    .setContentIntent(open)
                    .setAutoCancel(true)
                    .build()
                try {
                    NotificationManagerCompat.from(ctx).notify(key.hashCode(), n)
                } catch (e: SecurityException) { /* permission revoked meanwhile */ }
            }
        }
    }
}
