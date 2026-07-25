using System.Threading.Channels;
using JaatoBridge.Transport;

namespace JaatoBridge;

/// <summary>Thrown by a verb handler to produce an <c>ok:false</c> response with a §7 code.</summary>
public sealed class ProtoException(string code, string message, int? retryAfterMs = null) : Exception(message)
{
    public string Code { get; } = code;
    public int? RetryAfterMs { get; } = retryAfterMs;
}

/// <summary>
/// §8 router: a single-consumer command queue so verb handlers never overlap (mirrors the Android
/// single-consumer queue). Each request yields exactly one response (§3.2); handlers may additionally
/// emit binary frames / events through the <see cref="WsClient"/> directly.
/// </summary>
public sealed class CommandRouter
{
    public delegate Task<object> Handler(ReqFrame req, CancellationToken ct);

    readonly WsClient _ws;
    readonly Dictionary<string, Handler> _handlers = new(StringComparer.Ordinal);
    readonly Channel<ReqFrame> _queue = Channel.CreateUnbounded<ReqFrame>(new UnboundedChannelOptions { SingleReader = true });

    public CommandRouter(WsClient ws) => _ws = ws;

    public void Register(string verb, Handler h) => _handlers[verb] = h;

    public void Enqueue(ReqFrame req) => _queue.Writer.TryWrite(req);

    public async Task RunAsync(CancellationToken ct)
    {
        await foreach (var req in _queue.Reader.ReadAllAsync(ct).ConfigureAwait(false))
        {
            string response;
            try
            {
                if (!_handlers.TryGetValue(req.Verb, out var h))
                    response = Wire.ResError(req.Id, Err.Internal, $"unknown verb '{req.Verb}'");
                else
                {
                    // §10: time-box every handler so a wedged UIA call yields TIMEOUT, not a hung pump.
                    // waitForSettle self-bounds via its own hardTimeout, so it is exempt.
                    var task = h(req, ct);
                    var data = req.Verb == "waitForSettle"
                        ? await task.ConfigureAwait(false)
                        : await task.WaitAsync(TimeSpan.FromSeconds(30), ct).ConfigureAwait(false);
                    response = Wire.Res(req.Id, data);
                }
            }
            catch (ProtoException pe) { response = Wire.ResError(req.Id, pe.Code, pe.Message, pe.RetryAfterMs); }
            catch (TimeoutException) { response = Wire.ResError(req.Id, Err.Timeout, $"'{req.Verb}' exceeded its time bound"); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                Log.Err($"handler '{req.Verb}' threw: {ex}");
                response = Wire.ResError(req.Id, Err.Internal, $"{ex.GetType().Name}: {ex.Message}");
            }
            await _ws.SendTextAsync(response, ct).ConfigureAwait(false);
        }
    }
}
