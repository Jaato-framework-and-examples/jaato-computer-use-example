using System.Text.Json.Nodes;

namespace JaatoBridge.Act;

/// <summary>§10 Selector — names a target for <c>act</c>. Resolved mechanically against the current tree.</summary>
public sealed class Selector
{
    public int? Ref { get; init; }
    public long? SnapshotVersion { get; init; }
    public string? ViewId { get; init; }
    public string? Text { get; init; }
    public string? Desc { get; init; }
    public int? Index { get; init; }
    public int[]? Bounds { get; init; }

    public static Selector Parse(JsonObject? o)
    {
        if (o is null) return new Selector();
        return new Selector
        {
            Ref = (int?)(o["ref"]?.GetValue<int>()),
            SnapshotVersion = (long?)(o["snapshotVersion"]?.GetValue<long>()),
            ViewId = (string?)o["viewId"],
            Text = (string?)o["text"],
            Desc = (string?)o["desc"],
            Index = (int?)(o["index"]?.GetValue<int>()),
            Bounds = o["bounds"] is JsonArray a ? a.Select(n => (int)n!.GetValue<double>()).ToArray() : null,
        };
    }

    public bool HasRef => Ref.HasValue && SnapshotVersion.HasValue;
}
