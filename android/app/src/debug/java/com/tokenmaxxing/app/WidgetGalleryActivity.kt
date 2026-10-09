package com.tokenmaxxing.app

import android.appwidget.AppWidgetHost
import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.graphics.Color
import android.os.Bundle
import android.util.SizeF
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.glance.appwidget.updateAll
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/**
 * Debug-only gallery: hosts the real widget at the sizes Samsung's One UI grid gives on a
 * 384dp-wide phone (S25 Ultra at default zoom). Needs `adb shell appwidget grantbind`.
 */
class WidgetGalleryActivity : ComponentActivity() {
    private lateinit var host: AppWidgetHost

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppWidgetHost(this, 4242).deleteHost()      // drop widgets left by earlier runs
        host = AppWidgetHost(this, 4242)
        host.startListening()
        val mgr = AppWidgetManager.getInstance(this)
        val provider = ComponentName(this, UsageWidgetReceiver::class.java)
        val d = resources.displayMetrics.density
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding((12 * d).toInt(), (40 * d).toInt(), (12 * d).toInt(), (40 * d).toInt())
            setBackgroundColor(Color.rgb(26, 34, 44))
        }
        val sizes = (intent.getStringExtra("sizes") ?: "1x1:85x120,2x1:175x120,2x2:175x250,4x1:360x120")
            .split(",").map { it.split(":") }
        for ((name, wh) in sizes) {
            val (w, h) = wh.split("x").map { it.toFloat() }
            val id = host.allocateAppWidgetId()
            mgr.bindAppWidgetIdIfAllowed(id, provider)
            val v = host.createView(this, id, mgr.getAppWidgetInfo(id))
            v.updateAppWidgetSize(Bundle(), listOf(SizeF(w, h)))
            col.addView(TextView(this).apply {
                text = "$name  (${w.toInt()}×${h.toInt()} dp)"
                setTextColor(Color.LTGRAY)
                setPadding(0, (10 * d).toInt(), 0, (4 * d).toInt())
            })
            col.addView(v, LinearLayout.LayoutParams((w * d).toInt(), (h * d).toInt()))
        }
        setContentView(ScrollView(this).apply { addView(col) })
        lifecycleScope.launch { UsageWidget().updateAll(this@WidgetGalleryActivity) }
    }

    override fun onDestroy() {
        host.deleteHost()
        super.onDestroy()
    }
}
