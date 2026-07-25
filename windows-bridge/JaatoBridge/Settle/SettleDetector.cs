using System.Runtime.InteropServices;
using JaatoBridge.Observe;
using JaatoBridge.State;
using UIAutomationClient;

namespace JaatoBridge.Settle;

/// <summary>
/// §6 settle detector — the debounce state machine, unchanged in shape from Android; only the event
/// source differs. Fully parameterised by daemon-pushed <see cref="SettleConfig"/>. UIA event handlers
/// are scoped to the target window's subtree (never desktop-wide, §6 hazard 1). Each qualifying event
/// resets the quiet timer; quiet → settled(quiet), hard bound → settled(timeout).
/// </summary>
public sealed class SettleDetector
{
    readonly UiaSession _uia;
    public SettleDetector(UiaSession uia) => _uia = uia;

    public sealed record Result(string Reason);

    public async Task<Result> ArmAsync(IntPtr hwnd, SettleConfig cfg, CancellationToken ct)
    {
        var a = _uia.Automation;
        var window = _uia.ElementFromHwnd(hwnd);
        if (window is null) return new Result("timeout"); // window gone → nothing to settle

        var sem = new SemaphoreSlim(0);
        int count = 0;
        var sink = new EventSink(() => { Interlocked.Increment(ref count); try { sem.Release(); } catch { } });

        var mask = new HashSet<string>(cfg.EventMask, StringComparer.OrdinalIgnoreCase);
        Subscribe(a, window, mask, sink);
        try
        {
            long start = Environment.TickCount64;
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                long remainingHard = cfg.HardTimeoutMs - (Environment.TickCount64 - start);
                if (remainingHard <= 0) return new Result("timeout");
                int wait = (int)Math.Min(cfg.QuietWindowMs, remainingHard);

                bool got = await sem.WaitAsync(wait, ct).ConfigureAwait(false);
                if (!got)
                {
                    // Quiet window elapsed with no qualifying event.
                    bool minMet = cfg.Mode != "minEventsThenQuiet" || Volatile.Read(ref count) >= cfg.MinEventCount;
                    if (minMet) return new Result("quiet");
                    continue; // minEventsThenQuiet: keep waiting for the required events (bounded by hardTimeout)
                }
                while (sem.Wait(0)) { } // burst → one reset
            }
        }
        finally
        {
            try { a.RemoveAllEventHandlers(); } catch { }
            GC.KeepAlive(sink);
        }
    }

    static void Subscribe(IUIAutomation a, IUIAutomationElement window, HashSet<string> mask, EventSink sink)
    {
        const TreeScope sub = TreeScope.TreeScope_Subtree;
        if (mask.Contains("WINDOW_CONTENT_CHANGED"))
            TryDo(() => a.AddStructureChangedEventHandler(window, sub, null, sink));
        if (mask.Contains("WINDOW_STATE_CHANGED"))
        {
            TryDo(() => a.AddAutomationEventHandler(20016 /*Window_WindowOpened*/, window, sub, null, sink));
            TryDo(() => a.AddAutomationEventHandler(20017 /*Window_WindowClosed*/, window, sub, null, sink));
        }
        if (mask.Contains("VIEW_FOCUSED"))
            TryDo(() => a.AddFocusChangedEventHandler(null, sink));
        if (mask.Contains("VIEW_SCROLLED"))
            TryDo(() => a.AddPropertyChangedEventHandler(window, sub, null, sink, new[] { 30055, 30053 }));
        if (mask.Contains("VIEW_TEXT_CHANGED"))
            TryDo(() => a.AddPropertyChangedEventHandler(window, sub, null, sink, new[] { Uia.ValueValue }));
    }

    static void TryDo(Action x) { try { x(); } catch (Exception ex) { Log.Warn($"settle subscribe: {ex.Message}"); } }

    /// <summary>One COM sink implementing every UIA handler interface; all events funnel to one callback.</summary>
    [ComVisible(true)]
    sealed class EventSink :
        IUIAutomationStructureChangedEventHandler,
        IUIAutomationFocusChangedEventHandler,
        IUIAutomationEventHandler,
        IUIAutomationPropertyChangedEventHandler
    {
        readonly Action _on;
        public EventSink(Action on) => _on = on;
        public void HandleStructureChangedEvent(IUIAutomationElement sender, StructureChangeType changeType, int[] runtimeId) => _on();
        public void HandleFocusChangedEvent(IUIAutomationElement sender) => _on();
        public void HandleAutomationEvent(IUIAutomationElement sender, int eventId) => _on();
        public void HandlePropertyChangedEvent(IUIAutomationElement sender, int propertyId, object newValue) => _on();
    }
}
