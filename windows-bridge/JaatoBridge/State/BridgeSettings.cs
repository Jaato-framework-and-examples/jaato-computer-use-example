using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace JaatoBridge.State;

/// <summary>
/// User-editable connection settings — the daemon URL (ws:// or wss://) and an optional bearer token —
/// persisted to <c>%APPDATA%\JaatoBridge\settings.json</c> so the tray can set them once and they
/// survive restarts (matching the Android bridge's configurable endpoint). The token is a device-bound
/// secret (01 §13), so it is encrypted at rest with Windows DPAPI (CurrentUser scope) — never written
/// in clear, and only this Windows user account can decrypt it.
/// </summary>
public sealed class BridgeSettings
{
    public Uri Uri { get; private set; }
    public string? Token { get; private set; }

    BridgeSettings(Uri uri, string? token) { Uri = uri; Token = token; }

    static string Dir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "JaatoBridge");
    static string FilePath => Path.Combine(Dir, "settings.json");

    /// <summary>Load saved settings; fall back to <paramref name="fallback"/> URL with no token.</summary>
    public static BridgeSettings Load(Uri fallback)
    {
        try
        {
            if (File.Exists(FilePath))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(FilePath));
                var root = doc.RootElement;
                string? url = root.TryGetProperty("url", out var u) ? u.GetString() : null;
                string? token = null;
                if (root.TryGetProperty("tokenProtected", out var tp) && tp.GetString() is { Length: > 0 } b64)
                    token = Unprotect(b64);
                if (!string.IsNullOrWhiteSpace(url) && Uri.TryCreate(url, UriKind.Absolute, out var parsed))
                    return new BridgeSettings(parsed, token);
            }
        }
        catch (Exception ex) { Log.Warn($"settings load failed, using defaults: {ex.Message}"); }
        return new BridgeSettings(fallback, null);
    }

    /// <summary>Replace the endpoint and persist. Empty/whitespace token is stored as "no token".</summary>
    public void Update(Uri uri, string? token)
    {
        Uri = uri;
        Token = string.IsNullOrWhiteSpace(token) ? null : token;
        Save();
    }

    void Save()
    {
        try
        {
            Directory.CreateDirectory(Dir);
            var obj = new Dictionary<string, string?>
            {
                ["url"] = Uri.ToString(),
                ["tokenProtected"] = string.IsNullOrEmpty(Token) ? null : Protect(Token),
            };
            File.WriteAllText(FilePath, JsonSerializer.Serialize(obj, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex) { Log.Warn($"settings save failed: {ex.Message}"); }
    }

    static string Protect(string s) => Convert.ToBase64String(
        ProtectedData.Protect(Encoding.UTF8.GetBytes(s), null, DataProtectionScope.CurrentUser));

    static string? Unprotect(string b64)
    {
        try
        {
            return Encoding.UTF8.GetString(
                ProtectedData.Unprotect(Convert.FromBase64String(b64), null, DataProtectionScope.CurrentUser));
        }
        catch (Exception ex) { Log.Warn($"token decrypt failed (ignoring stored token): {ex.Message}"); return null; }
    }
}
