namespace JaatoBridge;

/// <summary>Minimal timestamped console logger. Real tray build routes this to a rolling file (M5).</summary>
public static class Log
{
    static readonly object Gate = new();
    static void Write(string level, string msg)
    {
        // No Date.Now sugar issues here — plain framework API.
        var ts = DateTime.Now.ToString("HH:mm:ss.fff");
        lock (Gate) Console.WriteLine($"{ts} {level,-5} {msg}");
    }
    public static void Info(string m) => Write("INFO", m);
    public static void Warn(string m) => Write("WARN", m);
    public static void Err(string m) => Write("ERR", m);
}
