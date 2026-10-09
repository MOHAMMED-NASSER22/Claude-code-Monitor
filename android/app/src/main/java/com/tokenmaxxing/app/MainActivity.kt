package com.tokenmaxxing.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.glance.appwidget.updateAll
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.util.UUID
import kotlinx.coroutines.launch

private fun c(v: Long) = Color(v)
private val Mono = FontFamily.Monospace

private sealed interface Screen {
    data object List : Screen
    data class Detail(val id: String) : Screen
    data object SignIn : Screen
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = c(Colors.ACCENT), background = Color.Black)) {
                Surface(color = Color.Black, modifier = Modifier.fillMaxSize()) { AppRoot() }
            }
        }
    }
}

@Composable
private fun AppRoot() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val usage by Repo.usage.collectAsState()
    val busy by Repo.busy.collectAsState()
    var screen by remember {
        mutableStateOf<Screen>(if (Store.creds(ctx).isEmpty()) Screen.SignIn else Screen.List)
    }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    var update by remember { mutableStateOf<Updater.Release?>(null) }

    LaunchedEffect(Unit) { update = Updater.check() }
    // While the app is open: refresh now, then every 2 minutes (the desktop's poll rate).
    LaunchedEffect(Unit) {
        while (true) {
            Repo.refreshAll(ctx)
            repeat(4) { now = System.currentTimeMillis(); delay(30_000) }
        }
    }

    BackHandler(enabled = screen != Screen.List && Store.creds(ctx).isNotEmpty()) { screen = Screen.List }

    Column(Modifier.fillMaxSize().systemBarsPadding().imePadding()) {
        when (val s = screen) {
            Screen.List -> ListScreen(
                usage, busy, now, update,
                onRefresh = { scope.launch { Repo.refreshAll(ctx, force = true) } },
                onOpen = { screen = Screen.Detail(it) },
                onAdd = { screen = Screen.SignIn },
            )
            is Screen.Detail -> {
                val u = usage.firstOrNull { it.id == s.id }
                if (u == null) LaunchedEffect(s.id) { screen = Screen.List }
                else DetailScreen(u, busy, now, onBack = { screen = Screen.List })
            }
            Screen.SignIn -> SignInScreen(
                canGoBack = Store.creds(ctx).isNotEmpty(),
                onBack = { screen = Screen.List },
                onDone = { screen = Screen.List },
            )
        }
    }
}

// ── shared pieces ────────────────────────────────────────────────────────────

@Composable
private fun MonoText(
    text: String, color: Long, size: TextUnit, modifier: Modifier = Modifier,
    align: TextAlign = TextAlign.Start, spacing: TextUnit = 0.08.sp,
) = Text(
    text, modifier = modifier, color = c(color), maxLines = 1, overflow = TextOverflow.Ellipsis,
    style = TextStyle(fontFamily = Mono, fontSize = size, letterSpacing = spacing, textAlign = align),
)

@Composable
private fun Meter(pct: Int, color: Long, expected: Double?, height: Dp, modifier: Modifier = Modifier) {
    Canvas(modifier.height(height)) {
        val r = CornerRadius(size.height * 0.2f)
        drawRoundRect(c(Colors.TRACK), cornerRadius = r)
        val fw = size.width * pct.coerceIn(0, 100) / 100f
        if (fw > 0) drawRoundRect(c(color), size = Size(fw, size.height), cornerRadius = r)
        if (expected != null) {
            val tw = maxOf(2.dp.toPx(), size.height / 4)
            val x = (size.width * expected / 100).toFloat().coerceIn(0f, size.width - tw)
            drawRect(c(Colors.TICK), topLeft = Offset(x, 0f), size = Size(tw, size.height))
        }
    }
}

@Composable
private fun Badge(text: String, color: Long = Colors.DIM) {
    Box(Modifier.border(1.dp, c(color).copy(alpha = 0.7f)).padding(horizontal = 6.dp, vertical = 1.dp)) {
        MonoText(text, color, 12.sp)
    }
}

@Composable
private fun Spark(size: Dp, color: Long = Colors.CLAUDE) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension
        val w = s * 0.11f
        val cx = s / 2
        val ends = listOf(Offset(cx, 0f) to Offset(cx, s), Offset(0f, cx) to Offset(s, cx),
                          Offset(s * .15f, s * .15f) to Offset(s * .85f, s * .85f),
                          Offset(s * .85f, s * .15f) to Offset(s * .15f, s * .85f))
        ends.forEach { (a, b) -> drawLine(c(color), a, b, strokeWidth = w, cap = androidx.compose.ui.graphics.StrokeCap.Round) }
    }
}

