package com.tokenmaxxing.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.DpSize
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
import androidx.glance.layout.ColumnScope
import androidx.glance.layout.ContentScale
import androidx.glance.layout.Row
import androidx.glance.layout.RowScope
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

/**
 * One resizable widget. Layouts are handed to the launcher per size step (Responsive),
 * so the launcher picks the one that fits: no guessing from reported sizes, which
 * Samsung's taller cells and late resize updates got wrong.
 */
class UsageWidget : GlanceAppWidget() {
    override val sizeMode = SizeMode.Responsive(
        setOf(
            DpSize(57.dp, 57.dp),                                   // 1×1
            DpSize(150.dp, 57.dp), DpSize(150.dp, 150.dp),          // 2×1, 2×2
            DpSize(250.dp, 57.dp),                                  // 4×1 strip
            DpSize(250.dp, 150.dp), DpSize(250.dp, 200.dp),         // 4×2 …
            DpSize(250.dp, 260.dp), DpSize(250.dp, 340.dp),         // … and taller
        )
    )

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val list = Store.usage(context)
        val pick = Store.widgetAccount(context)
        provideContent { Body(list, pick, System.currentTimeMillis()) }
    }
}

/**
 * Meter as a bitmap (Glance can't overlay the pace tick on a bar). One pixel tall and
 * wider than it will be shown: the launcher only ever shrinks it sideways, so edges and
 * the tick stay sharp, and every size layout's bars fit in one small widget update.
 */
object Bars {
    fun bitmap(w: Int, pct: Int, color: Long, expected: Double?, tickPx: Int): Bitmap {
        val b = Bitmap.createBitmap(w.coerceAtLeast(8), 1, Bitmap.Config.ARGB_8888)
        val c = Canvas(b)
        val p = Paint()
        p.color = Colors.TRACK.toInt()
        c.drawRect(0f, 0f, b.width.toFloat(), 1f, p)
        val fw = b.width * pct.coerceIn(0, 100) / 100f
        if (fw > 0) {
            p.color = color.toInt()
            c.drawRect(0f, 0f, fw, 1f, p)
        }
        if (expected != null) {
            val tw = tickPx.coerceAtLeast(2).toFloat()
            val x = (b.width * expected / 100).toFloat().coerceIn(0f, b.width - tw)
            p.color = Colors.TICK.toInt()
            c.drawRect(x, 0f, x + tw, 1f, p)
        }
        return b
    }
}

private fun mono(color: Long, size: Int, align: TextAlign = TextAlign.Start) = TextStyle(
    color = ColorProvider(Color(color)), fontSize = size.sp,
    fontFamily = FontFamily.Monospace, textAlign = align,
)

private fun pctCol(pct: Int, ok: Boolean) = if (!ok || pct < 0) Colors.DIM else Colors.forPct(pct)
private fun nameCol(u: Usage) = if (u.isCursor) Colors.CURSOR else Colors.CLAUDE
private fun mark(u: Usage) = if (u.isCursor) "◆" else "✶"

@Composable
private fun barBitmap(estWidth: Dp, height: Dp, pct: Int, color: Long, expected: Double?): Bitmap {
    // 2x the estimated width: the real widget is at most ~1.5x the size step it was laid out for.
    val d = LocalContext.current.resources.displayMetrics.density
    return Bars.bitmap((estWidth.value * d * 2).toInt(), pct, color, expected, tickPx = (4 * d).toInt())
}

/** Bar that takes the rest of a row. */
@Composable
private fun RowScope.FlexBar(estWidth: Dp, height: Dp, pct: Int, color: Long, expected: Double?) {
    Image(ImageProvider(barBitmap(estWidth, height, pct, color, expected)), contentDescription = null,
          contentScale = ContentScale.FillBounds,
          modifier = GlanceModifier.defaultWeight().height(height))
}

/** Bar across the full width of a column. */
@Composable
private fun WideBar(estWidth: Dp, height: Dp, pct: Int, color: Long, expected: Double?) {
    Image(ImageProvider(barBitmap(estWidth, height, pct, color, expected)), contentDescription = null,
          contentScale = ContentScale.FillBounds,
          modifier = GlanceModifier.fillMaxWidth().height(height))
}

@Composable
private fun Body(list: List<Usage>, pick: String, now: Long) {
    val size = LocalSize.current
    Box(
        modifier = GlanceModifier.fillMaxSize()
            .background(Color(0xE6000000))
            .cornerRadius(android.R.dimen.system_app_widget_background_radius)
            .clickable(actionStartActivity<MainActivity>()),
        contentAlignment = Alignment.Center,
    ) {
        if (list.isEmpty()) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text("TOKEN MAXXING", style = mono(Colors.ACCENT, 11))
                Text("Tap to sign in", style = mono(Colors.DIM, 11))
            }
            return@Box
        }
        val one = list.firstOrNull { it.id == pick } ?: list.first()
        val w = size.width
        val h = size.height
        when {
            w < 150.dp -> Tiny(one, now)
            w < 250.dp -> if (h < 150.dp) Wide(one, w, now) else Square(one, w, now)
            h < 150.dp -> Strip(list, now)
            else -> Large(list, w, h, now)
        }
    }
}

