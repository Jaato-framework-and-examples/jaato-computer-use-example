using System.Text.Json.Nodes;
using JaatoBridge;
using JaatoBridge.Act;
using JaatoBridge.Observe;
using JaatoBridge.Platform;
using JaatoBridge.Shot;
using JaatoBridge.State;
using JaatoBridge.Transport;

// ── configuration ──────────────────────────────────────────────────────────────────────────────
// Device dials OUT to the daemon (§2). Local dev uses ws://; production is wss:// over the VPN (§13).
// Endpoint precedence: an explicit non-flag CLI arg wins (and is persisted), else the saved tray
// settings, else JAATO_DAEMON_URL, else the dev default. Token is only ever set via tray Settings.
string? cliUrl = args.FirstOrDefault(a => !a.StartsWith("--"));
string? envUrl = Environment.GetEnvironmentVariable("JAATO_DAEMON_URL");

// ── M4 self-test: capture the foreground window to a PNG and exit (de-risks WGC+Vortice readback) ──
// Checked before parsing the URL so `--selftest-shot` works with no daemon arg.
if (args.Contains("--selftest-shot"))
{
    var hwnd = WinApi.GetForegroundWindow();
    using var cap = new JaatoBridge.Shot.ScreenCapturer();
    var f = cap.CaptureWindow(hwnd);
    var enc = JaatoBridge.Shot.ImageOut.Encode(f.Bgra, f.Width, f.Height, null, 1280, "png");
    var outPath = Path.Combine(Path.GetTempPath(), "jaato_selftest.png");
    File.WriteAllBytes(outPath, enc.Bytes);
    Console.WriteLine($"selftest: hwnd=0x{hwnd.ToInt64():X} '{WinApi.Title(hwnd)}' captured {f.Width}x{f.Height} → {enc.Format} {enc.Width}x{enc.Height} {enc.Bytes.Length}B → {outPath}");
    return;
}

// Diagnostic: time a per-window capture of the current foreground (open Start first to hit SearchHost)
// then the monitor fallback, reporting each — verifies the Start-surface fallback works and how slow it is.
if (args.Contains("--selftest-monitor"))
{
    Console.WriteLine("selftest-monitor: you have 4s to open Start (Ctrl+Esc) to put SearchHost in foreground...");
    Thread.Sleep(4000);
    var hwnd = WinApi.GetForegroundWindow();
    var cls = new System.Text.StringBuilder(256); WinApi.GetClassNameW(hwnd, cls, 256);
    Console.WriteLine($"foreground: '{WinApi.Title(hwnd)}' class='{cls}'");
    using var cap = new JaatoBridge.Shot.ScreenCapturer();
    var sw = System.Diagnostics.Stopwatch.StartNew();
    try { var f = cap.CaptureWindow(hwnd); Console.WriteLine($"window capture OK {f.Width}x{f.Height} in {sw.ElapsedMilliseconds}ms (fg='{WinApi.Title(hwnd)}')"); }
    catch (Exception ex) { Console.WriteLine($"window capture FAILED in {sw.ElapsedMilliseconds}ms: {ex.GetType().Name}: {ex.Message} (fg='{WinApi.Title(hwnd)}')"); }
    sw.Restart();
    try
    {
        var m = cap.CaptureMonitor();
        var enc = JaatoBridge.Shot.ImageOut.Encode(m.Bgra, m.Width, m.Height, null, 1280, "png");
        var p = Path.Combine(Path.GetTempPath(), "jaato_monitor.png");
        File.WriteAllBytes(p, enc.Bytes);
        Console.WriteLine($"MONITOR capture OK {m.Width}x{m.Height} in {sw.ElapsedMilliseconds}ms → {p} ({enc.Bytes.Length}B)");
    }
    catch (Exception ex) { Console.WriteLine($"MONITOR capture FAILED in {sw.ElapsedMilliseconds}ms: {ex.GetType().Name}: {ex.Message}"); }
    return;
}