/** Cursor's mark: an outlined hexagon (a cube seen corner-on). */
@Composable
private fun Cube(size: Dp, color: Long = Colors.CURSOR) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension
        val w = s * 0.09f
        val pts = listOf(Offset(s / 2, w), Offset(s - w, s * .27f), Offset(s - w, s * .73f),
                         Offset(s / 2, s - w), Offset(w, s * .73f), Offset(w, s * .27f))
        pts.indices.forEach { i -> drawLine(c(color), pts[i], pts[(i + 1) % 6], strokeWidth = w) }
        drawLine(c(color), Offset(w, s * .27f), Offset(s / 2, s / 2), strokeWidth = w)
        drawLine(c(color), Offset(s - w, s * .27f), Offset(s / 2, s / 2), strokeWidth = w)
        drawLine(c(color), Offset(s / 2, s / 2), Offset(s / 2, s - w), strokeWidth = w)
    }
}

@Composable
private fun Mark(u: Usage, size: Dp) = if (u.isCursor) Cube(size) else Spark(size)
private fun brand(u: Usage) = if (u.isCursor) Colors.CURSOR else Colors.CLAUDE

@Composable
private fun Header(title: String, onBack: (() -> Unit)?, trailing: @Composable RowScope.() -> Unit = {}) {
    Column {
        Row(Modifier.fillMaxWidth().height(56.dp).padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            if (onBack != null) {
                TextButton(onClick = onBack, modifier = Modifier.size(48.dp),
                           contentPadding = PaddingValues(0.dp)) { MonoText("<", Colors.TEXT, 20.sp) }
            } else Spacer(Modifier.width(8.dp))
            MonoText(title, Colors.ACCENT, 17.sp, Modifier.weight(1f), spacing = 3.sp)
            trailing()
        }
        Box(Modifier.fillMaxWidth().height(2.dp).background(c(Colors.ACCENT)))
    }
}

// ── list ─────────────────────────────────────────────────────────────────────

@Composable
private fun ColumnScope.ListScreen(
    usage: List<Usage>, busy: Boolean, now: Long, update: Updater.Release?,
    onRefresh: () -> Unit, onOpen: (String) -> Unit, onAdd: () -> Unit,
) {
    val ctx = LocalContext.current
    Header("TOKEN MAXXING", null) {
        TextButton(onClick = onRefresh, enabled = !busy) {
            MonoText(if (busy) "…" else "REFRESH", if (busy) Colors.DIM else Colors.ACCENT, 13.sp)
        }
    }
    if (update != null) {
        Row(Modifier.fillMaxWidth().background(c(0xFF0B2A3A))
                .clickable { ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(update.url))) }
                .padding(horizontal = 16.dp, vertical = 12.dp)) {
            Text("Version ${update.version} is out. Tap to download.", color = c(Colors.TEXT), fontSize = 14.sp)
        }
    }
    Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 16.dp)) {
        if (usage.isEmpty()) {
            Text("Loading your accounts…", color = c(Colors.DIM), modifier = Modifier.padding(vertical = 24.dp))
        }
        usage.forEach { u ->
            Column(Modifier.fillMaxWidth().clickable { onOpen(u.id) }.padding(vertical = 14.dp),
                   verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Mark(u, 16.dp)
                    Spacer(Modifier.width(8.dp))
                    MonoText(u.label, brand(u), 17.sp, Modifier.weight(1f))
                    if (u.plan.isNotBlank()) Badge(u.plan)
                }
                if (!u.ok) MonoText(u.error, Colors.RED, 12.sp)
                ListRow(u.session.label, u.sessionShown, u.session.resetsAt, u.sessionPace(now), u.ok, now)
                ListRow(u.weekly.label, u.weekly.pct, u.weekly.resetsAt, u.weeklyPace(now), u.ok, now)
                u.extra.forEach { ListRow(it.label, it.pct, it.resetsAt, null, u.ok, now) }
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(c(Colors.LINE)))
        }
        OutlinedButton(onClick = onAdd, modifier = Modifier.fillMaxWidth().padding(top = 18.dp).height(48.dp),
                       shape = RoundedCornerShape(10.dp), border = BorderStroke(1.dp, c(0xFF3A434E))) {
            Text("+  Add account", color = c(Colors.ACCENT), fontSize = 15.sp)
        }
        Spacer(Modifier.height(16.dp))
    }
    Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("Updated ${fmtAgo(usage.maxOfOrNull { it.fetchedAt } ?: 0, now)} · widgets refresh about every 15 min",
             color = c(Colors.DIM), fontSize = 13.sp)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.width(2.dp).height(12.dp).background(c(Colors.TICK)))
            Spacer(Modifier.width(8.dp))
            Text("Faded line = where an even pace would be now", color = c(Colors.DIM), fontSize = 13.sp)
        }
    }
}