/** 1×1: the headline % and its pace. */
@Composable
private fun Tiny(u: Usage, now: Long) {
    val p = u.mainPace(now)
    Column(GlanceModifier.fillMaxSize().padding(4.dp), verticalAlignment = Alignment.CenterVertically,
           horizontalAlignment = Alignment.CenterHorizontally) {
        Text(u.label, style = mono(nameCol(u), 10, TextAlign.Center), maxLines = 1)
        Text("${u.main.pct}%", style = mono(pctCol(u.main.pct, u.ok), 20, TextAlign.Center))
        Text(p?.delta ?: u.main.label.lowercase(), style = mono(p?.color ?: Colors.DIM, 11, TextAlign.Center))
    }
}

/** 2×1: one account, headline limit with bar and countdown. */
@Composable
private fun Wide(u: Usage, w: Dp, now: Long) {
    val p = u.mainPace(now)
    Column(GlanceModifier.fillMaxSize().padding(horizontal = 14.dp, vertical = 8.dp),
           verticalAlignment = Alignment.CenterVertically) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(u.label, style = mono(nameCol(u), 12), maxLines = 1, modifier = GlanceModifier.defaultWeight())
            Text("${u.main.pct}%", style = mono(pctCol(u.main.pct, u.ok), 18))
            if (p != null) {
                Spacer(GlanceModifier.width(5.dp))
                Text(p.delta, style = mono(p.color, 12))
            }
        }
        Spacer(GlanceModifier.height(6.dp))
        WideBar(w - 28.dp, 6.dp, u.main.pct, pctCol(u.main.pct, u.ok), p?.expected)
        Spacer(GlanceModifier.height(4.dp))
        Row(GlanceModifier.fillMaxWidth()) {
            Text(u.main.label, style = mono(Colors.DIM, 10), modifier = GlanceModifier.defaultWeight())
            Text(fmtShort(minutesLeft(u.main.resetsAt, now)), style = mono(Colors.SOFT, 10))
        }
    }
}

/** 2×2: one account, headline big, the other limit underneath. */
@Composable
private fun Square(u: Usage, w: Dp, now: Long) {
    val p = u.mainPace(now)
    Column(GlanceModifier.fillMaxSize().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("${mark(u)} ${u.label}", style = mono(nameCol(u), 13), maxLines = 1,
                 modifier = GlanceModifier.defaultWeight())
            if (u.plan.isNotBlank()) Text(u.plan, style = mono(Colors.DIM, 10))
        }
        Spacer(GlanceModifier.height(8.dp))
        Text(u.main.label, style = mono(Colors.DIM, 11))
        Row(verticalAlignment = Alignment.Bottom) {
            Text("${u.main.pct}%", style = mono(pctCol(u.main.pct, u.ok), 32))
            if (p != null) {
                Spacer(GlanceModifier.width(6.dp))
                Text(p.delta, style = mono(p.color, 14), modifier = GlanceModifier.padding(bottom = 5.dp))
            }
        }
        WideBar(w - 28.dp, 7.dp, u.main.pct, pctCol(u.main.pct, u.ok), p?.expected)
        Spacer(GlanceModifier.height(4.dp))
        Text("resets ${fmtShort(minutesLeft(u.main.resetsAt, now))}", style = mono(Colors.SOFT, 11))
        Spacer(GlanceModifier.height(10.dp))
        val other = if (u.isCursor) u.weekly else u.session
        val otherPct = if (u.isCursor) u.weekly.pct else u.sessionShown
        val line = when {
            !u.ok -> u.error
            otherPct < 0 -> "${other.label} idle"
            else -> "${other.label} $otherPct% · ${fmtShort(minutesLeft(other.resetsAt, now))}"
        }
        Text(line, style = mono(if (u.ok) Colors.SOFT else Colors.RED, 11), maxLines = 1)
    }
}