// Diagnostic: walk the foreground window and print nodes carrying scroll flags — verifies the
// position-aware scrollable/scrollableDown/Up/Left/Right emission on a real window (e.g. Notepad).
if (args.Contains("--selftest-observe"))
{
    // Target a top-level window whose title contains the substring after the flag (default "notepad"),
    // so we don't depend on foreground/focus. Prints nodes carrying scroll flags.
    int fi = Array.IndexOf(args, "--selftest-observe");
    string want = (fi >= 0 && fi + 1 < args.Length && !args[fi + 1].StartsWith("--")) ? args[fi + 1] : "notepad";
    var match = JaatoBridge.Observe.WindowLister.ListTopLevel()
        .FirstOrDefault(w => w.Title.Contains(want, StringComparison.OrdinalIgnoreCase));
    if (match is null) { Console.WriteLine($"no top-level window title contains '{want}'"); return; }
    var suia = new UiaSession();
    var el = suia.ElementFromHwnd(new IntPtr(match.Id));
    if (el is null) { Console.WriteLine("no UIA element for that window"); return; }
    var walk = new JaatoBridge.Observe.TreeWalker(suia).Walk(el);
    Console.WriteLine($"window '{match.Title}' — {walk.Nodes.Count} nodes; scroll-flagged:");
    foreach (var n in walk.Nodes.Where(n => n.Flags.Any(f => f.StartsWith("scroll"))))
        Console.WriteLine($"  [{n.Ref}] {n.Cls} '{n.Text}' <{string.Join(",", n.Flags)}>");
    return;
}

// Diagnostic: scroll a titled window's scrollable element to the bottom via UIA ScrollPattern (no focus
// needed), printing vertical percent before/after + the resulting scroll flags — validates the at-bottom case.
if (args.Contains("--selftest-scroll"))
{
    int fi = Array.IndexOf(args, "--selftest-scroll");
    string want = (fi + 1 < args.Length && !args[fi + 1].StartsWith("--")) ? args[fi + 1] : "notepad";
    var match = JaatoBridge.Observe.WindowLister.ListTopLevel().FirstOrDefault(w => w.Title.Contains(want, StringComparison.OrdinalIgnoreCase));
    if (match is null) { Console.WriteLine($"no window '{want}'"); return; }
    var suia = new UiaSession();
    var win = suia.ElementFromHwnd(new IntPtr(match.Id));
    var cond = suia.Automation.CreatePropertyCondition(Uia.IsScrollPatternAvailable, true);
    var scrollEl = win?.FindFirst(UIAutomationClient.TreeScope.TreeScope_Subtree, cond);
    if (scrollEl?.GetCurrentPattern(Uia.ScrollPattern) is UIAutomationClient.IUIAutomationScrollPattern sp)
    {
        Console.WriteLine($"before: vScrollable={sp.CurrentVerticallyScrollable} vPct={sp.CurrentVerticalScrollPercent:F1}");
        try { sp.SetScrollPercent(-1, 100); } catch (Exception ex) { Console.WriteLine($"SetScrollPercent: {ex.Message}"); }
        Thread.Sleep(400);
        Console.WriteLine($"after : vScrollable={sp.CurrentVerticallyScrollable} vPct={sp.CurrentVerticalScrollPercent:F1}");
    }
    else { Console.WriteLine("no scrollable element found"); return; }
    var walk = new JaatoBridge.Observe.TreeWalker(suia).Walk(win!);
    Console.WriteLine("scroll-flagged after scroll-to-bottom:");
    foreach (var n in walk.Nodes.Where(n => n.Flags.Any(f => f.StartsWith("scroll"))))
        Console.WriteLine($"  [{n.Ref}] {n.Cls} <{string.Join(",", n.Flags)}>");
    return;
}

