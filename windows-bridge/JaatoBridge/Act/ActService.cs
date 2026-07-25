using JaatoBridge.Observe;
using JaatoBridge.Platform;
using JaatoBridge.Settle;
using JaatoBridge.State;
using JaatoBridge.Transport;

namespace JaatoBridge.Act;

/// <summary>Glue for the <c>act</c> verb (§5.3): parse → integrity gate → resolve → actuate → arm settle.</summary>
public sealed class ActService
{
    readonly Resolver _resolver;
    readonly Actuator _actuator;
    readonly SnapshotStore _store;
    readonly SessionState _state;
    readonly SettleService _settle;

    public ActService(UiaSession uia, SnapshotStore store, SessionState state, SettleService settle)
    {
        _resolver = new Resolver(uia, store);
        _actuator = new Actuator();
        _store = store;
        _state = state;
        _settle = settle;
    }

    public object Act(ReqFrame req)
    {
        string action = req.Arg<string>("action") ?? throw new ProtoException(Err.Internal, "missing action");

        // GLOBAL is a desktop-level op — no element target, no per-window integrity gate.
        if (action.Equals("GLOBAL", StringComparison.OrdinalIgnoreCase))
            return _actuator.Global(req.Arg<string>("global"));

        // §11.1 TYPE_TEXT / PRESS_KEY — focus-directed, no target. Act on the foreground/focused window;
        // still integrity-gate it (§12.5: SendInput into an elevated foreground is silently dropped), and
        // NOT_ACTIONABLE if nothing holds focus.
        if (action.Equals("TYPE_TEXT", StringComparison.OrdinalIgnoreCase) ||
            action.Equals("PRESS_KEY", StringComparison.OrdinalIgnoreCase))
        {
            IntPtr fg = WinApi.GetForegroundWindow();
            if (fg == IntPtr.Zero) throw new ProtoException(Err.NotActionable, "nothing holds keyboard focus");
            GateIntegrity(fg);
            var r = _actuator.FocusInput(action, req.Arg<string>("text"), req.Arg<string>("key"));
            _settle.ArmForAct(fg, EffectiveSettle(req));
            Log.Info($"act {action} (focus-directed) hwnd=0x{fg.ToInt64():X} → ok");
            return r;
        }

        var sel = Selector.Parse(req.ArgObj("target"));
        IntPtr hwnd = DetermineHwnd(req, sel);

        // §3.2 MANDATORY integrity pre-check — before ANY actuation. SendInput can't report its own
        // failure against a higher-IL window (§12.5), so we must refuse up front, never attempt-and-observe.
        GateIntegrity(hwnd);

        var match = _resolver.Resolve(sel, hwnd);
        if (match.Hwnd != hwnd) GateIntegrity(match.Hwnd); // ref-selector may target a different stored window

        string? text = req.Arg<string>("text");
        var gesture = req.ArgObj("gesture");
        var result = _actuator.Perform(match, action, text, gesture);
        Log.Info($"act {action} matchedBy={match.MatchedBy} ref={match.MatchedRef} hwnd=0x{match.Hwnd.ToInt64():X} → ok");

        // §5.3: act does not block on settle — it arms the next settle window; the device emits `settled`.
        _settle.ArmForAct(match.Hwnd, EffectiveSettle(req));
        return result;
    }

    /// <summary>Session SettleConfig with any per-call <c>settleOverride</c> merged on top (§9).</summary>
    SettleConfig EffectiveSettle(ReqFrame req)
    {
        var baseCfg = _state.Current.Settle;
        var o = req.ArgObj("settleOverride");
        if (o is null) return baseCfg;
        return baseCfg with
        {
            QuietWindowMs = (int?)o["quietWindowMs"] ?? baseCfg.QuietWindowMs,
            HardTimeoutMs = (int?)o["hardTimeoutMs"] ?? baseCfg.HardTimeoutMs,
            Mode = (string?)o["mode"] ?? baseCfg.Mode,
            MinEventCount = (int?)o["minEventCount"] ?? baseCfg.MinEventCount,
            BundleScreenshotOnSettle = (bool?)o["bundleScreenshotOnSettle"] ?? baseCfg.BundleScreenshotOnSettle,
            EventMask = o["eventMask"] is System.Text.Json.Nodes.JsonArray a
                ? a.Select(n => (string)n!).ToArray() : baseCfg.EventMask,
        };
    }

    IntPtr DetermineHwnd(ReqFrame req, Selector sel)
    {
        if (sel.HasRef) return _store.Latest?.Hwnd ?? IntPtr.Zero;
        if (req.Args?["window"] is { } w) return new IntPtr(w.GetValue<long>());
        if (_store.Latest is { } e && e.Hwnd != IntPtr.Zero) return e.Hwnd;
        return WinApi.GetForegroundWindow();
    }

    static void GateIntegrity(IntPtr hwnd)
    {
        if (hwnd == IntPtr.Zero) throw new ProtoException(Err.NotFound, "no target window for act");
        int pid = WinApi.Pid(hwnd);
        if (pid == 0) throw new ProtoException(Err.NotFound, "target window has no process");
        var target = Native.ProcessIntegrity(pid);
        var own = Native.OwnIntegrity();
        if (target > own)
            throw new ProtoException(Err.Permission,
                $"target window is higher integrity ({target}) than the bridge ({own}); elevated windows are refused");
    }
}
