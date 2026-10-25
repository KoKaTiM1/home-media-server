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
        if (!HttpMethods.IsGet(request.Method) || request.HttpContext.User.Identity?.IsAuthenticated != true) return;
        var identifiesClient = request.Headers.ContainsKey("X-Emby-Authorization")
            || request.Headers.ContainsKey("X-Emby-Client") || request.Query.ContainsKey("DeviceId")
            || request.Headers.Authorization.ToString().Contains("Client=", StringComparison.OrdinalIgnoreCase);
        if (!identifiesClient) return;
        var metadata = controller is "UserViews" || action is "GetLatestMedia" or "GetResumeItems" or "GetNextUp"
            || (controller == "Items" && action == "GetItems" && !request.Query.ContainsKey("Ids"));
        if (!metadata) return;
        lock (gate)
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            if (now < nextWrite) return;
            var path = "/catalogue/.foreground-read-until";
            var temporary = path + "." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllText(temporary, (now + 30).ToString(System.Globalization.CultureInfo.InvariantCulture));
                File.Move(temporary, path, overwrite: true);nextWrite = now + 5;
            }
            catch (IOException) { File.Delete(temporary); }
            catch (UnauthorizedAccessException) { }
        }
    }
}
