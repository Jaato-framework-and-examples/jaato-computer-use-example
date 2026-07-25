// Local auth-check daemon — verifies the bridge sends the Authorization: Bearer <token> header on the
// WS upgrade (01 §13.1). Logs the header it received, accepts, then idles.

using System.Net;

string prefix = args.FirstOrDefault() ?? "http://127.0.0.1:8788/a11y/";
using var listener = new HttpListener();
listener.Prefixes.Add(prefix);
listener.Start();
Console.WriteLine($"[daemon] listening on {prefix}");
while (true)
{
    var ctx = await listener.GetContextAsync();
    string auth = ctx.Request.Headers["Authorization"] ?? "<none>";
    Console.WriteLine($"[daemon] upgrade from {ctx.Request.RemoteEndPoint} — Authorization: {auth}");
    if (!ctx.Request.IsWebSocketRequest) { ctx.Response.StatusCode = 400; ctx.Response.Close(); continue; }
    var wsCtx = await ctx.AcceptWebSocketAsync(null);
    Console.WriteLine("[daemon] device connected (accepted).");
    _ = Idle(wsCtx.WebSocket);
}

static async Task Idle(System.Net.WebSockets.WebSocket ws)
{
    var buf = new byte[1 << 16];
    try { while (ws.State == System.Net.WebSockets.WebSocketState.Open) await ws.ReceiveAsync(buf, CancellationToken.None); }
    catch { }
}
