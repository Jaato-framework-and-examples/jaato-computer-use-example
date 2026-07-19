package com.jaato.a11ybridge.shot

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.SystemClock
import android.view.Display
import com.jaato.a11ybridge.act.Resolver
import com.jaato.a11ybridge.observe.Nodes
import com.jaato.a11ybridge.observe.TreeWalker
import com.jaato.a11ybridge.state.SessionConfig
import com.jaato.a11ybridge.state.SessionStore
import com.jaato.a11ybridge.transport.BinaryFrame
import com.jaato.a11ybridge.transport.DeviceError
import com.jaato.a11ybridge.transport.ErrorCode
import com.jaato.a11ybridge.transport.ShotHeader
import com.jaato.a11ybridge.transport.ShotParams
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executor
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.suspendCancellableCoroutine
import okio.ByteString

/**
 * Captures, redacts, downsamples, encodes and ships a screenshot (device design §7, §12).
 *
 * Rate-limit is surfaced, never hidden (§12): the OS enforces ~1 capture/second; on the
 * interval error we return RATE_LIMITED with a computed `retryAfterMs`, and only if the
 * request set `allowCached` do we re-ship the last encoded frame instead.
 */
class ScreenshotCapturer(
    private val service: AccessibilityService,
    private val walker: TreeWalker,
    private val resolver: Resolver,
    private val session: SessionStore,
    private val sendBinary: (ByteString) -> Unit,
) {
    /** OS-documented minimum interval between captures (§12). Used only to compute retryAfterMs. */
    private val minIntervalMs = 1000L

    private val executor = Executor { it.run() }

    @Volatile private var lastCaptureUptime = 0L
    @Volatile private var cached: Cached? = null

    private class Cached(
        val bytes: ByteArray,
        val width: Int,
        val height: Int,
        val format: String,
        val version: Long?,
    )

    /**
     * Capture and ship one screenshot as a binary frame. On success the binary frame is sent
     * and [Result.success] returned; on failure a [DeviceError] is returned for the router to
     * turn into a failed response (or, for bundled captures, a `screenshot_error` event).
     */
    suspend fun capture(
        params: ShotParams,
        version: Long?,
        reason: String,
        correlationId: String,
    ): Result<Unit> {
        val shot = try {
            takeScreenshotOrThrow()
        } catch (e: DeviceError) {
            if (e.code == ErrorCode.RATE_LIMITED && params.allowCached) {
                cached?.let { c ->
                    sendBinary(frame(c.bytes, c.width, c.height, c.format, c.version, correlationId, reason))
                    return Result.success(Unit)
                }
            }
            return Result.failure(e)
        }

        return try {
            val cfg = session.get()
            val encoded = processAndEncode(shot, params, cfg)
            lastCaptureUptime = SystemClock.uptimeMillis()
            cached = Cached(encoded.bytes, encoded.width, encoded.height, params.format, version)
            sendBinary(frame(encoded.bytes, encoded.width, encoded.height, params.format, version, correlationId, reason))
            Result.success(Unit)
        } catch (e: DeviceError) {
            Result.failure(e)
        } catch (e: Throwable) {
            Result.failure(DeviceError(ErrorCode.INTERNAL, e.message ?: "screenshot processing failed"))
        }
    }

    private class Encoded(val bytes: ByteArray, val width: Int, val height: Int)

    private fun processAndEncode(
        shot: AccessibilityService.ScreenshotResult,
        params: ShotParams,
        cfg: SessionConfig,
    ): Encoded {
        // Wrap the hardware buffer, then copy to a MUTABLE software bitmap so we can redact,
        // scale and crop it (hardware bitmaps are immutable).
        val hw = Bitmap.wrapHardwareBuffer(shot.hardwareBuffer, shot.colorSpace)
            ?: run {
                shot.hardwareBuffer.close()
                throw DeviceError(ErrorCode.INTERNAL, "wrapHardwareBuffer returned null")
            }
        var bmp = hw.copy(Bitmap.Config.ARGB_8888, /* mutable = */ true)
        hw.recycle()
        shot.hardwareBuffer.close()

        // §13 redaction — on the full-resolution bitmap, before compression.
        Redactor.apply(bmp, buildMaskRects(cfg))

        // §12: crop is in screen pixels, applied before downsample; downsample last (bandwidth lever).
        params.crop?.let { bmp = cropTo(bmp, it) }
        bmp = downsample(bmp, params.maxDimension)

        val bytes = encode(bmp, params.format, params.quality)
        return Encoded(bytes, bmp.width, bmp.height)
    }

    /** Mask rects: password nodes (from one raw walk) + resolved extraMaskSelectors (§13). */
    private fun buildMaskRects(cfg: SessionConfig): List<Rect> {
        val rects = ArrayList<Rect>()
        val redaction = cfg.redaction
        if (redaction.maskPasswordNodes) {
            val raw = walker.walkRaw(cfg.packageScope).nodes
            for (n in raw) {
                if (n.password && n.visible) {
                    val b = n.bounds
                    rects.add(Rect(b[0], b[1], b[2], b[3]))
                }
            }
        }
        for (sel in redaction.extraMaskSelectors) {
            runCatching { resolver.resolve(sel, cfg.packageScope) }.getOrNull()?.let { rr ->
                val r = Rect()
                rr.node.getBoundsInScreen(r)
                rects.add(r)
                Nodes.recycle(rr.node)
            }
        }
        return rects
    }

    private fun cropTo(bmp: Bitmap, crop: List<Int>): Bitmap {
        if (crop.size != 4) return bmp
        val left = crop[0].coerceIn(0, bmp.width)
        val top = crop[1].coerceIn(0, bmp.height)
        val right = crop[2].coerceIn(left, bmp.width)
        val bottom = crop[3].coerceIn(top, bmp.height)
        val w = right - left
        val h = bottom - top
        if (w <= 0 || h <= 0) return bmp
        val out = Bitmap.createBitmap(bmp, left, top, w, h)
        if (out !== bmp) bmp.recycle()
        return out
    }

    private fun downsample(bmp: Bitmap, maxDimension: Int): Bitmap {
        if (maxDimension <= 0) return bmp
        val longest = maxOf(bmp.width, bmp.height)
        if (longest <= maxDimension) return bmp
        val scale = maxDimension.toFloat() / longest
        val w = (bmp.width * scale).toInt().coerceAtLeast(1)
        val h = (bmp.height * scale).toInt().coerceAtLeast(1)
        val out = Bitmap.createScaledBitmap(bmp, w, h, /* filter = */ true)
        if (out !== bmp) bmp.recycle()
        return out
    }

    private fun encode(bmp: Bitmap, format: String, quality: Int): ByteArray {
        val q = quality.coerceIn(0, 100)
        val fmt = when (format.lowercase()) {
            "webp" -> Bitmap.CompressFormat.WEBP_LOSSY
            "png" -> Bitmap.CompressFormat.PNG
            "jpeg", "jpg" -> Bitmap.CompressFormat.JPEG
            else -> Bitmap.CompressFormat.WEBP_LOSSY
        }
        val out = ByteArrayOutputStream()
        bmp.compress(fmt, q, out)
        return out.toByteArray()
    }

    private fun frame(
        bytes: ByteArray,
        width: Int,
        height: Int,
        format: String,
        version: Long?,
        correlationId: String,
        reason: String,
    ): ByteString {
        val header = ShotHeader(
            correlationId = correlationId,
            snapshotVersion = version,
            format = format,
            width = width,
            height = height,
            reason = reason,
        )
        return BinaryFrame.frame(header.toJson(), bytes)
    }

    private suspend fun takeScreenshotOrThrow(): AccessibilityService.ScreenshotResult =
        suspendCancellableCoroutine { cont ->
            service.takeScreenshot(
                Display.DEFAULT_DISPLAY,
                executor,
                object : AccessibilityService.TakeScreenshotCallback {
                    override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                        cont.resume(result)
                    }

                    override fun onFailure(errorCode: Int) {
                        cont.resumeWithException(mapError(errorCode))
                    }
                },
            )
        }

    private fun mapError(errorCode: Int): DeviceError = when (errorCode) {
        AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT ->
            DeviceError(ErrorCode.RATE_LIMITED, "capture interval too short", retryAfterMs = retryAfterMs())
        AccessibilityService.ERROR_TAKE_SCREENSHOT_NO_ACCESSIBILITY_ACCESS ->
            DeviceError(ErrorCode.PERMISSION, "no accessibility access for screenshot")
        else ->
            DeviceError(ErrorCode.INTERNAL, "takeScreenshot failed ($errorCode)")
    }

    private fun retryAfterMs(): Long {
        val elapsed = SystemClock.uptimeMillis() - lastCaptureUptime
        return (minIntervalMs - elapsed).coerceIn(0, minIntervalMs)
    }
}
