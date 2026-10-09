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

/**
 * Cursor. On the PC the overlay reads the Cursor app's own token; a phone has no Cursor
 * app, so it signs in the way the Cursor app does: open cursor.com/loginDeepControl with a
 * PKCE challenge, then poll api2.cursor.sh until the browser login completes.
 */
object CursorApi {
    private const val LOGIN_URL = "https://cursor.com/loginDeepControl"
    private const val POLL_URL  = "https://api2.cursor.sh/auth/poll"
    private const val TOKEN_URL = "https://api2.cursor.sh/oauth/token"
    private const val CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"   // the Cursor app's auth client
    const val USAGE_URL = "https://cursor.com/api/dashboard/get-current-period-usage"
    const val ME_URL    = "https://cursor.com/api/auth/me"

    fun loginUrl(uuid: String, verifier: String): String {
        val challenge = Base64.encodeToString(
            MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()),
            Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
        return Uri.parse(LOGIN_URL).buildUpon()
            .appendQueryParameter("challenge", challenge)
            .appendQueryParameter("uuid", uuid)
            .appendQueryParameter("mode", "login")
            .build().toString()
    }

    /** The login result once the user approved in the browser; null while still waiting (404). */
    fun poll(uuid: String, verifier: String): JSONObject? {
        val url = Uri.parse(POLL_URL).buildUpon()
            .appendQueryParameter("uuid", uuid)
            .appendQueryParameter("verifier", verifier)
            .build().toString()
        val r = Api.request("GET", url)
        return r.json()?.takeIf { r.status == 200 && it.optString("accessToken").isNotBlank() }
    }

    /** WorkOS user id from authId ("auth0|user_…") or the JWT's sub claim. */
    fun userId(token: String, authId: String = ""): String {
        if (authId.isNotBlank()) return authId.substringAfterLast("|")
        return try {
            val seg = token.split(".")[1]
            val json = JSONObject(String(Base64.decode(seg, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)))
            json.optString("sub").substringAfterLast("|")
        } catch (e: Exception) { "" }
    }

    private fun cookie(userId: String, token: String) =
        "WorkosCursorSessionToken=" + Uri.encode(userId) + "%3A%3A" + Uri.encode(token)

    private fun headers(userId: String, token: String) = mapOf(
        "Cookie" to cookie(userId, token),
        // cursor.com rejects state-changing requests without a matching origin.
        "Origin" to "https://cursor.com",
        "Referer" to "https://cursor.com/dashboard?tab=usage",
    )

    fun usage(userId: String, token: String) =
        Api.request("POST", USAGE_URL, headers(userId, token), json = JSONObject())

    fun email(userId: String, token: String): String? =
        Api.request("GET", ME_URL, headers(userId, token))
            .takeIf { it.status == 200 }?.json()?.optString("email")?.takeIf { it.isNotBlank() }

    /** New access token, or null when the refresh token is no longer accepted. */
    fun refresh(refreshToken: String): JSONObject? {
        if (refreshToken.isBlank()) return null
        val r = Api.request("POST", TOKEN_URL, json = JSONObject()
            .put("grant_type", "refresh_token")
            .put("client_id", CLIENT_ID)
            .put("refresh_token", refreshToken))
        return r.json()?.takeIf { r.status == 200 && it.optString("access_token").isNotBlank() }
    }
}
