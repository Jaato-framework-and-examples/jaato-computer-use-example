using JaatoBridge.Transport;

namespace JaatoBridge;

/// <summary>
/// Owns the outbound-connection lifecycle so the tray's CONNECT / DISCONNECT kill switch (§3.1) can
/// stop and restart it. The command router runs for the whole app lifetime; only the WS dial loop is
/// toggled here (Disconnect cancels reconnects too — a hard, user-visible off).
/// </summary>
public sealed class BridgeHost
{
    readonly WsClient _ws;
    CancellationTokenSource? _cts;
    Task? _task;

    public BridgeHost(WsClient ws) => _ws = ws;

    public bool IsActive => _task is { IsCompleted: false };
    public bool IsOpen => _ws.IsOpen;

    public void Connect()
    {
        if (IsActive) return;
        _cts = new CancellationTokenSource();
        _task = _ws.RunAsync(_cts.Token);
        Log.Info("CONNECT");
    }

    public void Disconnect()
    {
        if (!IsActive) return;
        _cts?.Cancel();
        Log.Info("DISCONNECT");
    }

    /// <summary>
    /// Restart the dial loop so a just-changed endpoint/token (tray Settings) takes effect: cancel the
    /// current loop, let it unwind, then reconnect. Fire-and-forget from the tray's UI thread.
    /// </summary>
    public async void Reconnect()
    {
        _cts?.Cancel();
        var old = _task;
        if (old is not null) { try { await old.ConfigureAwait(true); } catch { } }
        _cts = new CancellationTokenSource();
        _task = _ws.RunAsync(_cts.Token);
        Log.Info("RECONNECT (settings changed)");
    }
}
