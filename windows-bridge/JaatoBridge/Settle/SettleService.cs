using System.Collections.Concurrent;
using JaatoBridge.Observe;
using JaatoBridge.Platform;
using JaatoBridge.Shot;
using JaatoBridge.State;
using JaatoBridge.Transport;

namespace JaatoBridge.Settle;

/// <summary>
/// Drives the §6 detector on behalf of the verbs. After <c>act</c> the settle is <b>armed and the
/// device emits the unsolicited <c>settled</c> event</b> (§5.3/§6.2) — act itself never blocks.
/// <c>waitForSettle</c> arms-and-awaits, returning the outcome in its response, cancelable via <c>cancel</c>.
/// </summary>
public sealed class SettleService
{
    readonly SettleDetector _det;
    readonly WsClient _ws;
    readonly SnapshotClock _clock;
    readonly ScreenshotService _shots;
    readonly ObserveService _observe;
    readonly ConcurrentDictionary<string, CancellationTokenSource> _waits = new();
    CancellationTokenSource? _actCts;

    public SettleService(UiaSession uia, WsClient ws, SnapshotClock clock, ScreenshotService shots, ObserveService observe)
    {
        _det = new SettleDetector(uia);
        _ws = ws;
        _clock = clock;
        _shots = shots;
        _observe = observe;
    }

    /// <summary>Arm a settle after an action and emit <c>settled</c> when quiet/timeout (fire-and-forget).</summary>
    public void ArmForAct(IntPtr hwnd, SettleConfig cfg)
    {
        _actCts?.Cancel();
        var cts = new CancellationTokenSource();
        _actCts = cts;
        _ = Task.Run(async () =>
        {
            try
            {
                var res = await _det.ArmAsync(hwnd, cfg, cts.Token).ConfigureAwait(false);
                await EmitSettled(hwnd, cfg, res.Reason).ConfigureAwait(false);
            }
            catch (OperationCanceledException) { }
            catch (Exception ex) { Log.Warn($"act-settle: {ex.Message}"); }
        });
    }

    /// <summary><c>waitForSettle</c> — arm and await, returning {reason, snapshotVersion}. Cancelable.</summary>
    public async Task<object> WaitForSettle(string reqId, IntPtr hwnd, SettleConfig cfg)
    {
        var cts = new CancellationTokenSource();
        _waits[reqId] = cts;
        try
        {
            var res = await _det.ArmAsync(hwnd, cfg, cts.Token).ConfigureAwait(false);
            return new { reason = res.Reason, snapshotVersion = _clock.Next() };
        }
        catch (OperationCanceledException) { throw new ProtoException(Err.Canceled, "waitForSettle canceled"); }
        finally { _waits.TryRemove(reqId, out _); }
    }

    public object Cancel(string? targetId)
    {
        if (targetId is not null && _waits.TryGetValue(targetId, out var cts)) { cts.Cancel(); return new { canceled = true }; }
        return new { canceled = false };
    }

    async Task EmitSettled(IntPtr hwnd, SettleConfig cfg, string reason)
    {
        long v = _clock.Next();
        var info = WindowLister.Describe(hwnd);
        bool bundled = false;
        if (cfg.BundleScreenshotOnSettle)
            bundled = await _shots.Bundled(hwnd, v, $"settle-{v}", _observe.PasswordNodes(hwnd)).ConfigureAwait(false);

        await _ws.SendTextAsync(Wire.Event("settled", new
        {
            reason,
            snapshotVersion = v,
            pkg = WindowLister.Pkg(info),
            hasBundledScreenshot = bundled,
        })).ConfigureAwait(false);
        Log.Info($"settled({reason}) v{v} bundled={bundled}");
    }
}
