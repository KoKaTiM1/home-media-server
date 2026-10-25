using System.Text.Json;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

// Shared private filesystem events avoid a public mutation endpoint or a new
// credential. The bridge commits each event once, then archives it locally.
public sealed class HomeRecheckStore(string root = "/catalogue/.recheck-requests/pending")
{
    public void Request(IEnumerable<string> keys)
    {
        var scopes = keys.Distinct().ToArray();
        if (scopes.Length is < 1 or > 100) throw new ArgumentException("Invalid recheck batch size");
        Directory.CreateDirectory(root);
        var id = Guid.NewGuid().ToString("N");
        var temporary = Path.Combine(root, id + ".tmp");
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(new
            {
                id, requested = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, keys = scopes
            }));
            File.Move(temporary, Path.Combine(root, id + ".json"));
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