@Composable
private fun ListRow(label: String, pct: Int, resetsAt: Long?, pace: Pace?, ok: Boolean, now: Long) {
    val idle = pct < 0
    val col = if (!ok || idle) Colors.DIM else Colors.forPct(pct)
    Row(Modifier.fillMaxWidth().height(26.dp), verticalAlignment = Alignment.CenterVertically) {
        MonoText(label, Colors.DIM, 14.sp, Modifier.width(80.dp))
        MonoText(if (idle) "--" else "$pct%", col, 14.sp, Modifier.width(44.dp), TextAlign.End)
        Spacer(Modifier.width(8.dp))
        Meter(if (idle) 0 else pct, col, pace?.expected, 7.dp, Modifier.weight(1f))
        MonoText(pace?.delta ?: "", pace?.color ?: Colors.DIM, 14.sp, Modifier.width(40.dp), TextAlign.End)
        MonoText(if (idle) "idle" else fmtShort(minutesLeft(resetsAt, now)), if (idle) Colors.DIM else Colors.TEXT,
                 14.sp, Modifier.width(62.dp), TextAlign.End)
    }
}

// ── detail ───────────────────────────────────────────────────────────────────

/** "At this rate it runs out about 2 days early" — or the room left when under pace. */
private fun paceSentence(pct: Int, resetsAt: Long?, windowMin: Int, pace: Pace?, now: Long): String? {
    pace ?: return null
    val left = minutesLeft(resetsAt, now) ?: return null
    val exp = pace.expected.toInt()
    if (pace.ahead <= 0) return "An even pace would be $exp% by now. You have room to spare."
    val elapsed = windowMin - minOf(left, windowMin)
    if (elapsed <= 0 || pct <= 0) return null
    val toFull = (100 - pct) * elapsed / pct.toDouble()          // minutes until 100% at this rate
    val early = (left - toFull).toInt()
    return if (early <= 0) "An even pace would be $exp% by now. You should still make it to the reset."
           else "An even pace would be $exp% by now. At this rate it runs out ${fmtLong(early)} early."
}

private fun fmtLong(min: Int): String = when {
    min >= 1440 -> (min / 1440.0).let { d -> if (d < 1.5) "about a day" else "about ${Math.round(d)} days" }
    min >= 60 -> (min / 60.0).let { h -> if (h < 1.5) "about an hour" else "about ${Math.round(h)} hours" }
    else -> "about $min min"
}

