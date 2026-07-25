namespace JaatoBridge.State;

/// <summary>
/// Process-wide "world version" (§8 snapshotVersion). Monotonic. Every observe/settle stamps the
/// tree (and any bundled frame) with one value so the daemon knows tree and image describe one moment.
/// </summary>
public sealed class SnapshotClock
{
    long _v;
    public long Current => Interlocked.Read(ref _v);
    public long Next() => Interlocked.Increment(ref _v);
}
