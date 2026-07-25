using System.Text.Json.Nodes;

namespace JaatoBridge.State;

/// <summary>
/// §8 session state: an immutable config swapped whole (atomically) by <c>configure</c>. Fail-closed
/// defaults — empty scope observes/acts on nothing, password masking on, conservative settle.
/// Held as records so a running handler always sees a consistent snapshot.
/// </summary>
public sealed record SettleConfig
{
    public int QuietWindowMs { get; init; } = 500;
    public int HardTimeoutMs { get; init; } = 5000;
    public string[] EventMask { get; init; } = { "WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED" };
    public string[] PackageScope { get; init; } = Array.Empty<string>();
    public string Mode { get; init; } = "quiet"; // quiet | minEventsThenQuiet
    public int MinEventCount { get; init; } = 1;
    public bool BundleScreenshotOnSettle { get; init; }
}

public sealed record ScreenshotDefaults
{
    public string Format { get; init; } = "webp";
    public int Quality { get; init; } = 80;
    public int MaxDimension { get; init; } = 1280;
    public int[]? Crop { get; init; }
}

public sealed record RedactionPolicy
{
    public bool MaskPasswordNodes { get; init; } = true; // fail-closed default
    public JsonArray? ExtraMaskSelectors { get; init; }
}

public sealed record SessionConfig
{
    public SettleConfig Settle { get; init; } = new();
    public ScreenshotDefaults ScreenshotDefaults { get; init; } = new();
    public RedactionPolicy Redaction { get; init; } = new();
    /// <summary>§8 two-branch identity: AUMID / package-family for UWP, exe-path for Win32. Empty = nothing in scope.</summary>
    public string[] PackageScope { get; init; } = Array.Empty<string>();
    /// <summary>
    /// §13 daemon scope-POLICY decision (wire: <c>observeShellSurfaces</c>): when set, the OS shell/launcher
    /// surfaces (Start / Search) are observable even out of app scope so the agent can see Start to launch
    /// apps. The daemon owns the policy (whether); the device owns the mechanism (which windows are its shell,
    /// see <see cref="Observe.WindowLister.IsSystemShellSurface"/>). Default off = fail-closed.
    /// </summary>
    public bool ObserveShellSurfaces { get; init; } = false;
}

/// <summary>Atomically swappable holder for the active <see cref="SessionConfig"/>.</summary>
public sealed class SessionState
{
    volatile SessionConfig _cfg = new();
    public SessionConfig Current => _cfg;
    public void Replace(SessionConfig cfg) => _cfg = cfg;
}
