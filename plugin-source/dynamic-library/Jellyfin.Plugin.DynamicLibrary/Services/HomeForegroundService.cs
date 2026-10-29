using Microsoft.AspNetCore.Http;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

// A bounded navigation lease lets disk-heavy background work yield without
// changing timer schedules or treating interrupted probes as failed sources.
public sealed class HomeForegroundService
{
    private long nextWrite;
    private readonly object gate = new();
    public void Touch(HttpRequest request, string controller, string action)
    {
        var playbackInfo = (controller is "Items" or "Videos")
            && action.Contains("PlaybackInfo", StringComparison.OrdinalIgnoreCase);
        if ((!HttpMethods.IsGet(request.Method) && !(playbackInfo && HttpMethods.IsPost(request.Method)))
            || request.HttpContext.User.Identity?.IsAuthenticated != true) return;
        var identifiesClient = request.Headers.ContainsKey("X-Emby-Authorization")
            || request.Headers.ContainsKey("X-Emby-Client") || request.Query.ContainsKey("DeviceId")
            || request.Headers.Authorization.ToString().Contains("Client=", StringComparison.OrdinalIgnoreCase);
        if (!identifiesClient) return;
        var metadata = controller is "UserViews" || action is "GetLatestMedia" or "GetResumeItems" or "GetNextUp"
            || (controller == "Items" && action == "GetItems" && !request.Query.ContainsKey("Ids"));
        var titleDetails = (controller == "UserLibrary" && (action == "GetItem" || action == "GetItemLegacy"))
            || (controller == "TvShows" && (action == "GetSeasons" || action == "GetEpisodes"));
        if (!metadata && !titleDetails && !playbackInfo) return;
        lock (gate)
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            var path = "/catalogue/.foreground-read-until";
            var lease = now + (playbackInfo ? 60 : 30);
            long currentLease = 0;
            try
            {
                if (File.Exists(path) && long.TryParse(File.ReadAllText(path), out var current))
                {
                    currentLease = current;
                    if (current >= lease)
                    {
                        nextWrite = Math.Max(nextWrite, now + 5);
                        return;
                    }
                }
            }
            catch (IOException) { }
            if (!playbackInfo && now < nextWrite && currentLease >= now + 25) return;
            var temporary = path + "." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllText(temporary, lease.ToString(System.Globalization.CultureInfo.InvariantCulture));
                File.Move(temporary, path, overwrite: true);nextWrite = now + 5;
            }
            catch (IOException) { File.Delete(temporary); }
            catch (UnauthorizedAccessException) { }
        }
    }
}
