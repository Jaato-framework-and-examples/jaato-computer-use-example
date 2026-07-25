using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace JaatoBridge.Platform;

/// <summary>
/// Classic-P/Invoke Win32 surface for window enumeration and §8 scope identity (exe path / AUMID /
/// package family). Kept on DllImport (not LibraryImport) because of the EnumWindows delegate callback.
/// </summary>
public static class WinApi
{
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll", SetLastError = true)] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);

    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

    [DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)] public static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] public static extern bool QueryFullProcessImageNameW(IntPtr h, int flags, StringBuilder buf, ref int size);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern int GetPackageFamilyName(IntPtr h, ref uint len, StringBuilder? name);

    [DllImport("shell32.dll")] static extern int SHGetPropertyStoreForWindow(IntPtr hwnd, ref Guid riid, out IntPtr ppv);
    [DllImport("propsys.dll")] static extern int PropVariantToStringAlloc(ref PROPVARIANT pv, out IntPtr ppsz);
    [DllImport("ole32.dll")] static extern int PropVariantClear(ref PROPVARIANT pv);

    const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    const int APPMODEL_ERROR_NO_PACKAGE = 15700;
    static readonly Guid IID_IPropertyStore = new("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99");
    static PROPERTYKEY PKEY_AppUserModel_ID = new() { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };

    public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowTextW(h, sb, sb.Capacity); return sb.ToString(); }
    public static string ClassName(IntPtr h) { var sb = new StringBuilder(256); GetClassNameW(h, sb, sb.Capacity); return sb.ToString(); }
    public static int Pid(IntPtr h) { GetWindowThreadProcessId(h, out int pid); return pid; }

    public static string ProcName(int pid) { try { return Process.GetProcessById(pid).ProcessName; } catch { return "?"; } }

    public static string ExePath(int pid)
    {
        var h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
        if (h == IntPtr.Zero) return "";
        try { int cap = 1024; var sb = new StringBuilder(cap); return QueryFullProcessImageNameW(h, 0, sb, ref cap) ? sb.ToString() : ""; }
        finally { CloseHandle(h); }
    }

    /// <summary>Package family name of a packaged process, or null for classic Win32.</summary>
    public static string? PackageFamily(int pid)
    {
        var h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
        if (h == IntPtr.Zero) return null;
        try
        {
            uint len = 0;
            if (GetPackageFamilyName(h, ref len, null) == APPMODEL_ERROR_NO_PACKAGE) return null;
            var sb = new StringBuilder((int)len);
            return GetPackageFamilyName(h, ref len, sb) == 0 ? sb.ToString() : null;
        }
        finally { CloseHandle(h); }
    }

    /// <summary>AUMID off the top-level window (§8/§12.4), or empty for non-packaged windows.</summary>
    public static string Aumid(IntPtr hwnd)
    {
        Guid iid = IID_IPropertyStore;
        if (SHGetPropertyStoreForWindow(hwnd, ref iid, out IntPtr pStore) != 0 || pStore == IntPtr.Zero) return "";
        var store = (IPropertyStore)Marshal.GetObjectForIUnknown(pStore);
        Marshal.Release(pStore);
        try
        {
            var key = PKEY_AppUserModel_ID;
            if (store.GetValue(ref key, out PROPVARIANT pv) != 0) return "";
            try
            {
                if (pv.vt is 0 or 1) return "";
                if (PropVariantToStringAlloc(ref pv, out IntPtr psz) == 0 && psz != IntPtr.Zero)
                { var s = Marshal.PtrToStringUni(psz) ?? ""; Marshal.FreeCoTaskMem(psz); return s; }
                return "";
            }
            finally { PropVariantClear(ref pv); }
        }
        finally { Marshal.ReleaseComObject(store); }
    }

    [StructLayout(LayoutKind.Sequential)] struct PROPERTYKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Sequential)] struct PROPVARIANT { public ushort vt; public ushort r1; public ushort r2; public ushort r3; public IntPtr val; public IntPtr val2; }

    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        int GetCount(out uint c);
        int GetAt(uint i, out PROPERTYKEY pk);
        int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        int SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
        int Commit();
    }
}
