using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace JaatoBridge.Transport;

/// <summary>Wire models & (de)serialization for §3 text frames. Single source of truth is 01-PROTOCOL.md.</summary>
public static class Wire
{
    public const int ProtocolVersion = 1;

    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>Parse an incoming text frame. Device only ever receives <c>kind:"req"</c> (§3.1).</summary>
    public static ReqFrame? TryParseReq(string text)
    {
        try
        {
            var node = JsonNode.Parse(text)?.AsObject();
            if (node is null) return null;
            var kind = (string?)node["kind"];
            if (kind != "req") return null;
            var id = (string?)node["id"];
            var verb = (string?)node["verb"];
            if (id is null || verb is null) return null;
            return new ReqFrame(id, verb, node["args"]?.AsObject());
        }
        catch { return null; }
    }

    public static string Res(string id, object data)
        => JsonSerializer.Serialize(new ResFrame { Id = id, Ok = true, Data = data }, Json);

    public static string ResError(string id, string code, string message, int? retryAfterMs = null)
        => JsonSerializer.Serialize(
            new ResFrame { Id = id, Ok = false, Error = new ProtoError { Code = code, Message = message, RetryAfterMs = retryAfterMs } },
            Json);

    public static string Event(string ev, object data)
        => JsonSerializer.Serialize(new EventFrame { Event = ev, Data = data }, Json);
}

public sealed record ReqFrame(string Id, string Verb, JsonObject? Args)
{
    public T? Arg<T>(string name)
    {
        if (Args is null || !Args.TryGetPropertyValue(name, out var v) || v is null) return default;
        return v.Deserialize<T>(Wire.Json);
    }
    public JsonObject? ArgObj(string name) => Args?[name]?.AsObject();
    public bool ArgBool(string name, bool dflt = false) => (bool?)(Args?[name]) ?? dflt;
}

public sealed class ResFrame
{
    public string Kind => "res";
    public string Id { get; init; } = "";
    public bool Ok { get; init; }
    public object? Data { get; init; }
    public ProtoError? Error { get; init; }
}

public sealed class EventFrame
{
    public string Kind => "event";
    public string Event { get; init; } = "";
    public object? Data { get; init; }
}

public sealed class ProtoError
{
    public string Code { get; init; } = "INTERNAL";
    public string Message { get; init; } = "";
    public int? RetryAfterMs { get; init; }
}

/// <summary>§7 error taxonomy — codes shared verbatim with the Android half.</summary>
public static class Err
{
    public const string NotFound = "NOT_FOUND";
    public const string Ambiguous = "AMBIGUOUS";
    public const string Stale = "STALE";
    public const string NotActionable = "NOT_ACTIONABLE";
    public const string RateLimited = "RATE_LIMITED";
    public const string SecureWindow = "SECURE_WINDOW";
    public const string Canceled = "CANCELED";
    public const string Timeout = "TIMEOUT";
    public const string ProtocolVersion = "PROTOCOL_VERSION";
    public const string Permission = "PERMISSION";
    public const string Internal = "INTERNAL";
}