@Composable
private fun ColumnScope.DetailScreen(u: Usage, busy: Boolean, now: Long, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val cred = remember(u.id) { Store.creds(ctx).firstOrNull { it.id == u.id } }
    var notify by remember(u.id) { mutableStateOf(cred?.notify == true) }
    var onWidget by remember(u.id) { mutableStateOf(Store.widgetAccount(ctx).let { it == u.id || it.isBlank() }) }
    var confirmRemove by remember { mutableStateOf(false) }
    var renaming by remember { mutableStateOf(false) }
    var name by remember(u.id) { mutableStateOf(u.label) }

    val askPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        notify = granted
        Repo.setNotify(ctx, u.id, granted)
    }

    Header(u.label, onBack) { if (u.plan.isNotBlank()) { Badge(u.plan); Spacer(Modifier.width(12.dp)) } }

    Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),
           verticalArrangement = Arrangement.spacedBy(22.dp)) {
        if (!u.ok) MonoText(u.error, Colors.RED, 13.sp)
        val sp = u.sessionPace(now)
        val sWindow = if (u.isCursor) (if (u.cycleMin > 0) u.cycleMin else Pace.MONTH_MIN) else Pace.SESSION_MIN
        BigCard(u.session.label, u.sessionBadge, u.sessionShown, u.session.resetsAt, sp,
                paceSentence(u.session.pct, u.session.resetsAt, sWindow, sp, now), u.ok, now, brand(u))
        Box(Modifier.fillMaxWidth().height(1.dp).background(c(Colors.LINE)))
        val wp = u.weeklyPace(now)
        BigCard(u.weekly.label, u.weeklyBadge, u.weekly.pct, u.weekly.resetsAt, wp,
                paceSentence(u.weekly.pct, u.weekly.resetsAt, Pace.WEEK_MIN, wp, now), u.ok, now, brand(u))
        if (u.extra.isNotEmpty()) {
            Box(Modifier.fillMaxWidth().height(1.dp).background(c(Colors.LINE)))
            u.extra.forEach { ListRow(it.label, it.pct, it.resetsAt, null, u.ok, now) }
        }

        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            SettingRow("Notify at ${Repo.NOTIFY_AT}%", notify) { on ->
                if (on && Build.VERSION.SDK_INT >= 33 &&
                    ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                    askPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                } else {
                    notify = on
                    Repo.setNotify(ctx, u.id, on)
                }
            }
            SettingRow("Show on small widgets", onWidget) { on ->
                onWidget = on
                Store.setWidgetAccount(ctx, if (on) u.id else "")
                scope.launch { UsageWidget().updateAll(ctx) }
            }
        }

        if (renaming) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(name, { name = it.take(24) }, singleLine = true, label = { Text("Name") },
                                  modifier = Modifier.weight(1f))
                Button(onClick = {
                    renaming = false
                    scope.launch { Repo.rename(ctx, u.id, name.trim().ifBlank { u.label }) }
                }) { Text("Save") }
            }
        }
    }

    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
           verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedButton(onClick = { scope.launch { Repo.refreshAll(ctx, force = true) } }, enabled = !busy,
                           modifier = Modifier.weight(1f).height(48.dp), shape = RoundedCornerShape(10.dp)) {
                Text(if (busy) "Refreshing…" else "Refresh now", color = c(Colors.TEXT))
            }
            OutlinedButton(onClick = { renaming = !renaming }, modifier = Modifier.weight(1f).height(48.dp),
                           shape = RoundedCornerShape(10.dp)) { Text("Rename", color = c(Colors.TEXT)) }
        }
        if (confirmRemove) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Remove ${u.label} from this phone?", color = c(Colors.TEXT), modifier = Modifier.weight(1f))
                TextButton(onClick = { confirmRemove = false }) { Text("Cancel") }
                TextButton(onClick = { scope.launch { Repo.removeAccount(ctx, u.id); onBack() } }) {
                    Text("Remove", color = c(Colors.RED))
                }
            }
        } else {
            TextButton(onClick = { confirmRemove = true }, modifier = Modifier.fillMaxWidth()) {
                Text("Remove account", color = c(Colors.RED))
            }
        }
    }
}

@Composable
private fun SettingRow(label: String, on: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().height(52.dp).clickable { onChange(!on) },
        verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = c(Colors.TEXT), fontSize = 15.sp, modifier = Modifier.weight(1f))
        Switch(checked = on, onCheckedChange = onChange)
    }
}

@Composable
private fun BigCard(label: String, badge: String, pct: Int, resetsAt: Long?, pace: Pace?,
                    sentence: String?, ok: Boolean, now: Long, labelCol: Long = Colors.CLAUDE) {
    val idle = pct < 0
    val col = if (!ok || idle) Colors.DIM else Colors.forPct(pct)
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            MonoText(label, labelCol, 15.sp)
            Badge(badge)
            if (pace != null) Badge("${pace.delta} ${pace.word}", pace.color)
        }
        Row(verticalAlignment = Alignment.Bottom) {
            Text(if (idle) "--" else "$pct%", color = c(col), modifier = Modifier.weight(1f),
                 style = TextStyle(fontFamily = Mono, fontSize = 54.sp, fontWeight = FontWeight.Normal))
            MonoText(if (idle) "idle" else "resets in ${fmtShort(minutesLeft(resetsAt, now))}", Colors.SOFT, 15.sp,
                     Modifier.padding(bottom = 10.dp))
        }
        Meter(if (idle) 0 else pct, col, pace?.expected, 12.dp, Modifier.fillMaxWidth())
        if (sentence != null) Text(sentence, color = c(Colors.DIM), fontSize = 13.sp)
    }
}

// ── sign-in ──────────────────────────────────────────────────────────────────

