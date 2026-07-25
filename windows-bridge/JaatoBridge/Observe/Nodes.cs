namespace JaatoBridge.Observe;

/// <summary>§8 Snapshot — the pruned, serialized tree returned by <c>observe</c>.</summary>
public sealed class Snapshot
{
    public long SnapshotVersion { get; init; }
    public string Pkg { get; init; } = "";            // §9: "process identity" (exe path or AUMID)
    public WindowInfo Window { get; init; } = new();   // §9 addition: required on a multi-window desktop
    public ScreenInfo Screen { get; init; } = new();
    public string? ScreenshotRef { get; init; }
    public List<NodeSnap> Nodes { get; init; } = new();
}

public sealed class ScreenInfo { public int Width { get; init; } public int Height { get; init; } }

/// <summary>§9 window descriptor carried on every Snapshot and by the <c>windows</c> verb.</summary>
public sealed class WindowInfo
{
    public long Id { get; init; }               // HWND as a stable integer for the window's lifetime (§4.1)
    public string Title { get; init; } = "";
    public int ProcessId { get; init; }
    public string ExePath { get; init; } = "";
    public string? Aumid { get; init; }         // present for UWP/packaged windows
    public bool Foreground { get; init; }
}

/// <summary>§8 node. Absent flags are false; parent omitted for roots.</summary>
public sealed class NodeSnap
{
    public int Ref { get; init; }
    public string Cls { get; init; } = "";
    public string? ViewId { get; init; }        // UIA AutomationId
    public string? Text { get; init; }
    public string? Desc { get; init; }
    public int[] Bounds { get; init; } = new int[4]; // [left, top, right, bottom]
    public List<string> Flags { get; init; } = new();
    public int? Parent { get; init; }
}

/// <summary>Well-known UIA property & pattern ids (validated in the latency spike).</summary>
public static class Uia
{
    public const int RuntimeId = 30000, Name = 30005, AutomationId = 30011, ControlType = 30003,
        ClassName = 30012, FrameworkId = 30024, BoundingRectangle = 30001, IsOffscreen = 30022,
        IsEnabled = 30010, IsKeyboardFocusable = 30009, HasKeyboardFocus = 30008, IsPassword = 30019,
        HelpText = 30013, FullDescription = 30159, ValueValue = 30045,
        IsInvokePatternAvailable = 30031, IsValuePatternAvailable = 30043,
        IsScrollPatternAvailable = 30034, IsTogglePatternAvailable = 30041;

    public const int InvokePattern = 10000, ValuePattern = 10002, ScrollPattern = 10004, TogglePattern = 10015;

    public static readonly int[] AllProps =
    {
        RuntimeId, Name, AutomationId, ControlType, ClassName, FrameworkId, BoundingRectangle,
        IsOffscreen, IsEnabled, IsKeyboardFocusable, HasKeyboardFocus, IsPassword, HelpText,
        FullDescription, ValueValue, IsInvokePatternAvailable, IsValuePatternAvailable,
        IsScrollPatternAvailable, IsTogglePatternAvailable,
    };
    public static readonly int[] AllPatterns = { InvokePattern, ValuePattern, ScrollPattern, TogglePattern };
}
