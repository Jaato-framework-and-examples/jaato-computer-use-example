using UIAutomationClient;

namespace JaatoBridge.Observe;

/// <summary>
/// Owns the raw <c>IUIAutomation</c> COM instance and the cache-request template. The whole design
/// hinges on controlling this cache request directly (§4.2), which is why we use raw COM, not a wrapper.
/// UIA access is serialized by the router's single-consumer queue, so no extra locking here.
/// </summary>
public sealed class UiaSession
{
    public IUIAutomation Automation { get; } = new CUIAutomation();

    public IUIAutomationCondition ContentView => Automation.ContentViewCondition;

    /// <summary>
    /// Cache request for a whole-window subtree walk. TreeScope_Subtree here is applied to a SINGLE
    /// root via BuildUpdatedCache (O(N), caches the subtree once) — NOT via FindAllBuildCache, which
    /// would return N roots and re-cache each subtree (the O(N²) footgun in §4.2). Filtered to the
    /// Content view; Full element mode keeps RuntimeId for the ref→RuntimeId table (measured free, §12.2).
    /// </summary>
    public IUIAutomationCacheRequest BuildTreeCacheRequest()
    {
        var cr = Automation.CreateCacheRequest();
        cr.TreeScope = TreeScope.TreeScope_Subtree;
        cr.TreeFilter = ContentView;
        cr.AutomationElementMode = AutomationElementMode.AutomationElementMode_Full;
        foreach (var p in Uia.AllProps) cr.AddProperty(p);
        foreach (var pat in Uia.AllPatterns) cr.AddPattern(pat);
        return cr;
    }

    public IUIAutomationElement? ElementFromHwnd(IntPtr hwnd)
    {
        try { return Automation.ElementFromHandle(hwnd); }
        catch { return null; }
    }
}
