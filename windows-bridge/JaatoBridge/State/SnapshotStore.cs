namespace JaatoBridge.State;

/// <summary>
/// Holds the ref→RuntimeId table for the <em>current</em> snapshotVersion so <c>act</c> can resolve a
/// <c>{ref,snapshotVersion}</c> selector by identity (§4.3). Refs are valid only within the version that
/// produced them (§8); a newer observe replaces the entry, and a stale version resolves to STALE.
/// </summary>
public sealed class SnapshotStore
{
    public sealed record Entry(long Version, IntPtr Hwnd, IReadOnlyDictionary<int, int[]> RefTable);

    volatile Entry? _latest;
    public Entry? Latest => _latest;
    public void Set(long version, IntPtr hwnd, IReadOnlyDictionary<int, int[]> refTable)
        => _latest = new Entry(version, hwnd, refTable);
}
