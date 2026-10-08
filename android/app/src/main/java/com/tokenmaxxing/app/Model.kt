package com.tokenmaxxing.app

import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.roundToInt

/** A signed-in Claude account. Tokens rotate on every refresh, so this is rewritten often. */
data class Cred(
    val id: String,
    val label: String,
    val accessToken: String,
    val refreshToken: String,
    val expiresAt: Long,            // epoch ms
    val uuid: String = "",
    val plan: String = "",
    val planCheckedAt: Long = 0,
    val notify: Boolean = false,    // notify at 85%
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("label", label)
        .put("accessToken", accessToken).put("refreshToken", refreshToken)
        .put("expiresAt", expiresAt).put("uuid", uuid)
        .put("plan", plan).put("planCheckedAt", planCheckedAt).put("notify", notify)

    companion object {
        fun from(j: JSONObject) = Cred(
            id = j.getString("id"),
            label = j.optString("label"),
            accessToken = j.optString("accessToken"),
            refreshToken = j.optString("refreshToken"),
            expiresAt = j.optLong("expiresAt"),
            uuid = j.optString("uuid"),
            plan = j.optString("plan"),
            planCheckedAt = j.optLong("planCheckedAt"),
            notify = j.optBoolean("notify"),
        )
    }
}

/** One limit window. `resetsAt` is absolute so countdowns stay right between refreshes. */
data class Limit(val label: String, val pct: Int, val resetsAt: Long?) {
    fun toJson(): JSONObject = JSONObject().put("label", label).put("pct", pct)
        .put("resetsAt", resetsAt ?: JSONObject.NULL)

    companion object {
        fun from(j: JSONObject) = Limit(
            j.optString("label"), j.optInt("pct"),
            if (j.isNull("resetsAt")) null else j.optLong("resetsAt"),
        )
    }
}

data class Usage(
    val id: String,
    val label: String,
    val plan: String,
    val ok: Boolean,
    val error: String = "",
    val active: Boolean = false,    // a 5h session is running
    val session: Limit = Limit("SESSION", 0, null),
    val weekly: Limit = Limit("WEEKLY", 0, null),
    val extra: List<Limit> = emptyList(),
    val fetchedAt: Long = 0,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("label", label).put("plan", plan).put("ok", ok)
        .put("error", error).put("active", active)
        .put("session", session.toJson()).put("weekly", weekly.toJson())
        .put("extra", JSONArray().apply { extra.forEach { put(it.toJson()) } })
        .put("fetchedAt", fetchedAt)

    companion object {
        fun from(j: JSONObject) = Usage(
            id = j.getString("id"),
            label = j.optString("label"),
            plan = j.optString("plan"),
            ok = j.optBoolean("ok"),
            error = j.optString("error"),
            active = j.optBoolean("active"),
            session = j.optJSONObject("session")?.let(Limit::from) ?: Limit("SESSION", 0, null),
            weekly = j.optJSONObject("weekly")?.let(Limit::from) ?: Limit("WEEKLY", 0, null),
            extra = j.optJSONArray("extra")?.let { a ->
                (0 until a.length()).map { Limit.from(a.getJSONObject(it)) }
            } ?: emptyList(),
            fetchedAt = j.optLong("fetchedAt"),
        )
    }
}

object Colors {
    const val BG     = 0xFF000000
    const val TRACK  = 0xFF202020
    const val TEXT   = 0xFFFFFFFF
    const val SOFT   = 0xFFB4BFCB
    const val DIM    = 0xFF808080
    const val LINE   = 0xFF1D2229
    const val ACCENT = 0xFF00A8F8
    const val CLAUDE = 0xFFD87450
    const val GREEN  = 0xFF28BC50
    const val YELLOW = 0xFFF8CC00
    const val RED    = 0xFFF83430
    const val TICK   = 0x80FFFFFF     // 50% white: the even-pace marker

    fun forPct(pct: Int): Long = when {
        pct >= 85 -> RED
        pct >= 60 -> YELLOW
        else -> GREEN
    }
}

/** Usage vs an even burn of the window (same rules as the desktop overlay's _pace). */
data class Pace(val expected: Double, val ahead: Double, val delta: String, val word: String, val color: Long) {
    companion object {
        const val SESSION_MIN = 5 * 60
        const val WEEK_MIN = 7 * 24 * 60
        private const val GRACE = 1.0 / 14          // 12h of a week

        fun of(pct: Int, resetsAt: Long?, windowMin: Int, now: Long = System.currentTimeMillis()): Pace? {
            if (resetsAt == null || pct < 0 || pct >= 100) return null
            val resetMin = ((resetsAt - now) / 60_000).toInt()
            if (resetMin <= 0) return null
            val elapsed = windowMin - minOf(resetMin, windowMin)
            if (elapsed < windowMin * GRACE) return null
            val expected = elapsed * 100.0 / windowMin
            val ahead = pct - expected
            val d = ahead.roundToInt().coerceIn(-99, 99)
            val delta = when {
                d > 0 -> "+$d"
                d < 0 -> "$d"
                else -> "0"
            }
            val (word, col) = when {
                ahead <= 0 -> "UNDER" to Colors.GREEN
                ahead <= 5 -> "ON PACE" to Colors.GREEN
                ahead <= 15 -> "FAST" to Colors.YELLOW
                else -> "SLOW" to Colors.RED
            }
            return Pace(expected, ahead, delta, word, col)
        }
    }
}

fun minutesLeft(resetsAt: Long?, now: Long = System.currentTimeMillis()): Int? =
    resetsAt?.let { ((it - now) / 60_000).toInt().coerceAtLeast(0) }

/** "4d18h", "2h36m", "12m" — the desktop's _fmt_short. */
fun fmtShort(minutes: Int?): String {
    if (minutes == null || minutes <= 0) return "--"
    val d = minutes / 1440
    val h = (minutes % 1440) / 60
    val m = minutes % 60
    return when {
        d > 0 -> "${d}d${h}h"
        h > 0 -> "${h}h" + m.toString().padStart(2, '0') + "m"
        else -> "${m}m"
    }
}

fun fmtAgo(ts: Long, now: Long = System.currentTimeMillis()): String {
    if (ts <= 0) return "never"
    val m = ((now - ts) / 60_000).toInt()
    return when {
        m < 1 -> "just now"
        m < 60 -> "${m}m ago"
        else -> "${m / 60}h ago"
    }
}
