using System.Net.WebSockets;
using System.Text;

namespace JaatoBridge.Transport;

/// <summary>
/// §2 transport: the device dials <em>out</em> to the daemon (daemon never initiates). One socket,
/// text + binary frames, WS keepalive, exponential-backoff reconnect. No app state survives a reconnect.
/// </summary>
public sealed class WsClient
{
    volatile Uri _uri;
    volatile string? _token;
    readonly SemaphoreSlim _sendLock = new(1, 1);
    ClientWebSocket? _ws;

    public event Action? Connected;
    public event Action? Disconnected;
    public event Action<string>? TextReceived;

    public WsClient(Uri uri, string? token = null) { _uri = uri; _token = token; }

    public Uri Uri => _uri;

    /// <summary>
    /// Swap the daemon endpoint + bearer token. Read fresh at the top of every dial attempt, so the
    /// tray's "Settings" flow (disconnect → set → connect) picks these up with no restart.
    /// </summary>
    public void SetEndpoint(Uri uri, string? token) { _uri = uri; _token = token; }

    public bool IsOpen => _ws?.State == WebSocketState.Open;

    /// <summary>Connect-receive-reconnect loop until cancelled. Raises Connected/Disconnected around each session.</summary>
    public async Task RunAsync(CancellationToken ct)
    {
        var backoff = TimeSpan.FromSeconds(1);
        var maxBackoff = TimeSpan.FromSeconds(30);
        var rng = new Random();

        while (!ct.IsCancellationRequested)
        {
            var uri = _uri;
            var token = _token;
            try
            {
                _ws = new ClientWebSocket();
                _ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(15); // §2 keepalive
                // §13.1 device-bound auth: bearer token on the WS upgrade (daemon strips "Bearer ").
                // Harmless against an unsafe_no_auth daemon (the header is ignored).
                if (!string.IsNullOrEmpty(token))
                    _ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
                await _ws.ConnectAsync(uri, ct).ConfigureAwait(false);
                backoff = TimeSpan.FromSeconds(1);
                Connected?.Invoke();
                await ReceiveLoopAsync(_ws, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex) { Log.Warn($"ws session ended: {ex.GetType().Name}: {ex.Message}"); }
            finally
            {
                Disconnected?.Invoke();
                try { _ws?.Dispose(); } catch { }
                _ws = null;
            }

            if (ct.IsCancellationRequested) break;
            var jitter = TimeSpan.FromMilliseconds(rng.Next(0, 1000));
            var wait = backoff + jitter;
            Log.Info($"reconnecting in {wait.TotalSeconds:F1}s");
            try { await Task.Delay(wait, ct).ConfigureAwait(false); } catch (OperationCanceledException) { break; }
            backoff = TimeSpan.FromMilliseconds(Math.Min(maxBackoff.TotalMilliseconds, backoff.TotalMilliseconds * 2));
        }
    }

    async Task ReceiveLoopAsync(ClientWebSocket ws, CancellationToken ct)
    {
        var buf = new byte[64 * 1024];
        var acc = new MemoryStream();
        while (!ct.IsCancellationRequested && ws.State == WebSocketState.Open)
        {
            acc.SetLength(0);
            WebSocketReceiveResult res;
            do
            {
                res = await ws.ReceiveAsync(new ArraySegment<byte>(buf), ct).ConfigureAwait(false);
                if (res.MessageType == WebSocketMessageType.Close)
                {
                    await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, null, ct).ConfigureAwait(false);
                    return;
                }
                acc.Write(buf, 0, res.Count);
            } while (!res.EndOfMessage);

            // Daemon→device is text-only per §2; ignore any binary the daemon sends.
            if (res.MessageType == WebSocketMessageType.Text)
            {
                var text = Encoding.UTF8.GetString(acc.GetBuffer(), 0, (int)acc.Length);
                TextReceived?.Invoke(text);
            }
        }
    }

    public async Task SendTextAsync(string json, CancellationToken ct = default)
    {
        var ws = _ws;
        if (ws is null || ws.State != WebSocketState.Open) return;
        var bytes = Encoding.UTF8.GetBytes(json);
        await _sendLock.WaitAsync(ct).ConfigureAwait(false);
        try { await ws.SendAsync(bytes, WebSocketMessageType.Text, true, ct).ConfigureAwait(false); }
        finally { _sendLock.Release(); }
    }

    public async Task SendBinaryAsync(byte[] frame, CancellationToken ct = default)
    {
        var ws = _ws;
        if (ws is null || ws.State != WebSocketState.Open) return;
        await _sendLock.WaitAsync(ct).ConfigureAwait(false);
        try { await ws.SendAsync(frame, WebSocketMessageType.Binary, true, ct).ConfigureAwait(false); }
        finally { _sendLock.Release(); }
    }
}
