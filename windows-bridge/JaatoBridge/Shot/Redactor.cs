using JaatoBridge.Observe;
using JaatoBridge.Platform;

namespace JaatoBridge.Shot;

/// <summary>
/// §13 redaction at source: composite opaque black over IsPassword element bounds in the raw BGRA frame
/// BEFORE encoding, so those pixels never leave the device. Node bounds are screen coords; the WGC frame
/// origin is the window's top-left, so we offset by GetWindowRect.
/// </summary>
public static class Redactor
{
    public static void MaskPasswords(byte[] bgra, int fw, int fh, IntPtr hwnd, IReadOnlyList<NodeSnap> nodes)
    {
        if (!WinApi.GetWindowRect(hwnd, out var wr)) return;
        foreach (var n in nodes)
        {
            if (!n.Flags.Contains("password")) continue;
            var b = n.Bounds; // screen [l,t,r,b]
            int l = Math.Clamp(b[0] - wr.Left, 0, fw);
            int t = Math.Clamp(b[1] - wr.Top, 0, fh);
            int r = Math.Clamp(b[2] - wr.Left, 0, fw);
            int bot = Math.Clamp(b[3] - wr.Top, 0, fh);
            FillBlack(bgra, fw, l, t, r, bot);
        }
    }

    static void FillBlack(byte[] bgra, int fw, int l, int t, int r, int b)
    {
        for (int y = t; y < b; y++)
        {
            int row = (y * fw + l) * 4;
            for (int x = l; x < r; x++, row += 4)
            {
                bgra[row] = 0; bgra[row + 1] = 0; bgra[row + 2] = 0; bgra[row + 3] = 255;
            }
        }
    }
}
