using System.Security.Cryptography;
using Microsoft.Extensions.Caching.Memory;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

// Only shared public artwork bytes are cached, never user DTOs or credentials.
public sealed class HomeImageService(IHttpClientFactory clients) : IDisposable
{
    public sealed record Image(byte[] Bytes, string ContentType, string ETag);
    private readonly MemoryCache cache = new(new MemoryCacheOptions { SizeLimit = 16 * 1024 * 1024 });
    private readonly SemaphoreSlim[] locks = Enumerable.Range(0, 64).Select(_ => new SemaphoreSlim(1, 1)).ToArray();

    public static bool IsLocalPoster(string url) => Uri.TryCreate(url, UriKind.Absolute, out var uri)
        && uri.Port == 8092 && uri.AbsolutePath.StartsWith("/poster/", StringComparison.Ordinal);

    public async Task<Image> GetAsync(string url, CancellationToken cancellation)
    {
        cancellation.ThrowIfCancellationRequested();
        if (cache.TryGetValue<Image>(url, out var hit) && hit is not null) return hit;
        var gate = locks[(url.GetHashCode() & int.MaxValue) % locks.Length];
        await gate.WaitAsync(cancellation);
        try
        {
            if (cache.TryGetValue<Image>(url, out hit) && hit is not null) return hit;
            using var client = clients.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(10);
            using var response = await client.GetAsync(url, cancellation);
            response.EnsureSuccessStatusCode();
            var bytes = await response.Content.ReadAsByteArrayAsync(cancellation);
            var result = new Image(bytes, response.Content.Headers.ContentType?.MediaType ?? "image/jpeg",
                "\"" + Convert.ToHexString(SHA256.HashData(bytes)) + "\"");
            if (bytes.Length <= 4 * 1024 * 1024)
                cache.Set(url, result, new MemoryCacheEntryOptions { Size = bytes.Length, AbsoluteExpirationRelativeToNow = TimeSpan.FromSeconds(60) });
            return result;
        }
        finally { gate.Release(); }
    }

    public void Dispose() { cache.Dispose(); foreach (var gate in locks) gate.Dispose(); }
}
