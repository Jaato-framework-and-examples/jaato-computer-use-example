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