@Composable
private fun ColumnScope.SignInScreen(canGoBack: Boolean, onBack: () -> Unit, onDone: () -> Unit) {
    val ctx = LocalContext.current
    var provider by remember {
        mutableStateOf(if (Store.pendingCursor(ctx).isNotBlank()) KIND_CURSOR else KIND_CLAUDE)
    }
    Header("ADD ACCOUNT", if (canGoBack) onBack else null)
    Row(Modifier.fillMaxWidth().padding(start = 24.dp, end = 24.dp, top = 18.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        ProviderTab("Claude", provider == KIND_CLAUDE, Colors.CLAUDE, Modifier.weight(1f)) { provider = KIND_CLAUDE }
        ProviderTab("Cursor", provider == KIND_CURSOR, Colors.CURSOR, Modifier.weight(1f)) { provider = KIND_CURSOR }
    }
    if (provider == KIND_CLAUDE) ClaudeSignIn(onDone) else CursorSignIn(onDone)
}

@Composable
private fun ProviderTab(label: String, selected: Boolean, color: Long, modifier: Modifier, onClick: () -> Unit) {
    OutlinedButton(
        onClick = onClick, modifier = modifier.height(48.dp), shape = RoundedCornerShape(10.dp),
        border = BorderStroke(if (selected) 2.dp else 1.dp, c(if (selected) color else 0xFF3A434E)),
        colors = ButtonDefaults.outlinedButtonColors(containerColor = if (selected) c(0xFF14181D) else Color.Transparent),
    ) {
        if (label == "Cursor") Cube(16.dp) else Spark(16.dp)
        Spacer(Modifier.width(8.dp))
        Text(label, color = c(if (selected) Colors.TEXT else Colors.DIM), fontSize = 15.sp)
    }
}

@Composable
private fun ColumnScope.ClaudeSignIn(onDone: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    var code by remember { mutableStateOf("") }
    var opened by remember { mutableStateOf(Store.pendingVerifier(ctx).isNotBlank()) }
    var working by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 24.dp, vertical = 12.dp),
           verticalArrangement = Arrangement.spacedBy(24.dp)) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Spark(40.dp)
            Text("Add a Claude account", color = c(Colors.TEXT), fontSize = 26.sp, fontWeight = FontWeight.SemiBold)
            Text("Sign in once and the app keeps your usage up to date, including on the home-screen widgets.",
                 color = c(Colors.SOFT), fontSize = 15.sp)
        }
        Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Step("1", "Tap Open claude.ai. Your browser opens the Claude sign-in page.")
            Step("2", "Check it's the right account, then tap Authorize.")
            Step("3", "Copy the code it shows, come back here and paste it below.")
        }
        if (opened) {
            OutlinedTextField(
                value = code, onValueChange = { code = it.trim(); error = null },
                label = { Text("Code from claude.ai") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii),
                modifier = Modifier.fillMaxWidth(),
                trailingIcon = {
                    TextButton(onClick = { clipboard.getText()?.text?.let { code = it.trim() } }) { Text("Paste") }
                },
            )
        }
        error?.let { Text(it, color = c(Colors.RED), fontSize = 14.sp) }
        Row(Modifier.border(1.dp, c(Colors.LINE), RoundedCornerShape(10.dp)).padding(12.dp)) {
            Text("The phone gets its own sign-in, so it won't log out Token Maxxing on your PC. " +
                 "To add a second account, switch accounts on claude.ai in the browser first.",
                 color = c(Colors.DIM), fontSize = 13.sp)
        }
    }
    Column(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 16.dp),
           verticalArrangement = Arrangement.spacedBy(10.dp)) {
        if (opened && code.isNotBlank()) {
            Button(
                onClick = {
                    working = true; error = null
                    scope.launch {
                        try {
                            Repo.addAccount(ctx, code)
                            Repo.refreshAll(ctx, force = true)
                            onDone()
                        } catch (e: Exception) {
                            error = e.message ?: "Sign-in failed"
                        } finally { working = false }
                    }
                },
                enabled = !working,
                modifier = Modifier.fillMaxWidth().height(52.dp), shape = RoundedCornerShape(12.dp),
                colors = ButtonDefaults.buttonColors(containerColor = c(Colors.CLAUDE), contentColor = c(0xFF140C08)),
            ) { Text(if (working) "Signing in…" else "Add account", fontSize = 16.sp, fontWeight = FontWeight.SemiBold) }
        }
        val primary = !opened || code.isBlank()
        val openClaude = {
            val verifier = Api.randomToken()
            Store.setPendingVerifier(ctx, verifier)
            ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(Api.authorizeUrl(verifier, Api.randomToken()))))
            opened = true
        }
        if (primary) {
            Button(onClick = openClaude, modifier = Modifier.fillMaxWidth().height(52.dp),
                   shape = RoundedCornerShape(12.dp),
                   colors = ButtonDefaults.buttonColors(containerColor = c(Colors.CLAUDE), contentColor = c(0xFF140C08))) {
                Text(if (opened) "Open claude.ai again" else "Open claude.ai", fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            }
        } else {
            OutlinedButton(onClick = openClaude, modifier = Modifier.fillMaxWidth().height(48.dp),
                           shape = RoundedCornerShape(12.dp)) { Text("Open claude.ai again", color = c(Colors.SOFT)) }
        }
    }
}