// Diagnostic: dump the FrameworkId distribution of a titled window's subtree — confirms browser/HTML
// content reports FrameworkId "Chrome" (the signal Actuator.Click uses to route to a real mouse click).
if (args.Contains("--selftest-fw"))
{
    int fi = Array.IndexOf(args, "--selftest-fw");
    string want = (fi + 1 < args.Length && !args[fi + 1].StartsWith("--")) ? args[fi + 1] : "chrome";
    var match = JaatoBridge.Observe.WindowLister.ListTopLevel().FirstOrDefault(w => w.Title.Contains(want, StringComparison.OrdinalIgnoreCase));
    if (match is null) { Console.WriteLine($"no window '{want}'"); return; }
    var suia = new UiaSession();
    var win = suia.ElementFromHwnd(new IntPtr(match.Id));
    var cr = suia.Automation.CreateCacheRequest();
    cr.AddProperty(Uia.FrameworkId);
    var all = win!.FindAllBuildCache(UIAutomationClient.TreeScope.TreeScope_Subtree, suia.Automation.CreateTrueCondition(), cr);
    var fws = new Dictionary<string, int>();
    for (int i = 0; i < all.Length; i++)
    {
        var f = all.GetElement(i).GetCachedPropertyValue(Uia.FrameworkId) as string ?? "(null)";
        fws[f] = fws.GetValueOrDefault(f) + 1;
    }
    Console.WriteLine($"window '{match.Title}' FrameworkId distribution:");
    foreach (var kv in fws.OrderByDescending(k => k.Value)) Console.WriteLine($"  {kv.Value,5}  {kv.Key}");
    return;
}

// Diagnostic: exercise the browser-click fix — find an element by AutomationId in a titled window,
// report FrameworkId, real-click it (the fix path), and print node count before/after (did the menu open?).
if (args.Contains("--selftest-click"))
{
    int fi = Array.IndexOf(args, "--selftest-click");
    string winWant = fi + 1 < args.Length ? args[fi + 1] : "chrome";
    string aid = fi + 2 < args.Length ? args[fi + 2] : "";
    var match = JaatoBridge.Observe.WindowLister.ListTopLevel().FirstOrDefault(w => w.Title.Contains(winWant, StringComparison.OrdinalIgnoreCase));
    if (match is null) { Console.WriteLine($"no window '{winWant}'"); return; }
    WinApi.SetForegroundWindow(new IntPtr(match.Id)); Thread.Sleep(600);   // bring the target window up so the click lands
    var suia = new UiaSession();
    var win = suia.ElementFromHwnd(new IntPtr(match.Id));
    var before = new JaatoBridge.Observe.TreeWalker(suia).Walk(win!).Nodes;
    var target = before.FirstOrDefault(n => (n.Text ?? "").Contains(aid, StringComparison.OrdinalIgnoreCase));
    if (target is null) { Console.WriteLine($"no node whose text contains '{aid}'"); return; }
    int cx = (target.Bounds[0] + target.Bounds[2]) / 2, cy = (target.Bounds[1] + target.Bounds[3]) / 2;
    Console.WriteLine($"clicking [{target.Ref}] '{target.Text}' center=({cx},{cy})");
    JaatoBridge.Act.SyntheticInput.Click(cx, cy);  // the fix's action for browser content
    Thread.Sleep(1400);
    int after = new JaatoBridge.Observe.TreeWalker(suia).Walk(win!).Nodes.Count;
    Console.WriteLine($"pruned node count: before={before.Count} after={after}  ({(after > before.Count ? "MENU OPENED (+" + (after - before.Count) + " nodes)" : "no change")})");
    return;
}

// Single-instance guard (§3.1) — one bridge per interactive session.
using var singleInstance = new System.Threading.Mutex(true, @"Local\JaatoBridge", out bool createdNew);
if (!createdNew) { Log.Warn("another JaatoBridge instance is already running — exiting"); return; }

