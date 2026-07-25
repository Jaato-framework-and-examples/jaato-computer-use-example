using JaatoBridge.Platform;

namespace JaatoBridge.Observe;

/// <summary>
/// Top-level window enumeration and §8 two-branch scope identity. The <c>windows</c> verb is the entry
/// point on Windows (§4.1) — it is non-scope-gated metadata; scope is enforced only at observe/act.
/// </summary>
public static class WindowLister
{
    const string AppFrameHost = "applicationframehost";

    public static List<WindowInfo> ListTopLevel()
    {
        var fg = WinApi.GetForegroundWindow();
        var list = new List<WindowInfo>();
        WinApi.EnumWindows((h, _) =>
        {
            if (!WinApi.IsWindowVisible(h)) return true;
            var title = WinApi.Title(h);
            if (title.Length == 0) return true;                       // skip untitled tool/host windows
            list.Add(Describe(h, fg));
            return true;
        }, IntPtr.Zero);
        return list;
    }

    public static WindowInfo Describe(IntPtr hwnd) => Describe(hwnd, WinApi.GetForegroundWindow());

    public static WindowInfo Describe(IntPtr hwnd, IntPtr fg)
    {
        int pid = WinApi.Pid(hwnd);
        string aumid = WinApi.Aumid(hwnd);
        return new WindowInfo
        {
            Id = hwnd.ToInt64(),
            Title = WinApi.Title(hwnd),
            ProcessId = pid,
            ExePath = WinApi.ExePath(pid),
            Aumid = string.IsNullOrEmpty(aumid) ? null : aumid,
            Foreground = hwnd == fg,
        };
    }

    /// <summary>§9 "process identity": AUMID for UWP, else full exe path.</summary>
    public static string Pkg(WindowInfo w) => !string.IsNullOrEmpty(w.Aumid) ? w.Aumid! : w.ExePath;

    // System shell / launcher surfaces: Start menu, Search, and the Alt+Tab task switcher. These are the OS
    // navigation UI the agent must be able to see to launch and switch apps — the Windows equivalent of the
    // Android launcher/home, which is always observable. They are never a user "app window", so exempting
    // them from the package scope gate leaks no user-app content while unblocking launch (otherwise the model
    // opens Start and gets an empty tree + no screenshot, so it can't see the search box and flails).
    static readonly string[] ShellSurfaceProcs =
        { "SearchHost", "SearchApp", "StartMenuExperienceHost", "ShellExperienceHost" };

    public static bool IsSystemShellSurface(WindowInfo w)
    {
        string proc = WinApi.ProcName(w.ProcessId);
        foreach (var s in ShellSurfaceProcs)
            if (proc.Equals(s, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    /// <summary>
    /// §8/§13 scope gate. UWP matches on AUMID or package family; Win32 on full exe path or bare process
    /// name. Case-insensitive. Empty scope matches nothing (fail-closed). System shell/launcher surfaces
    /// (Start/Search) are in scope ONLY when the daemon set <c>observeShellSurfaces</c> (§13 scope policy) —
    /// the daemon owns whether, the device owns which windows are its shell.
    /// </summary>
    public static bool InScope(WindowInfo w, IReadOnlyList<string> scope, bool observeShellSurfaces)
    {
        if (observeShellSurfaces && IsSystemShellSurface(w)) return true;
        if (scope.Count == 0) return false;
        bool isUwp = w.Aumid is not null || WinApi.ProcName(w.ProcessId).Equals("ApplicationFrameHost", StringComparison.OrdinalIgnoreCase);
        string? family = isUwp ? WinApi.PackageFamily(w.ProcessId) : null;
        string exe = w.ExePath;
        string procName = exe.Length > 0 ? System.IO.Path.GetFileName(exe) : "";

        foreach (var s in scope)
        {
            if (isUwp)
            {
                if (w.Aumid is not null && w.Aumid.Equals(s, StringComparison.OrdinalIgnoreCase)) return true;
                if (family is not null && family.Equals(s, StringComparison.OrdinalIgnoreCase)) return true;
            }
            else
            {
                if (exe.Equals(s, StringComparison.OrdinalIgnoreCase)) return true;
                if (procName.Length > 0 && procName.Equals(s, StringComparison.OrdinalIgnoreCase)) return true;
            }
        }
        return false;
    }
}
