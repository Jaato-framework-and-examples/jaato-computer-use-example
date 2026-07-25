using JaatoBridge.Observe;
using JaatoBridge.State;
using JaatoBridge.Transport;
using UIAutomationClient;

namespace JaatoBridge.Act;

/// <summary>
/// §10 mechanical resolution against the <em>current</em> tree of the target window:
/// <c>{ref,snapshotVersion}</c> → <c>viewId</c> → <c>text</c>/<c>desc</c> → <c>bounds</c>.
/// Zero matches NOT_FOUND; multiple without a disambiguator AMBIGUOUS; stale version STALE.
/// The device never guesses which of several matches was meant.
/// </summary>
public sealed class Resolver
{
    readonly UiaSession _uia;
    readonly SnapshotStore _store;
    public Resolver(UiaSession uia, SnapshotStore store) { _uia = uia; _store = store; }

    public sealed record Match(IUIAutomationElement Element, string MatchedBy, int? MatchedRef, IntPtr Hwnd);

    public Match Resolve(Selector sel, IntPtr defaultHwnd)
    {
        var a = _uia.Automation;

        // 1) {ref, snapshotVersion} — tightest binding, resolved by RuntimeId identity (§4.3).
        if (sel.HasRef)
        {
            var entry = _store.Latest;
            if (entry is null || entry.Version != sel.SnapshotVersion)
                throw new ProtoException(Err.Stale, $"snapshotVersion {sel.SnapshotVersion} is not current ({entry?.Version.ToString() ?? "none"})");
            if (!entry.RefTable.TryGetValue(sel.Ref!.Value, out var rid))
                throw new ProtoException(Err.Stale, $"ref {sel.Ref} not present in version {entry.Version}");

            var win = _uia.ElementFromHwnd(entry.Hwnd) ?? throw new ProtoException(Err.NotFound, "target window gone");
            var cond = a.CreatePropertyCondition(Uia.RuntimeId, rid);
            var el = win.FindFirst(TreeScope.TreeScope_Subtree, cond)
                     ?? throw new ProtoException(Err.NotFound, $"ref {sel.Ref} (RuntimeId) not on current tree");
            return new Match(el, "ref", sel.Ref, entry.Hwnd);
        }

        var hwnd = defaultHwnd;
        var window = _uia.ElementFromHwnd(hwnd) ?? throw new ProtoException(Err.NotFound, "no target window");

        // 2) viewId (+ optional text/index disambiguator)
        if (!string.IsNullOrEmpty(sel.ViewId))
            return Pick(window, hwnd, a.CreatePropertyCondition(Uia.AutomationId, sel.ViewId), "viewId", sel);

        // 3) text / desc
        if (!string.IsNullOrEmpty(sel.Text))
            return Pick(window, hwnd, a.CreatePropertyCondition(Uia.Name, sel.Text), "text", sel);
        if (!string.IsNullOrEmpty(sel.Desc))
            return Pick(window, hwnd, a.CreatePropertyCondition(Uia.HelpText, sel.Desc), "desc", sel);

        // 4) bounds — last resort: hit-test the element at the bounds centre
        if (sel.Bounds is { Length: 4 } b)
        {
            var pt = new tagPOINT { x = (b[0] + b[2]) / 2, y = (b[1] + b[3]) / 2 };
            var el = a.ElementFromPoint(pt) ?? throw new ProtoException(Err.NotFound, "no element at bounds centre");
            return new Match(el, "bounds", null, hwnd);
        }

        throw new ProtoException(Err.NotFound, "empty selector");
    }

    Match Pick(IUIAutomationElement window, IntPtr hwnd, IUIAutomationCondition cond, string by, Selector sel)
    {
        var arr = window.FindAll(TreeScope.TreeScope_Subtree, cond);
        int n = arr?.Length ?? 0;
        if (n == 0) throw new ProtoException(Err.NotFound, $"{by} matched nothing");

        // Secondary text disambiguator on viewId matches.
        if (by == "viewId" && !string.IsNullOrEmpty(sel.Text))
        {
            var filtered = new List<IUIAutomationElement>();
            for (int i = 0; i < n; i++)
            {
                var e = arr!.GetElement(i);
                if (string.Equals(e.CurrentName, sel.Text, StringComparison.Ordinal)) filtered.Add(e);
            }
            if (filtered.Count == 0) throw new ProtoException(Err.NotFound, "viewId+text matched nothing");
            if (filtered.Count > 1 && sel.Index is null) throw new ProtoException(Err.Ambiguous, $"{filtered.Count} matches; provide index");
            return new Match(filtered[sel.Index ?? 0], by, null, hwnd);
        }

        if (n > 1 && sel.Index is null) throw new ProtoException(Err.Ambiguous, $"{n} matches for {by}; provide index");
        int idx = sel.Index ?? 0;
        if (idx < 0 || idx >= n) throw new ProtoException(Err.NotFound, $"index {idx} out of range ({n})");
        return new Match(arr!.GetElement(idx), by, null, hwnd);
    }
}
