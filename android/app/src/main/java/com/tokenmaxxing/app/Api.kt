package com.tokenmaxxing.app

import android.net.Uri
import android.util.Base64
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * Claude OAuth + usage endpoints. Same values and flow as the Windows overlay
 * (windows/claude_monitor_overlay.py), which took them from the Claude Code binary.
 */
object Api {
    const val CLIENT_ID    = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    const val TOKEN_URL    = "https://platform.claude.com/v1/oauth/token"
    const val AUTH_URL     = "https://claude.com/cai/oauth/authorize"
    const val REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
    const val SCOPE        = "user:inference user:profile user:sessions:claude_code " +
                             "user:mcp_servers user:file_upload"
    const val USAGE_URL    = "https://api.anthropic.com/api/oauth/usage"
    const val PROFILE_URL  = "https://api.anthropic.com/api/oauth/profile"
    const val BETA         = "oauth-2025-04-20"

    class Resp(val status: Int, val body: String, val retryAfterS: Long) {
        fun json(): JSONObject? = try { JSONObject(body) } catch (e: Exception) { null }
    }

    fun request(
        method: String, url: String,
        headers: Map<String, String> = emptyMap(),
        json: JSONObject? = null,
        form: Map<String, String>? = null,
    ): Resp = try {
        val c = URL(url).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 20_000
        c.readTimeout = 20_000
        c.setRequestProperty("User-Agent", "TokenMaxxing-Android/${BuildConfig.VERSION_NAME}")
        c.setRequestProperty("Accept", "application/json")
        headers.forEach { (k, v) -> c.setRequestProperty(k, v) }
        val data: ByteArray? = when {
            form != null -> {
                c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
                form.entries.joinToString("&") { enc(it.key) + "=" + enc(it.value) }.toByteArray()
            }
            json != null -> {
                c.setRequestProperty("Content-Type", "application/json")
                json.toString().toByteArray()
            }
            else -> null
        }
        if (data != null) {
            c.doOutput = true
            c.outputStream.use { it.write(data) }
        }
        val status = c.responseCode
        val stream = if (status >= 400) c.errorStream else c.inputStream
        val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
        val ra = c.getHeaderField("Retry-After")?.trim()?.toDoubleOrNull()?.toLong() ?: 0L
        c.disconnect()
        Resp(status, body, ra)
    } catch (e: Exception) {
        Resp(0, e.message ?: e.toString(), 0)
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    private fun b64url(b: ByteArray) =
        Base64.encodeToString(b, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)

    /** randomBytes(32) as base64url, like the binary; used for the PKCE verifier and state. */
    fun randomToken(): String = ByteArray(32).also { SecureRandom().nextBytes(it) }.let(::b64url)

    fun authorizeUrl(verifier: String, state: String): String {
        val challenge = b64url(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()))
        return Uri.parse(AUTH_URL).buildUpon()
            .appendQueryParameter("code", "true")          // show the code instead of redirecting
            .appendQueryParameter("client_id", CLIENT_ID)
            .appendQueryParameter("response_type", "code")
            .appendQueryParameter("redirect_uri", REDIRECT_URI)
            .appendQueryParameter("scope", SCOPE)
            .appendQueryParameter("code_challenge", challenge)
            .appendQueryParameter("code_challenge_method", "S256")
            .appendQueryParameter("state", state)
            .build().toString()
    }

    /** Token endpoint call: JSON first, form-encoded fallback (same as the desktop app). */
    private fun tokenCall(payload: Map<String, String>, what: String): JSONObject {
        var r = request("POST", TOKEN_URL, json = JSONObject(payload))
        if (r.status in setOf(400, 415, 422)) r = request("POST", TOKEN_URL, form = payload)
        val j = r.json()
        if (r.status != 200 || j == null || !j.has("access_token")) {
            val hint = j?.optJSONObject("error")?.optString("message")
                ?: j?.optString("error_description")?.ifBlank { null }
                ?: j?.optString("error")?.ifBlank { null }
                ?: r.body.take(120)
            throw IllegalStateException("$what failed (HTTP ${r.status}): $hint")
        }
        return j
    }

    /** The paste flow hands back "<code>#<state>"; only the code part is exchanged. */
    fun exchange(raw: String, verifier: String): JSONObject {
        val parts = raw.trim().split("#", limit = 2)
        val payload = mutableMapOf(
            "grant_type" to "authorization_code",
            "code" to parts[0],
            "client_id" to CLIENT_ID,
            "redirect_uri" to REDIRECT_URI,
            "code_verifier" to verifier,
        )
        if (parts.size == 2 && parts[1].isNotBlank()) payload["state"] = parts[1]
        return tokenCall(payload, "Sign-in")
    }

    fun refresh(refreshToken: String): JSONObject = tokenCall(
        mapOf(
            "grant_type" to "refresh_token",
            "refresh_token" to refreshToken,
            "client_id" to CLIENT_ID,
        ),
        "Token refresh",
    )

    fun authHeaders(token: String) = mapOf("Authorization" to "Bearer $token", "anthropic-beta" to BETA)
}
