using System.Text.Json;
using Microsoft.AspNetCore.Http;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

// Select a reachable bridge origin only for explicitly configured remote hosts.
// Source signatures, exact-source binding and local server-side paths survive.
public static class HomeConnectionPaths
{
    public static string Rewrite(string source, HttpRequest request, IEnumerable<string> remoteHosts)
    {
        if (!remoteHosts.Contains(request.Host.Host, StringComparer.OrdinalIgnoreCase)
            || !Uri.TryCreate(source, UriKind.Absolute, out var uri)
            || uri.Port != 8092 || !uri.AbsolutePath.StartsWith("/play/", StringComparison.Ordinal)) return source;
        var target = new UriBuilder(uri) { Host = request.Host.Host, Port = 8092, Scheme = "http" };
        return target.Uri.AbsoluteUri;
    }

    public static string ForRequest(string source, HttpRequest request)
    {
        try
        {
            var hosts = JsonSerializer.Deserialize<string[]>(File.ReadAllText("/config/home-media-remote-hosts.json")) ?? Array.Empty<string>();
            return Rewrite(source, request, hosts);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            return source;
        }
    }
}
