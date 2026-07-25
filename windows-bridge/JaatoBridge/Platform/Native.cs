using System.Runtime.InteropServices;

namespace JaatoBridge.Platform;

/// <summary>
/// Thin Win32 surface shared across layers: integrity levels (the §3.2 elevation gate),
/// screen metrics, and window/process identity helpers. Kept in one place so the P/Invoke
/// signatures have a single home.
/// </summary>
public static partial class Native
{
    // ---- integrity (§3.2 / §12.5) ----
    public const int TokenIntegrityLevel = 25;
    public const uint TOKEN_QUERY = 0x0008;
    public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

    public enum Integrity { Unknown = 0, Untrusted = 0x0000, Low = 0x1000, Medium = 0x2000, High = 0x3000, System = 0x4000 }

    public static Integrity OwnIntegrity()
    {
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, out var tok)) return Integrity.Unknown;
        try { return RidToIntegrity(RidFromToken(tok)); } finally { CloseHandle(tok); }
    }

    public static Integrity ProcessIntegrity(int pid)
    {
        var h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
        if (h == IntPtr.Zero) return Integrity.Unknown;
        try
        {
            if (!OpenProcessToken(h, TOKEN_QUERY, out var tok)) return Integrity.Unknown;
            try { return RidToIntegrity(RidFromToken(tok)); } finally { CloseHandle(tok); }
        }
        finally { CloseHandle(h); }
    }

    static Integrity RidToIntegrity(uint rid) => rid switch
    {
        0 => Integrity.Unknown,
        < 0x1000 => Integrity.Untrusted,
        < 0x2000 => Integrity.Low,
        < 0x3000 => Integrity.Medium,
        < 0x4000 => Integrity.High,
        _ => Integrity.System,
    };

    static uint RidFromToken(IntPtr tok)
    {
        GetTokenInformation(tok, TokenIntegrityLevel, IntPtr.Zero, 0, out int len);
        if (len == 0) return 0;
        var buf = Marshal.AllocHGlobal(len);
        try
        {
            if (!GetTokenInformation(tok, TokenIntegrityLevel, buf, len, out _)) return 0;
            var sid = Marshal.ReadIntPtr(buf);
            int count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
            return (uint)Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)(count - 1)));
        }
        finally { Marshal.FreeHGlobal(buf); }
    }

    public static (int w, int h) PrimaryScreen() => (GetSystemMetrics(0), GetSystemMetrics(1));

    // ---- P/Invoke ----
    [LibraryImport("kernel32.dll")] public static partial IntPtr GetCurrentProcess();
    [LibraryImport("kernel32.dll", SetLastError = true)] public static partial IntPtr OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, int pid);
    [LibraryImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] public static partial bool CloseHandle(IntPtr h);
    [LibraryImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] public static partial bool OpenProcessToken(IntPtr h, uint access, out IntPtr tok);
    [LibraryImport("advapi32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] public static partial bool GetTokenInformation(IntPtr tok, int tic, IntPtr buf, int len, out int retLen);
    [LibraryImport("advapi32.dll")] public static partial IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [LibraryImport("advapi32.dll")] public static partial IntPtr GetSidSubAuthority(IntPtr sid, uint idx);
    [LibraryImport("user32.dll")] public static partial int GetSystemMetrics(int index);
}
