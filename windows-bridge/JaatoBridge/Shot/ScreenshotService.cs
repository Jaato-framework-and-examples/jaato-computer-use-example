using JaatoBridge.Observe;
using JaatoBridge.State;
using JaatoBridge.Transport;

namespace JaatoBridge.Shot;

/// <summary>
/// Orchestrates §7 capture: WGC frame → redact (§13) → crop/downsample/encode → emit as a §4 binary
/// frame. Used by the <c>screenshot</c> verb (reason=on_demand) and by <c>observe</c> bundling
/// (reason=bundled, sharing the observe's snapshotVersion so tree and image describe one moment).
/// </summary>
public sealed class ScreenshotService
{
    readonly WsClient _ws;
    readonly SessionState _state;
    readonly Lazy<ScreenCapturer?> _cap;

    public ScreenshotService(WsClient ws, SessionState state)
    {
        _ws = ws;
        _state = state;
        _cap = new Lazy<ScreenCapturer?>(() => { try { return new ScreenCapturer(); } catch (Exception ex) { Log.Err($"WGC init failed: {ex.Message}"); return null; } });
    }

    public bool Available => ScreenCapturer.Supported();

    /// <summary>Standalone capture (§5.4). Emits an on_demand binary frame; returns a small res ack.</summary>
    public async Task<object> Standalone(IntPtr hwnd, string correlationId, IReadOnlyList<NodeSnap> passwordNodes, ScreenshotDefaults? over)
    {
        var enc = CaptureEncode(hwnd, passwordNodes, over, out int _)
                  ?? throw new ProtoException(Err.Internal, "capture failed");
        await Emit(correlationId, null, "on_demand", enc);
        return new { format = enc.Format, width = enc.Width, height = enc.Height };
    }

    /// <summary>Bundled capture for observe (§5.2). Best-effort — never fails the observe.</summary>
    public async Task<bool> Bundled(IntPtr hwnd, long snapshotVersion, string correlationId, IReadOnlyList<NodeSnap> passwordNodes)
    {
        try
        {
            var enc = CaptureEncode(hwnd, passwordNodes, null, out int _);
            if (enc is null) return false;
            await Emit(correlationId, snapshotVersion, "bundled", enc);
            return true;
        }
        catch (Exception ex) { Log.Warn($"bundled screenshot skipped: {ex.Message}"); return false; }
    }

    ImageOut.Encoded? CaptureEncode(IntPtr hwnd, IReadOnlyList<NodeSnap> passwordNodes, ScreenshotDefaults? over, out int _)
    {
        _ = 0;
        var cap = _cap.Value;
        if (cap is null) return null;
        var cfg = _state.Current;
        var sd = over ?? cfg.ScreenshotDefaults;

        // Per-window WGC capture can't produce a frame for DWM-cloaked shell surfaces (the Start menu /
        // Search "SearchHost" window) or a vanished/hwnd==0 target — CreateForWindow yields an item but no
        // frame ever arrives, so CaptureWindow times out. Fall back to a full-screen monitor capture, which
        // still shows the Start overlay (what the model needs) and never leaves the daemon blocked on a blob
        // that will never come (which otherwise crashes the controller at connect).
        ScreenCapturer.Frame frame;
        if (hwnd == IntPtr.Zero)
        {
            frame = cap.CaptureMonitor();
        }
        else
        {
            try { frame = cap.CaptureWindow(hwnd); }
            catch (Exception ex)
            {
                Log.Warn($"window capture failed ({ex.Message}); falling back to full-screen");
                frame = cap.CaptureMonitor();
            }
        }
        if (cfg.Redaction.MaskPasswordNodes && passwordNodes.Count > 0)
            Redactor.MaskPasswords(frame.Bgra, frame.Width, frame.Height, hwnd, passwordNodes);
        return ImageOut.Encode(frame.Bgra, frame.Width, frame.Height, sd.Crop, sd.MaxDimension, sd.Format);
    }

    Task Emit(string correlationId, long? snapshotVersion, string reason, ImageOut.Encoded enc)
    {
        var header = new ScreenshotHeader
        {
            CorrelationId = correlationId,
            SnapshotVersion = snapshotVersion ?? 0,
            Format = enc.Format,
            Width = enc.Width,
            Height = enc.Height,
            Reason = reason,
        };
        Log.Info($"screenshot → {reason} {enc.Format} {enc.Width}x{enc.Height} {enc.Bytes.Length}B (corr={correlationId})");
        return _ws.SendBinaryAsync(BinaryFrame.Build(header, enc.Bytes));
    }
}
