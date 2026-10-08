package com.tokenmaxxing.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalContext
import androidx.glance.LocalSize
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontFamily
import androidx.glance.text.Text
import androidx.glance.text.TextAlign
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider

class UsageWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = UsageWidget()
}

/** One resizable widget; its layout follows its size on the home screen. */
class UsageWidget : GlanceAppWidget() {
    override val sizeMode = SizeMode.Exact

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val list = Store.usage(context)
        val pick = Store.widgetAccount(context)
        provideContent { Body(list, pick, System.currentTimeMillis()) }
    }
}

/** Meter as a bitmap: Glance can't overlay the pace tick on a progress bar. */
object Bars {
    fun bitmap(w: Int, h: Int, pct: Int, color: Long, expected: Double?): Bitmap {
        val b = Bitmap.createBitmap(w.coerceAtLeast(4), h.coerceAtLeast(2), Bitmap.Config.ARGB_8888)
        val c = Canvas(b)
        val p = Paint(Paint.ANTI_ALIAS_FLAG)
        val r = b.height * 0.2f
        p.color = Colors.TRACK.toInt()
        c.drawRoundRect(0f, 0f, b.width.toFloat(), b.height.toFloat(), r, r, p)
        val fw = b.width * pct.coerceIn(0, 100) / 100f
        if (fw > 0) {
            p.color = color.toInt()
            c.drawRoundRect(0f, 0f, fw, b.height.toFloat(), r, r, p)
        }
        if (expected != null) {
            val tw = maxOf(2f, b.height / 3f)
            val x = (b.width * expected / 100).toFloat().coerceIn(0f, b.width - tw)
            p.color = Colors.TICK.toInt()
            c.drawRect(x, 0f, x + tw, b.height.toFloat(), p)
        }
        return b
    }
}

private fun mono(color: Long, size: Int, align: TextAlign = TextAlign.Start) = TextStyle(
    color = ColorProvider(Color(color)), fontSize = size.sp,
    fontFamily = FontFamily.Monospace, textAlign = align,
)

@Composable
private fun Bar(width: Dp, height: Dp, pct: Int, color: Long, expected: Double?) {
    val d = LocalContext.current.resources.displayMetrics.density
    val bmp = Bars.bitmap((width.value * d).toInt(), (height.value * d).toInt(), pct, color, expected)
    Image(ImageProvider(bmp), contentDescription = null,
          modifier = GlanceModifier.width(width).height(height))
}

private fun weeklyPace(u: Usage, now: Long) = Pace.of(u.weekly.pct, u.weekly.resetsAt, Pace.WEEK_MIN, now)
private fun sessionPace(u: Usage, now: Long) =
    if (u.active) Pace.of(u.session.pct, u.session.resetsAt, Pace.SESSION_MIN, now) else null

@Composable
private fun Body(list: List<Usage>, pick: String, now: Long) {
    val size = LocalSize.current
    Box(
        modifier = GlanceModifier.fillMaxSize()
            .background(Color(0xE6000000))
            .cornerRadius(24.dp)
            .clickable(actionStartActivity<MainActivity>()),
    ) {
        if (list.isEmpty()) {
            Column(GlanceModifier.fillMaxSize().padding(12.dp),
                   verticalAlignment = Alignment.CenterVertically,
                   horizontalAlignment = Alignment.CenterHorizontally) {
                Text("TOKEN MAXXING", style = mono(Colors.ACCENT, 11))
                Text("Tap to sign in", style = mono(Colors.DIM, 11))
            }
            return@Box
        }
        val one = list.firstOrNull { it.id == pick } ?: list.first()
        val w = size.width
        val h = size.height
        when {
            w < 110.dp && h < 110.dp -> Tiny(one, now)
            h < 110.dp && w >= 250.dp -> Strip(list, w, now)
            h < 110.dp -> Wide(one, w, now)
            w < 250.dp -> Square(one, w, now)
            else -> Large(list, w, h, now)
        }
    }
}

/** 1×1: weekly % and pace. */
@Composable
private fun Tiny(u: Usage, now: Long) {
    val p = weeklyPace(u, now)
    Column(GlanceModifier.fillMaxSize(), verticalAlignment = Alignment.CenterVertically,
           horizontalAlignment = Alignment.CenterHorizontally) {
        Text("${u.weekly.pct}%", style = mono(if (u.ok) Colors.forPct(u.weekly.pct) else Colors.DIM, 20))
        if (p != null) Text(p.delta, style = mono(p.color, 12))
    }
}

/** 2×1: one account, weekly with bar. */
@Composable
private fun Wide(u: Usage, w: Dp, now: Long) {
    val p = weeklyPace(u, now)
    Column(GlanceModifier.fillMaxSize().padding(horizontal = 14.dp),
           verticalAlignment = Alignment.CenterVertically) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(u.label, style = mono(Colors.CLAUDE, 12), maxLines = 1,
                 modifier = GlanceModifier.defaultWeight())
            Text("${u.weekly.pct}%", style = mono(Colors.forPct(u.weekly.pct), 18))
            if (p != null) {
                Spacer(GlanceModifier.width(5.dp))
                Text(p.delta, style = mono(p.color, 12))
            }
        }
        Spacer(GlanceModifier.height(6.dp))
        Bar(w - 28.dp, 6.dp, u.weekly.pct, Colors.forPct(u.weekly.pct), p?.expected)
    }
}