/**
 * Cursor works like the Cursor app's own login: cursor.com asks you to confirm, and the
 * app polls until it gets the token. Nothing to copy or paste.
 */
@Composable
private fun ColumnScope.CursorSignIn(onDone: () -> Unit) {
    val ctx = LocalContext.current
    var pending by remember { mutableStateOf(Store.pendingCursor(ctx)) }
    var status by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(pending) {
        if (pending.isBlank()) return@LaunchedEffect
        val uuid = pending.substringBefore("|")
        val verifier = pending.substringAfter("|")
        status = "Waiting for you to confirm in the browser…"
        val deadline = System.currentTimeMillis() + 10 * 60_000
        while (System.currentTimeMillis() < deadline) {
            val res = withContext(Dispatchers.IO) { CursorApi.poll(uuid, verifier) }
            if (res != null) {
                status = "Signed in. Loading your usage…"
                try {
                    Repo.addCursorAccount(ctx, res)
                    Repo.refreshAll(ctx, force = true)
                    onDone()
                } catch (e: Exception) {
                    status = ""
                    error = e.message ?: "Cursor sign-in failed"
                }
                return@LaunchedEffect
            }
            delay(2_000)
        }
        Store.setPendingCursor(ctx, "")
        pending = ""
        status = ""
        error = "The Cursor sign-in timed out. Tap Open cursor.com to try again."
    }

    Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 24.dp, vertical = 12.dp),
           verticalArrangement = Arrangement.spacedBy(24.dp)) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Cube(40.dp)
            Text("Add your Cursor account", color = c(Colors.TEXT), fontSize = 26.sp, fontWeight = FontWeight.SemiBold)
            Text("Shows your Auto + Composer and API usage for this billing month, with the same pace line.",
                 color = c(Colors.SOFT), fontSize = 15.sp)
        }
        Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Step("1", "Tap Open cursor.com. Sign in there if it asks (Google, GitHub or email all work).")
            Step("2", "Confirm the login on the Cursor page.")
            Step("3", "Come back here. The account appears by itself, no code needed.")
        }
        if (status.isNotBlank()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = c(Colors.ACCENT))
                Spacer(Modifier.width(10.dp))
                Text(status, color = c(Colors.TEXT), fontSize = 14.sp)
            }
        }
        error?.let { Text(it, color = c(Colors.RED), fontSize = 14.sp) }
        Row(Modifier.border(1.dp, c(Colors.LINE), RoundedCornerShape(10.dp)).padding(12.dp)) {
            Text("This is a separate login for the phone. It doesn't sign you out of Cursor on your PC.",
                 color = c(Colors.DIM), fontSize = 13.sp)
        }
    }
    Column(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 16.dp)) {
        Button(
            onClick = {
                val p = UUID.randomUUID().toString() + "|" + Api.randomToken()
                Store.setPendingCursor(ctx, p)
                error = null
                pending = p
                ctx.startActivity(Intent(Intent.ACTION_VIEW,
                    Uri.parse(CursorApi.loginUrl(p.substringBefore("|"), p.substringAfter("|")))))
            },
            modifier = Modifier.fillMaxWidth().height(52.dp), shape = RoundedCornerShape(12.dp),
            colors = ButtonDefaults.buttonColors(containerColor = c(Colors.CURSOR), contentColor = Color.Black),
        ) {
            Text(if (pending.isBlank()) "Open cursor.com" else "Open cursor.com again",
                 fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
private fun Step(n: String, text: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Box(Modifier.size(28.dp).border(1.dp, c(Colors.ACCENT), RoundedCornerShape(14.dp)),
            contentAlignment = Alignment.Center) { MonoText(n, Colors.ACCENT, 13.sp) }
        Text(text, color = c(Colors.TEXT), fontSize = 15.sp, modifier = Modifier.padding(top = 3.dp))
    }
}
