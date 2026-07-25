using JaatoBridge.Observe;
using JaatoBridge.Transport;
using UIAutomationClient;

namespace JaatoBridge.Act;

/// <summary>
/// §5.2 actuator — control patterns first; SendInput only for GESTURE/LONG_CLICK/GLOBAL. A pattern that
/// is unsupported or refuses surfaces NOT_ACTIONABLE; the device never silently substitutes synthetic
/// input for a refused semantic action (that is the controller's decision).
/// </summary>
public sealed class Actuator
{
    public object Perform(Resolver.Match m, string action, string? text, System.Text.Json.Nodes.JsonObject? gesture)
    {
        var el = m.Element;
        try
        {
            switch (action.ToUpperInvariant())
            {
                case "CLICK": Click(el); break;
                case "SET_TEXT": SetText(el, text ?? ""); break;
                case "FOCUS": el.SetFocus(); break;
                case "SCROLL_DOWN": Scroll(el, ScrollAmount.ScrollAmount_NoAmount, ScrollAmount.ScrollAmount_LargeIncrement); break;
                case "SCROLL_UP": Scroll(el, ScrollAmount.ScrollAmount_NoAmount, ScrollAmount.ScrollAmount_LargeDecrement); break;
                case "SCROLL_RIGHT": Scroll(el, ScrollAmount.ScrollAmount_LargeIncrement, ScrollAmount.ScrollAmount_NoAmount); break;
                case "SCROLL_LEFT": Scroll(el, ScrollAmount.ScrollAmount_LargeDecrement, ScrollAmount.ScrollAmount_NoAmount); break;
                case "SCROLL_FORWARD": Scroll(el, ScrollAmount.ScrollAmount_NoAmount, ScrollAmount.ScrollAmount_LargeIncrement); break;
                case "SCROLL_BACKWARD": Scroll(el, ScrollAmount.ScrollAmount_NoAmount, ScrollAmount.ScrollAmount_LargeDecrement); break;
                case "LONG_CLICK": { var (x, y) = Center(el); SyntheticInput.LongClick(x, y); break; }
                case "GESTURE": DoGesture(gesture); break;
                default: throw new ProtoException(Err.NotActionable, $"unsupported action '{action}'");
            }
        }
        // §5.2: a pattern that is unsupported OR fails/refuses surfaces NOT_ACTIONABLE — never a crash to
        // INTERNAL. (Some providers throw on Invoke/SetValue for custom-drawn controls; that is "not actionable".)
        catch (ProtoException) { throw; }
        catch (Exception ex) { throw new ProtoException(Err.NotActionable, $"{action} not applied: {ex.GetType().Name}: {ex.Message}"); }

        return new { resolved = true, matchedRef = m.MatchedRef, matchedBy = m.MatchedBy, settleAwaited = false };
    }

    /// <summary>§9 Windows global action set (Android's BACK/HOME/RECENTS do not exist here).</summary>
    public object Global(string? g)
    {
        switch ((g ?? "").ToUpperInvariant())
        {
            case "MINIMIZE_ALL": SyntheticInput.KeyCombo(SyntheticInput.VK_LWIN, SyntheticInput.VK_M); break;
            case "SHOW_DESKTOP": SyntheticInput.KeyCombo(SyntheticInput.VK_LWIN, SyntheticInput.VK_D); break;
            // START_MENU: the Windows key opens Start (search auto-focused) regardless of foreground —
            // the deterministic "reach the shell" primitive the launch recipe needs (a window-scoped
            // model can't otherwise get from an app to the taskbar/Start).
            case "START_MENU": SyntheticInput.KeyCombo(SyntheticInput.VK_LWIN); break;
            case "SWITCH_WINDOW": SyntheticInput.KeyCombo(SyntheticInput.VK_MENU, SyntheticInput.VK_TAB); break;
            case "CLOSE_WINDOW": SyntheticInput.KeyCombo(SyntheticInput.VK_MENU, SyntheticInput.VK_F4); break;
            case "LOCK_SCREEN": SyntheticInput.LockWorkStation(); break;
            default: throw new ProtoException(Err.NotActionable, $"unknown global '{g}'");
        }
        return new { resolved = true, matchedBy = "global", settleAwaited = false };
    }

