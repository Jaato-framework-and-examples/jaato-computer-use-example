using System.Runtime.InteropServices;

namespace JaatoBridge.Act;

/// <summary>
/// SendInput-based synthetic input — used ONLY when the controller explicitly asks (GESTURE, LONG_CLICK,
/// GLOBAL). §5.2: this moves the physical cursor and steals real input, so it is never an automatic
/// fallback for a refused semantic action.
/// </summary>
public static class SyntheticInput
{
    public static void Click(int x, int y) { Move(x, y); Down(); Up(); }
    public static void LongClick(int x, int y, int ms = 600) { Move(x, y); Down(); Thread.Sleep(ms); Up(); }

    public static void Swipe(IReadOnlyList<(int x, int y)> path, int ms = 300)
    {
        if (path.Count == 0) return;
        Move(path[0].x, path[0].y); Down();
        int steps = Math.Max(1, path.Count - 1);
        int per = Math.Max(1, ms / Math.Max(1, steps));
        for (int i = 1; i < path.Count; i++) { Move(path[i].x, path[i].y); Thread.Sleep(per); }
        Up();
    }

    public static void KeyCombo(params ushort[] vks)
    {
        var inputs = new List<INPUT>();
        foreach (var vk in vks) inputs.Add(Key(vk, false));
        for (int i = vks.Length - 1; i >= 0; i--) inputs.Add(Key(vks[i], true));
        Send(inputs.ToArray());
    }

    /// <summary>
    /// §11.1 TYPE_TEXT — inject text into whatever holds keyboard focus. Printable chars go as Unicode
    /// (VK=0, scancode=codepoint); a newline is injected as a real Enter key, because a lone U+000A does
    /// nothing via KEYEVENTF_UNICODE (it silently drops line breaks). CR is folded into the LF.
    /// </summary>
    public static void TypeText(string text)
    {
        var inputs = new List<INPUT>(text.Length * 2);
        foreach (char c in text)
        {
            if (c == '\r') continue;
            if (c == '\n') { inputs.Add(Key(VK_RETURN, false)); inputs.Add(Key(VK_RETURN, true)); continue; }
            inputs.Add(Unicode(c, false));
            inputs.Add(Unicode(c, true));
        }
        if (inputs.Count > 0) Send(inputs.ToArray());
    }

    /// <summary>§11.1 PRESS_KEY — inject a virtual-key press to the focused element.</summary>
    public static void PressKey(ushort vk) => Send(new[] { Key(vk, false), Key(vk, true) });

    [DllImport("user32.dll")] public static extern bool LockWorkStation();

    // ---- primitives ----
    static void Move(int x, int y)
    {
        int vx = GetSystemMetrics(76), vy = GetSystemMetrics(77);      // SM_XVIRTUALSCREEN / Y
        int vw = GetSystemMetrics(78), vh = GetSystemMetrics(79);      // SM_CXVIRTUALSCREEN / CY
        int ax = (int)((x - vx) * 65535.0 / Math.Max(1, vw - 1));
        int ay = (int)((y - vy) * 65535.0 / Math.Max(1, vh - 1));
        Send(new[] { Mouse(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK) });
    }
    static void Down() => Send(new[] { Mouse(0, 0, MOUSEEVENTF_LEFTDOWN) });
    static void Up() => Send(new[] { Mouse(0, 0, MOUSEEVENTF_LEFTUP) });

    static INPUT Mouse(int dx, int dy, uint flags) => new()
    { type = INPUT_MOUSE, U = new InputUnion { mi = new MOUSEINPUT { dx = dx, dy = dy, dwFlags = flags } } };
    static INPUT Key(ushort vk, bool up) => new()
    { type = INPUT_KEYBOARD, U = new InputUnion { ki = new KEYBDINPUT { wVk = vk, dwFlags = up ? KEYEVENTF_KEYUP : 0 } } };
    static INPUT Unicode(char c, bool up) => new()
    { type = INPUT_KEYBOARD, U = new InputUnion { ki = new KEYBDINPUT { wVk = 0, wScan = c, dwFlags = KEYEVENTF_UNICODE | (up ? KEYEVENTF_KEYUP : 0) } } };

    static void Send(INPUT[] inputs) => SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>());

    const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
    const uint MOUSEEVENTF_MOVE = 0x0001, MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004,
        MOUSEEVENTF_ABSOLUTE = 0x8000, MOUSEEVENTF_VIRTUALDESK = 0x4000;
    const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;

    public const ushort VK_LWIN = 0x5B, VK_MENU = 0x12, VK_TAB = 0x09, VK_F4 = 0x73, VK_D = 0x44, VK_M = 0x4D, VK_RETURN = 0x0D;

    [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint n, INPUT[] inputs, int cb);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int i);

    [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public InputUnion U; }
    [StructLayout(LayoutKind.Explicit)]
    struct InputUnion { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
}
