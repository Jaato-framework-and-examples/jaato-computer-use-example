using System.Drawing;
using System.Windows.Forms;
using JaatoBridge.Platform;
using JaatoBridge.State;
using JaatoBridge.Transport;

namespace JaatoBridge.Tray;

/// <summary>
/// §3.1 the standing transparency signal: a persistent tray icon whenever the bridge is live, plus the
/// explicit CONNECT / DISCONNECT kill switch, a Settings dialog for the daemon URL + bearer token, and a
/// logon-autostart toggle. Runs its own STA message loop (UIA/COM work stays on MTA pool threads).
/// </summary>
public sealed class TrayApp
{
    readonly BridgeHost _host;
    readonly WsClient _ws;
    readonly BridgeSettings _settings;
    readonly Action _onQuit;

    NotifyIcon _icon = null!;
    ToolStripMenuItem _status = null!, _connect = null!, _autostart = null!;
    System.Windows.Forms.Timer _timer = null!;
    Icon _iconOn = null!, _iconOff = null!;

    public TrayApp(BridgeHost host, WsClient ws, BridgeSettings settings, Action onQuit)
    {
        _host = host;
        _ws = ws;
        _settings = settings;
        _onQuit = onQuit;
    }

    public void Run()
    {
        Application.EnableVisualStyles();
        _iconOn = MakeDot(Color.LimeGreen);
        _iconOff = MakeDot(Color.Gray);

        _icon = new NotifyIcon { Visible = true, Icon = _iconOff, Text = "JaatoBridge" };
        var menu = new ContextMenuStrip();
        _status = new ToolStripMenuItem("starting…") { Enabled = false };
        _connect = new ToolStripMenuItem("Disconnect", null, (_, _) => Toggle());
        var settings = new ToolStripMenuItem("Settings…", null, (_, _) => ShowSettings());
        _autostart = new ToolStripMenuItem("Start at logon", null, (_, _) => ToggleAutostart()) { Checked = Autostart.IsEnabled() };
        var quit = new ToolStripMenuItem("Quit", null, (_, _) => Quit());
        menu.Items.Add(_status);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_connect);
        menu.Items.Add(settings);
        menu.Items.Add(_autostart);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(quit);
        _icon.ContextMenuStrip = menu;
        _icon.DoubleClick += (_, _) => Toggle();

        _timer = new System.Windows.Forms.Timer { Interval = 1000 };
        _timer.Tick += (_, _) => UpdateUi();
        _timer.Start();
        UpdateUi();

        Application.Run();

        _icon.Visible = false;
        _icon.Dispose();
        _iconOn.Dispose();
        _iconOff.Dispose();
    }

    void Toggle()
    {
        if (_host.IsActive) _host.Disconnect(); else _host.Connect();
        UpdateUi();
    }

    /// <summary>Modal editor for the daemon URL (ws:// or wss://) + optional bearer token (01 §13).</summary>
    void ShowSettings()
    {
        using var dlg = new SettingsDialog(_settings.Uri.ToString(), _settings.Token);
        if (dlg.ShowDialog() != DialogResult.OK) return;

        _settings.Update(dlg.Url, dlg.Token);          // persists (token DPAPI-encrypted at rest)
        _ws.SetEndpoint(dlg.Url, dlg.Token);           // active endpoint for the next dial
        _host.Reconnect();                              // apply immediately
        UpdateUi();
        _icon.ShowBalloonTip(3000, "JaatoBridge",
            $"Reconnecting to {dlg.Url.Host}:{dlg.Url.Port}" + (dlg.Token is null ? "" : " (authenticated)"),
            ToolTipIcon.Info);
    }

    void ToggleAutostart()
    {
        bool enable = !Autostart.IsEnabled();
        var exe = Environment.ProcessPath ?? "";
        // No URL baked into the command line — the exe loads the saved settings.json on launch.
        Autostart.Set(enable, $"\"{exe}\"");
        _autostart.Checked = Autostart.IsEnabled();
    }

    void Quit()
    {
        _host.Disconnect();
        _onQuit();
        Application.ExitThread();
    }

    void UpdateUi()
    {
        bool open = _host.IsOpen;
        bool active = _host.IsActive;
        var uri = _settings.Uri;
        bool secure = uri.Scheme == "wss";
        _icon.Icon = open ? _iconOn : _iconOff;
        string state = open ? "connected" : active ? "connecting…" : "disconnected";
        _icon.Text = $"JaatoBridge — {state}";
        string auth = _settings.Token is null ? "" : secure ? " 🔒" : " (token)";
        _status.Text = open ? $"Connected → {uri.Host}:{uri.Port}{auth}"
                     : active ? $"Connecting → {uri.Host}:{uri.Port}…"
                     : "Disconnected";
        _connect.Text = active ? "Disconnect" : "Connect";
    }

    static Icon MakeDot(Color c)
    {
        using var bmp = new Bitmap(16, 16);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);
            using var b = new SolidBrush(c);
            g.FillEllipse(b, 2, 2, 12, 12);
        }
        return Icon.FromHandle(bmp.GetHicon());
    }
}

/// <summary>Small two-field modal: daemon URL + optional token, with validation on OK.</summary>
file sealed class SettingsDialog : Form
{
    readonly TextBox _url;
    readonly TextBox _token;

    public Uri Url { get; private set; } = null!;
    public string? Token { get; private set; }

    public SettingsDialog(string url, string? token)
    {
        Text = "JaatoBridge — Connection";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(420, 170);

        var urlLabel = new Label { Text = "Daemon URL (ws:// or wss://)", Left = 12, Top = 14, Width = 396 };
        _url = new TextBox { Left = 12, Top = 34, Width = 396, Text = url };

        var tokenLabel = new Label { Text = "Bearer token (optional — required for wss://)", Left = 12, Top = 66, Width = 396 };
        _token = new TextBox { Left = 12, Top = 86, Width = 300, Text = token ?? "", UseSystemPasswordChar = true };
        var show = new CheckBox { Text = "Show", Left = 320, Top = 86, Width = 88 };
        show.CheckedChanged += (_, _) => _token.UseSystemPasswordChar = !show.Checked;

        var ok = new Button { Text = "Save & Connect", Left = 232, Top = 128, Width = 176, DialogResult = DialogResult.None };
        var cancel = new Button { Text = "Cancel", Left = 152, Top = 128, Width = 72, DialogResult = DialogResult.Cancel };
        ok.Click += (_, _) => OnOk();

        Controls.AddRange(new Control[] { urlLabel, _url, tokenLabel, _token, show, ok, cancel });
        AcceptButton = ok;
        CancelButton = cancel;
    }

    void OnOk()
    {
        var text = _url.Text.Trim();
        if (!Uri.TryCreate(text, UriKind.Absolute, out var uri) || (uri.Scheme != "ws" && uri.Scheme != "wss"))
        {
            MessageBox.Show(this, "Enter a valid ws:// or wss:// URL.", "Invalid URL",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        var tok = _token.Text.Trim();
        if (uri.Scheme == "wss" && tok.Length == 0 &&
            MessageBox.Show(this, "wss:// with no token — the daemon will reject an unauthenticated upgrade unless it runs in no-auth mode. Continue anyway?",
                "No token", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            return;

        Url = uri;
        Token = tok.Length == 0 ? null : tok;
        DialogResult = DialogResult.OK;
        Close();
    }
}
