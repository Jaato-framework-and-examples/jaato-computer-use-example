using UIAutomationClient;

namespace JaatoBridge.Observe;

/// <summary>
/// The fixed, mechanical §8 pruning transform — shared contract with Android. Reads a node's cached
/// properties (in-proc, no round trip) and decides keep/drop: emit iff visible ∧ (actionable ∨
/// text-bearing ∨ content-described). No policy, no heuristics.
/// </summary>
public static class Pruner
{
    public struct NodeProps
    {
        public string Name, AutomationId, ClassName, HelpText, FullDesc, Value;
        public int[] Bounds;      // [l,t,r,b]
        public int[] RuntimeId;
        public bool Offscreen, Enabled, KbFocusable, Focused, Password;
        public bool Invoke, ValuePat, Scroll, Toggle;
        public bool VScrollable, HScrollable;   // can scroll at all in that axis (false when content fits)
        public double VScrollPct, HScrollPct;    // 0..100 position along the axis (-1 = NoScroll)

        public string? Text => !string.IsNullOrWhiteSpace(Name) ? Name : (!string.IsNullOrWhiteSpace(Value) ? Value : null);
        public string? Desc => !string.IsNullOrWhiteSpace(FullDesc) ? FullDesc : (!string.IsNullOrWhiteSpace(HelpText) ? HelpText : null);
        public bool Actionable => Invoke || ValuePat || Scroll || Toggle;
        public bool Visible => !Offscreen;
    }

    public static NodeProps Read(IUIAutomationElement e)
    {
        return new NodeProps
        {
            Name = Str(e, Uia.Name),
            AutomationId = Str(e, Uia.AutomationId),
            ClassName = Str(e, Uia.ClassName),
            HelpText = Str(e, Uia.HelpText),
            FullDesc = Str(e, Uia.FullDescription),
            Value = Str(e, Uia.ValueValue),
            Bounds = Rect(e),
            RuntimeId = IntArr(e, Uia.RuntimeId),
            Offscreen = Bool(e, Uia.IsOffscreen),
            Enabled = Bool(e, Uia.IsEnabled),
            KbFocusable = Bool(e, Uia.IsKeyboardFocusable),
            Focused = Bool(e, Uia.HasKeyboardFocus),
            Password = Bool(e, Uia.IsPassword),
            Invoke = Bool(e, Uia.IsInvokePatternAvailable),
            ValuePat = Bool(e, Uia.IsValuePatternAvailable),
            Scroll = Bool(e, Uia.IsScrollPatternAvailable),
            Toggle = Bool(e, Uia.IsTogglePatternAvailable),
            VScrollable = Bool(e, Uia.VerticallyScrollable),
            HScrollable = Bool(e, Uia.HorizontallyScrollable),
            VScrollPct = Dbl(e, Uia.VerticalScrollPercent),
            HScrollPct = Dbl(e, Uia.HorizontalScrollPercent),
        };
    }

    public static bool Keep(in NodeProps p) => p.Visible && (p.Actionable || p.Text is not null || p.Desc is not null);

    public static NodeSnap ToSnap(int refId, in NodeProps p, int? parent)
    {
        var flags = new List<string>(8);
        if (p.Invoke) flags.Add("clickable");
        if (p.ValuePat) flags.Add("editable");
        // Position-aware scroll flags (§8, matches Android's scrollableDown/Up/Left/Right companions which
        // the daemon folds into `scrollable:down,up`). Emit an axis direction only if it can STILL scroll
        // that way; `scrollable` iff any direction is available. Fully visible / at all edges => nothing,
        // so the model stops instead of no-op-scroll-looping (Windows no-ops an at-edge scroll silently).
        bool sDown = p.VScrollable && p.VScrollPct < 99.5;
        bool sUp = p.VScrollable && p.VScrollPct > 0.5;
        bool sRight = p.HScrollable && p.HScrollPct < 99.5;
        bool sLeft = p.HScrollable && p.HScrollPct > 0.5;
        if (sDown || sUp || sRight || sLeft)
        {
            flags.Add("scrollable");
            if (sDown) flags.Add("scrollableDown");
            if (sUp) flags.Add("scrollableUp");
            if (sLeft) flags.Add("scrollableLeft");
            if (sRight) flags.Add("scrollableRight");
        }
        if (p.Toggle) flags.Add("checkable");
        if (p.Enabled) flags.Add("enabled");
        if (p.KbFocusable) flags.Add("focusable");
        if (p.Focused) flags.Add("focused");
        if (!p.Offscreen) flags.Add("visible");
        if (p.Password) flags.Add("password");

        return new NodeSnap
        {
            Ref = refId,
            Cls = !string.IsNullOrEmpty(p.ClassName) ? p.ClassName : "",
            ViewId = string.IsNullOrEmpty(p.AutomationId) ? null : p.AutomationId,
            Text = p.Text,
            Desc = p.Desc,
            Bounds = p.Bounds,
            Flags = flags,
            Parent = parent,
        };
    }

    static string Str(IUIAutomationElement e, int id)
    {
        try { return e.GetCachedPropertyValue(id) as string ?? ""; } catch { return ""; }
    }
    static bool Bool(IUIAutomationElement e, int id)
    {
        try { return e.GetCachedPropertyValue(id) is bool b && b; } catch { return false; }
    }
    static double Dbl(IUIAutomationElement e, int id)
    {
        try { return e.GetCachedPropertyValue(id) is double d ? d : -1; } catch { return -1; }
    }
    static int[] IntArr(IUIAutomationElement e, int id)
    {
        try { return e.GetCachedPropertyValue(id) is int[] a ? a : Array.Empty<int>(); } catch { return Array.Empty<int>(); }
    }
    static int[] Rect(IUIAutomationElement e)
    {
        try
        {
            if (e.GetCachedPropertyValue(Uia.BoundingRectangle) is double[] r && r.Length == 4)
                return new[] { (int)r[0], (int)r[1], (int)(r[0] + r[2]), (int)(r[1] + r[3]) }; // {l,t,w,h} -> {l,t,r,b}
        }
        catch { }
        return new[] { 0, 0, 0, 0 };
    }
}