/** 2×2: one account, weekly big plus the session line. */
@Composable
private fun Square(u: Usage, w: Dp, now: Long) {
    val p = weeklyPace(u, now)
    Column(GlanceModifier.fillMaxSize().padding(14.dp)) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(u.label, style = mono(Colors.CLAUDE, 13), maxLines = 1,
                 modifier = GlanceModifier.defaultWeight())
            if (u.plan.isNotBlank()) Text(u.plan, style = mono(Colors.DIM, 10))
        }
        Spacer(GlanceModifier.height(6.dp))
        Text("WEEKLY", style = mono(Colors.DIM, 11))
        Row(verticalAlignment = Alignment.Bottom) {
            Text("${u.weekly.pct}%", style = mono(if (u.ok) Colors.forPct(u.weekly.pct) else Colors.DIM, 32))
            if (p != null) {
                Spacer(GlanceModifier.width(6.dp))
                Text(p.delta, style = mono(p.color, 14), modifier = GlanceModifier.padding(bottom = 5.dp))
            }
        }
        Spacer(GlanceModifier.height(4.dp))
        Bar(w - 28.dp, 6.dp, u.weekly.pct, Colors.forPct(u.weekly.pct), p?.expected)
        Spacer(GlanceModifier.defaultWeight())
        val s = if (u.active) "SESSION ${u.session.pct}% · ${fmtShort(minutesLeft(u.session.resetsAt, now))}"
                else "SESSION idle"
        Text(if (u.ok) s else u.error, style = mono(Colors.SOFT, 11), maxLines = 1)
    }
}

/** 4×1: every account side by side, session on top, weekly below (like the taskbar strip). */
@Composable
private fun Strip(list: List<Usage>, w: Dp, now: Long) {
    val fit = ((w.value - 24) / 92).toInt().coerceAtLeast(1)
    Row(GlanceModifier.fillMaxSize().padding(horizontal = 12.dp),
        verticalAlignment = Alignment.CenterVertically) {
        list.take(fit).forEach { u ->
            Column(GlanceModifier.defaultWeight()) {
                Text(u.label, style = mono(Colors.CLAUDE, 11), maxLines = 1)
                PctLine(if (u.active) u.session.pct else -1, sessionPct = true, pace = sessionPace(u, now), ok = u.ok)
                PctLine(u.weekly.pct, sessionPct = false, pace = weeklyPace(u, now), ok = u.ok)
            }
        }
    }
}

@Composable
private fun PctLine(pct: Int, sessionPct: Boolean, pace: Pace?, ok: Boolean) {
    Row(verticalAlignment = Alignment.Bottom) {
        val txt = if (pct < 0) "--" else "$pct%"
        Text(txt, style = mono(if (!ok || pct < 0) Colors.DIM else Colors.forPct(pct), 15))
        if (pace != null) {
            Spacer(GlanceModifier.width(4.dp))
            Text(pace.delta, style = mono(pace.color, 11))
        }
    }
}

/** 4×2 and up: all accounts, weekly pace; tall enough adds each session row. */
@Composable
private fun Large(list: List<Usage>, w: Dp, h: Dp, now: Long) {
    val tall = h >= 220.dp
    val inner = w - 32.dp
    val barW = (inner - 72.dp - 44.dp - 36.dp - 18.dp).coerceAtLeast(20.dp)
    Column(GlanceModifier.fillMaxSize().padding(horizontal = 16.dp, vertical = 14.dp)) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("TOKEN MAXXING", style = mono(Colors.ACCENT, 12), modifier = GlanceModifier.defaultWeight())
            Text(fmtAgo(list.maxOf { it.fetchedAt }, now), style = mono(Colors.DIM, 11))
        }
        Spacer(GlanceModifier.height(8.dp))
        val rowsPerAcct = if (tall) 2 else 1
        val maxAccts = (((h.value - 70) / 22) / rowsPerAcct).toInt().coerceAtLeast(1)
        list.take(maxAccts).forEach { u ->
            if (!u.ok && u.fetchedAt == 0L) {
                Row(GlanceModifier.fillMaxWidth().height(22.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(u.label, style = mono(Colors.CLAUDE, 13), maxLines = 1, modifier = GlanceModifier.width(72.dp))
                    Text(u.error, style = mono(Colors.DIM, 11), maxLines = 1)
                }
                return@forEach
            }
            if (tall) {
                LargeRow(u.label, if (u.active) u.session.pct else -1, sessionPace(u, now), barW, u.ok, nameCol = Colors.CLAUDE)
                LargeRow("", u.weekly.pct, weeklyPace(u, now), barW, u.ok, nameCol = Colors.CLAUDE)
            } else {
                LargeRow(u.label, u.weekly.pct, weeklyPace(u, now), barW, u.ok, nameCol = Colors.CLAUDE)
            }
        }
        Spacer(GlanceModifier.defaultWeight())
        Text(if (tall) "SESSION / WEEKLY" else "WEEKLY", style = mono(Colors.DIM, 10))
    }
}

@Composable
private fun LargeRow(name: String, pct: Int, pace: Pace?, barW: Dp, ok: Boolean, nameCol: Long) {
    val col = if (!ok || pct < 0) Colors.DIM else Colors.forPct(pct)
    Row(GlanceModifier.fillMaxWidth().height(22.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(name, style = mono(nameCol, 13), maxLines = 1, modifier = GlanceModifier.width(72.dp))
        Text(if (pct < 0) "--" else "$pct%", style = mono(col, 13, TextAlign.End),
             modifier = GlanceModifier.width(44.dp))
        Spacer(GlanceModifier.width(8.dp))
        Bar(barW, 6.dp, maxOf(pct, 0), col, pace?.expected)
        Spacer(GlanceModifier.width(4.dp))
        Text(pace?.delta ?: "", style = mono(pace?.color ?: Colors.DIM, 13, TextAlign.End),
             modifier = GlanceModifier.width(36.dp))
    }
}
