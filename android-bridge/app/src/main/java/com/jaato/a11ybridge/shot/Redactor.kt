package com.jaato.a11ybridge.shot

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect

/**
 * Composites opaque rectangles over sensitive regions of the raw bitmap BEFORE compression,
 * so those pixels never leave the device (PROTOCOL §13, device design §7).
 *
 * This is the one place the device composites — because "do not emit these pixels" is a
 * security boundary, not a heuristic. Rects are in screen pixels and are applied while the
 * bitmap is still at full screen resolution (before any downsample/crop).
 */
object Redactor {

    fun apply(bitmap: Bitmap, rects: List<Rect>) {
        if (rects.isEmpty()) return
        val canvas = Canvas(bitmap)
        val paint = Paint().apply {
            color = Color.BLACK
            style = Paint.Style.FILL
            isAntiAlias = false
        }
        for (r in rects) {
            val clip = Rect(r)
            if (clip.intersect(0, 0, bitmap.width, bitmap.height)) {
                canvas.drawRect(clip, paint)
            }
        }
    }
}
