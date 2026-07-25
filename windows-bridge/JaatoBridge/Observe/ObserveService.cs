using JaatoBridge.Platform;
using JaatoBridge.Shot;
using JaatoBridge.State;
using JaatoBridge.Transport;

namespace JaatoBridge.Observe;

/// <summary>Glue for the <c>windows</c> and <c>observe</c> verbs (§4.1).</summary>
public sealed class ObserveService
{
    readonly UiaSession _uia;
    readonly TreeWalker _walker;
    readonly SessionState _state;
    readonly SnapshotClock _clock;
    readonly SnapshotStore _store;
    readonly ScreenshotService _shots;

    public ObserveService(UiaSession uia, SessionState state, SnapshotClock clock, SnapshotStore store, ScreenshotService shots)
    {
        _uia = uia;
        _walker = new TreeWalker(uia);
        _state = state;
        _clock = clock;
        _store = store;
        _shots = shots;
    }

    /// <summary>Password-flagged nodes of a window, for redaction of a standalone <c>screenshot</c>.</summary>
    public IReadOnlyList<NodeSnap> PasswordNodes(IntPtr hwnd)
    {
        var el = _uia.ElementFromHwnd(hwnd);
        if (el is null) return Array.Empty<NodeSnap>();
        return _walker.Walk(el).Nodes.Where(n => n.Flags.Contains("password")).ToList();
    }

    /// <summary><c>windows</c> — enumerate top-level windows, metadata only, non-scope-gated (§4.1).</summary>
    public object ListWindows()
    {
        var wins = WindowLister.ListTopLevel();
        return new { windows = wins };
    }

    /// <summary><c>observe { window?, includeScreenshot? }</c> — pruned tree of that window (§4.1).</summary>
    public async Task<object> Observe(ReqFrame req)
    {
        long? reqWindow = req.Args?["window"] is { } w ? (long?)w.GetValue<long>() : null;
        IntPtr hwnd = reqWindow is { } id ? new IntPtr(id) : WinApi.GetForegroundWindow();
        if (hwnd == IntPtr.Zero) throw new ProtoException(Err.NotFound, "no target window");

        var info = WindowLister.Describe(hwnd);
        var cfg = _state.Current;
        var (scrW, scrH) = Native.PrimaryScreen();
        long version = _clock.Next();

        // Fail-closed scope gate (§8/§13): out-of-scope windows serialize to nothing.
        if (!WindowLister.InScope(info, cfg.PackageScope, cfg.ObserveShellSurfaces))
        {
            Log.Info($"observe hwnd=0x{hwnd.ToInt64():X} '{info.Title}' OUT OF SCOPE → empty tree");
            _store.Set(version, hwnd, new Dictionary<int, int[]>());
            return new Snapshot { SnapshotVersion = version, Pkg = WindowLister.Pkg(info), Window = info, Screen = new ScreenInfo { Width = scrW, Height = scrH } };
        }

        var el = _uia.ElementFromHwnd(hwnd);
        if (el is null) throw new ProtoException(Err.NotFound, $"window 0x{hwnd.ToInt64():X} not resolvable via UIA");

        var walk = _walker.Walk(el);
        _store.Set(version, hwnd, walk.RefTable);
        Log.Info($"observe hwnd=0x{hwnd.ToInt64():X} '{Trunc(info.Title)}' → {walk.Nodes.Count} nodes in {walk.WalkMs:F0}ms (v{version})");

        string? screenshotRef = null;
        if (req.ArgBool("includeScreenshot"))
        {
            var pw = walk.Nodes.Where(n => n.Flags.Contains("password")).ToList();
            if (await _shots.Bundled(hwnd, version, req.Id, pw)) screenshotRef = req.Id;
        }

        return new Snapshot
        {
            SnapshotVersion = version,
            Pkg = WindowLister.Pkg(info),
            Window = info,
            Screen = new ScreenInfo { Width = scrW, Height = scrH },
            ScreenshotRef = screenshotRef,
            Nodes = walk.Nodes,
        };
    }

    static string Trunc(string s) => s.Length <= 60 ? s : s[..60] + "…";
}