    /// <summary>§11.1 focus-directed keyboard input — no target; acts on whatever holds keyboard focus.</summary>
    public object FocusInput(string action, string? text, string? key)
    {
        switch (action.ToUpperInvariant())
        {
            case "TYPE_TEXT":
                if (string.IsNullOrEmpty(text)) throw new ProtoException(Err.NotActionable, "TYPE_TEXT missing text");
                SyntheticInput.TypeText(text);
                break;
            case "PRESS_KEY":
                SyntheticInput.PressKey(MapKey(key));
                break;
            default:
                throw new ProtoException(Err.NotActionable, $"unknown focus action '{action}'");
        }
        return new { resolved = true, matchedBy = "focus", settleAwaited = false };
    }

    // PRESS_KEY.key is an extensible enum — ENTER only for now (§11.1).
    static ushort MapKey(string? key) => (key ?? "").ToUpperInvariant() switch
    {
        "ENTER" => SyntheticInput.VK_RETURN,
        _ => throw new ProtoException(Err.NotActionable, $"unsupported key '{key}' (ENTER only)"),
    };

    void Click(IUIAutomationElement el)
    {
        try { if (el.GetCurrentPattern(Uia.InvokePattern) is IUIAutomationInvokePattern inv) { inv.Invoke(); return; } }
        catch (Exception ex) { Log.Warn($"Invoke threw, trying legacy: {ex.Message}"); }
        try { if (el.GetCurrentPattern(10018) is IUIAutomationLegacyIAccessiblePattern leg) { leg.DoDefaultAction(); return; } }
        catch (Exception ex) { Log.Warn($"legacy DoDefaultAction threw: {ex.Message}"); }
        throw new ProtoException(Err.NotActionable, "Invoke/DoDefaultAction unavailable or refused");
    }

    void SetText(IUIAutomationElement el, string text)
    {
        if (el.GetCurrentPattern(Uia.ValuePattern) is IUIAutomationValuePattern vp)
        {
            if (vp.CurrentIsReadOnly != 0) throw new ProtoException(Err.NotActionable, "value is read-only");
            vp.SetValue(text); return;
        }
        throw new ProtoException(Err.NotActionable, "no ValuePattern");
    }

    void Scroll(IUIAutomationElement el, ScrollAmount h, ScrollAmount v)
    {
        if (el.GetCurrentPattern(Uia.ScrollPattern) is not IUIAutomationScrollPattern sp)
            throw new ProtoException(Err.NotActionable, "no ScrollPattern");
        try { sp.Scroll(h, v); }
        catch (Exception ex) { throw new ProtoException(Err.NotActionable, $"scroll refused: {ex.Message}"); }
    }

    void DoGesture(System.Text.Json.Nodes.JsonObject? g)
    {
        if (g is null) throw new ProtoException(Err.NotActionable, "no gesture payload");
        var type = (string?)g["type"] ?? "tap";
        var path = (g["path"] as System.Text.Json.Nodes.JsonArray)?
            .Select(p => { var a = p!.AsArray(); return ((int)a[0]!.GetValue<double>(), (int)a[1]!.GetValue<double>()); })
            .ToList() ?? new List<(int, int)>();
        int ms = (int?)(g["durationMs"]?.GetValue<int>()) ?? 200;
        if (path.Count == 0) throw new ProtoException(Err.NotActionable, "empty gesture path");
        if (type == "tap") SyntheticInput.Click(path[0].Item1, path[0].Item2);
        else SyntheticInput.Swipe(path, ms);
    }

    static (int x, int y) Center(IUIAutomationElement el)
    {
        var r = el.CurrentBoundingRectangle;
        return ((r.left + r.right) / 2, (r.top + r.bottom) / 2);
    }
}