/** 4×1: every account side by side, two limits each (like the taskbar strip). */
@Composable
private fun Strip(list: List<Usage>, now: Long) {
    Row(GlanceModifier.fillMaxSize().padding(horizontal = 14.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        list.take(4).forEach { u ->
            Column(GlanceModifier.defaultWeight()) {
                Text(u.label, style = mono(nameCol(u), 11), maxLines = 1)
                StripLine(if (u.isCursor) "auto" else "5h", u.sessionShown, u.sessionPace(now), u.ok)
                StripLine(if (u.isCursor) "api" else "7d", u.weekly.pct, u.weeklyPace(now), u.ok)
            }
        }
    }
}

@Composable
private fun StripLine(tag: String, pct: Int, pace: Pace?, ok: Boolean) {
    Row(verticalAlignment = Alignment.Bottom) {
        Text(tag, style = mono(Colors.DIM, 10), modifier = GlanceModifier.padding(bottom = 2.dp))
        Spacer(GlanceModifier.width(4.dp))
        Text(if (pct < 0) "--" else "$pct%", style = mono(pctCol(pct, ok), 15))
        if (pace != null) {
            Spacer(GlanceModifier.width(3.dp))
            Text(pace.delta, style = mono(pace.color, 11), modifier = GlanceModifier.padding(bottom = 2.dp))
        }
    }
}

/**
 * 4×2 and up: all accounts like the overlay's stacked view, in the richest form that fits:
 * full (name line + two labelled rows), pair (two rows, name on the first, 5h/7d tags),
 * or compact (one row per account with its headline limit).
 */
@Composable
private fun Large(list: List<Usage>, w: Dp, h: Dp, now: Long) {
    val shown = list.take(5)
    val full = 30 + shown.size * 54 <= h.value.toInt()
    val pair = !full && 40 + shown.size * 36 <= h.value.toInt()
    val labelW = when { full -> 56.dp; pair -> 64.dp; else -> 70.dp }
    val barEst = (w - 28.dp - labelW - (if (pair) 30.dp else 0.dp) - 40.dp - 34.dp - 48.dp).coerceAtLeast(30.dp)
    Column(GlanceModifier.fillMaxSize().padding(horizontal = 14.dp, vertical = 12.dp),
           verticalAlignment = Alignment.CenterVertically) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("TOKEN MAXXING", style = mono(Colors.ACCENT, 11), modifier = GlanceModifier.defaultWeight())
            Text(fmtAgo(list.maxOf { it.fetchedAt }, now), style = mono(Colors.DIM, 10))
        }
        Spacer(GlanceModifier.height(2.dp))
        // Glance allows at most 10 children per Column: one sub-column per account.
        shown.forEach { u ->
            if (full) Column(GlanceModifier.fillMaxWidth().padding(top = 3.dp)) {
                Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("${mark(u)} ${u.label}", style = mono(nameCol(u), 12), maxLines = 1,
                         modifier = GlanceModifier.defaultWeight())
                    if (u.plan.isNotBlank()) Text(u.plan, style = mono(Colors.DIM, 10))
                }
                if (!u.ok) Text(u.error, style = mono(Colors.RED, 10), maxLines = 1)
                LimitRow(u.session.label, 56.dp, u.sessionShown, u.sessionPace(now), u.session.resetsAt, u.ok, barEst, now)
                LimitRow(u.weekly.label, 56.dp, u.weekly.pct, u.weeklyPace(now), u.weekly.resetsAt, u.ok, barEst, now)
            } else if (pair) Column(GlanceModifier.fillMaxWidth().padding(top = 2.dp)) {
                LimitRow(u.label, labelW, u.sessionShown, u.sessionPace(now), u.session.resetsAt, u.ok, barEst, now,
                         labelCol = nameCol(u), tag = if (u.isCursor) "auto" else "5h")
                LimitRow("", labelW, u.weekly.pct, u.weeklyPace(now), u.weekly.resetsAt, u.ok, barEst, now,
                         tag = if (u.isCursor) "api" else "7d")
            } else Column(GlanceModifier.fillMaxWidth()) {
                LimitRow(u.label, 70.dp, u.main.pct, u.mainPace(now), u.main.resetsAt, u.ok, barEst, now,
                         labelCol = nameCol(u))
            }
        }
    }
}

@Composable
private fun ColumnScope.LimitRow(
    label: String, labelW: Dp, pct: Int, pace: Pace?, resetsAt: Long?, ok: Boolean,
    barEst: Dp, now: Long, labelCol: Long = Colors.DIM, tag: String? = null,
) {
    val col = pctCol(pct, ok)
    Row(GlanceModifier.fillMaxWidth().padding(vertical = 1.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = mono(labelCol, 11), maxLines = 1, modifier = GlanceModifier.width(labelW))
        if (tag != null) Text(tag, style = mono(Colors.DIM, 10), modifier = GlanceModifier.width(30.dp))
        Text(if (pct < 0) "--" else "$pct%", style = mono(col, 12, TextAlign.End),
             modifier = GlanceModifier.width(40.dp))
        Spacer(GlanceModifier.width(6.dp))
        FlexBar(barEst, 6.dp, maxOf(pct, 0), col, pace?.expected)
        Text(pace?.delta ?: "", style = mono(pace?.color ?: Colors.DIM, 12, TextAlign.End),
             modifier = GlanceModifier.width(34.dp))
        Text(if (pct < 0) "idle" else fmtShort(minutesLeft(resetsAt, now)), style = mono(Colors.SOFT, 11, TextAlign.End),
             modifier = GlanceModifier.width(48.dp))
    }
}