var settings = BridgeSettings.Load(new Uri(envUrl ?? "ws://127.0.0.1:8788/a11y"));
if (cliUrl is not null && Uri.TryCreate(cliUrl, UriKind.Absolute, out var cu) && (cu.Scheme == "ws" || cu.Scheme == "wss"))
    settings.Update(cu, settings.Token); // explicit CLI url wins and persists; token kept as saved

var state = new SessionState();
var clock = new SnapshotClock();
var store = new SnapshotStore();
var uia = new UiaSession();
var ws = new WsClient(settings.Uri, settings.Token);
var shots = new ScreenshotService(ws, state);
var observe = new ObserveService(uia, state, clock, store, shots);
var settle = new JaatoBridge.Settle.SettleService(uia, ws, clock, shots, observe);
var act = new ActService(uia, store, state, settle);
var router = new CommandRouter(ws);

// ── M1 verbs: configure + ping (observe/act/screenshot/settle land in later milestones) ──────────
router.Register("configure", (req, _) =>
{
    var cur = state.Current;
    var next = cur with
    {
        Settle = req.ArgObj("settle") is not null ? req.Arg<SettleConfig>("settle")! : cur.Settle,
        ScreenshotDefaults = req.ArgObj("screenshotDefaults") is not null ? req.Arg<ScreenshotDefaults>("screenshotDefaults")! : cur.ScreenshotDefaults,
        Redaction = req.ArgObj("redaction") is not null ? req.Arg<RedactionPolicy>("redaction")! : cur.Redaction,
        PackageScope = req.Args?["packageScope"] is not null ? req.Arg<string[]>("packageScope")! : cur.PackageScope,
        ObserveShellSurfaces = req.Args?["observeShellSurfaces"] is { } os ? os.GetValue<bool>() : cur.ObserveShellSurfaces,
    };
    state.Replace(next);
    Log.Info($"configure: scope=[{string.Join(",", next.PackageScope)}] quiet={next.Settle.QuietWindowMs}ms maskPw={next.Redaction.MaskPasswordNodes} observeShell={next.ObserveShellSurfaces}");
    return Task.FromResult<object>(new { applied = true });
});

router.Register("ping", (_, _) =>
    Task.FromResult<object>(new { t = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() }));

// ── M2 verbs: windows + observe (§4.1) ───────────────────────────────────────────────────────────
router.Register("windows", (_, _) => Task.FromResult(observe.ListWindows()));
router.Register("observe", async (req, _) => await observe.Observe(req));

// ── M3 verb: act (§5.3) with the mandatory integrity gate (§3.2) ──────────────────────────────────
router.Register("act", async (req, _) => act.Act(req));

// ── M4 verb: screenshot (§5.4) — standalone WGC capture → binary frame ─────────────────────────────
router.Register("screenshot", async (req, _) =>
{
    IntPtr hwnd = req.Args?["window"] is { } w ? new IntPtr(w.GetValue<long>())
                : (store.Latest is { Hwnd: var h } && h != IntPtr.Zero ? h : WinApi.GetForegroundWindow());
    var cur = state.Current.ScreenshotDefaults;
    var over = cur with
    {
        Format = (string?)req.Args?["format"] ?? cur.Format,
        Quality = (int?)req.Args?["quality"] ?? cur.Quality,
        MaxDimension = (int?)req.Args?["maxDimension"] ?? cur.MaxDimension,
        Crop = req.Args?["crop"] is JsonArray a ? a.Select(n => (int)n!.GetValue<double>()).ToArray() : cur.Crop,
    };
    return await shots.Standalone(hwnd, req.Id, observe.PasswordNodes(hwnd), over);
});

