using Microsoft.Win32;

namespace JaatoBridge.Platform;

/// <summary>
/// §3.1 logon autostart via the per-user Run key (no admin, runs in the interactive session — the
/// only session that can see the desktop). Toggled from the tray menu.
/// </summary>
public static class Autostart
{
    const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    const string ValueName = "JaatoBridge";

    public static bool IsEnabled()
    {
        using var k = Registry.CurrentUser.OpenSubKey(RunKey);
        return k?.GetValue(ValueName) is string;
    }

    public static void Set(bool enabled, string commandLine)
    {
        using var k = Registry.CurrentUser.CreateSubKey(RunKey);
        if (k is null) return;
        if (enabled) k.SetValue(ValueName, commandLine);
        else k.DeleteValue(ValueName, throwOnMissingValue: false);
    }
}
