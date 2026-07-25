using System.Buffers.Binary;
using System.Text;
using System.Text.Json;

namespace JaatoBridge.Transport;

/// <summary>
/// §4 binary framing: [4-byte BE headerLen][UTF-8 JSON header][raw blob].
/// The device only ever <em>builds</em> these (screenshots); daemon→device is text-only.
/// </summary>
public static class BinaryFrame
{
    public static byte[] Build(object header, ReadOnlySpan<byte> payload)
    {
        byte[] headerBytes = JsonSerializer.SerializeToUtf8Bytes(header, Wire.Json);
        var frame = new byte[4 + headerBytes.Length + payload.Length];
        BinaryPrimitives.WriteUInt32BigEndian(frame.AsSpan(0, 4), (uint)headerBytes.Length);
        headerBytes.CopyTo(frame.AsSpan(4));
        payload.CopyTo(frame.AsSpan(4 + headerBytes.Length));
        return frame;
    }
}

/// <summary>Header schema for a screenshot binary frame (§4).</summary>
public sealed class ScreenshotHeader
{
    public string Type => "screenshot";
    public string CorrelationId { get; init; } = "";
    public long SnapshotVersion { get; init; }
    public string Format { get; init; } = "webp";
    public int Width { get; init; }
    public int Height { get; init; }
    public string Reason { get; init; } = "on_demand"; // on_demand | bundled
}