// ── M4b verbs: waitForSettle (§5.5) + cancel (§5.6) ────────────────────────────────────────────────
router.Register("waitForSettle", async (req, _) =>
{
    IntPtr hwnd = req.Args?["window"] is { } w ? new IntPtr(w.GetValue<long>())
                : (store.Latest is { Hwnd: var h } && h != IntPtr.Zero ? h : WinApi.GetForegroundWindow());
    var baseCfg = state.Current.Settle;
    var o = req.ArgObj("settle");
    var cfg = o is null ? baseCfg : baseCfg with
    {
        QuietWindowMs = (int?)o["quietWindowMs"] ?? baseCfg.QuietWindowMs,
        HardTimeoutMs = (int?)o["hardTimeoutMs"] ?? baseCfg.HardTimeoutMs,
        Mode = (string?)o["mode"] ?? baseCfg.Mode,
        MinEventCount = (int?)o["minEventCount"] ?? baseCfg.MinEventCount,
        BundleScreenshotOnSettle = (bool?)o["bundleScreenshotOnSettle"] ?? baseCfg.BundleScreenshotOnSettle,
        EventMask = o["eventMask"] is JsonArray a ? a.Select(n => (string)n!).ToArray() : baseCfg.EventMask,
    };
    return await settle.WaitForSettle(req.Id, hwnd, cfg);
});

router.Register("cancel", (req, _) => Task.FromResult(settle.Cancel(req.Arg<string>("target"))));

// ── hello on every (re)connect (§6.1) ────────────────────────────────────────────────────────────
ws.Connected += () =>
{
    var (sw, sh) = Native.PrimaryScreen();
    bool elevated = Native.OwnIntegrity() >= Native.Integrity.High;
    bool canCapture = shots.Available; // WGC capture path is wired (M4) — advertise honestly
    var hello = Wire.Event("hello", new
    {
        pv = Wire.ProtocolVersion,
        deviceId = Environment.MachineName,
        platform = "windows",
        osBuild = Environment.OSVersion.Version.Build,
        capabilities = new
        {
            takeScreenshot = canCapture,   // controller-facing name from §6.1
            canCaptureWindow = canCapture, // §9 Windows addition
            reportViewIds = true,          // UIA AutomationId
            isElevated = elevated,         // §9
            uiAccess = false,              // §9 — reference build is not a signed uiAccess binary
        },
        screen = new { width = sw, height = sh },
    });
    _ = ws.SendTextAsync(hello);
    Log.Info($"connected → {ws.Uri}; sent hello (elevated={elevated}, canCapture={canCapture}, screen={sw}x{sh})");
};

ws.Disconnected += () => Log.Warn("disconnected");
ws.TextReceived += text =>
{
    var req = Wire.TryParseReq(text);
    if (req is null) { Log.Warn($"ignoring non-req frame: {Trunc(text)}"); return; }
    router.Enqueue(req);
};

// ── run: router runs for the app lifetime; the tray owns connect/disconnect + quit (§3.1) ─────────
using var appCts = new CancellationTokenSource();
Log.Info($"JaatoBridge starting — daemon {settings.Uri}");
var routerTask = router.RunAsync(appCts.Token);

var host = new BridgeHost(ws);
host.Connect();

Console.CancelKeyPress += (_, e) => { e.Cancel = true; host.Disconnect(); appCts.Cancel(); Environment.Exit(0); };

// The tray runs its own STA message loop; joining it keeps the process alive until Quit.
var trayThread = new Thread(() => new JaatoBridge.Tray.TrayApp(host, ws, settings, () => appCts.Cancel()).Run())
{
    IsBackground = false,
    Name = "tray",
};
trayThread.SetApartmentState(ApartmentState.STA);
trayThread.Start();
trayThread.Join();

host.Disconnect();
appCts.Cancel();
try { await routerTask; } catch (OperationCanceledException) { }
Log.Info("JaatoBridge stopped");

static bool ProbeWgc()
{
    try { return Windows.Graphics.Capture.GraphicsCaptureSession.IsSupported(); }
    catch { return false; }
}

static string Trunc(string s) => s.Length <= 120 ? s : s[..120] + "…";
