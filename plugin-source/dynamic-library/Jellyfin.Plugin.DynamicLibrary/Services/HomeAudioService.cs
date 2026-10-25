using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Caching.Memory;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

// Original verified sources are exposed to Jellyfin's native HLS encoder.
// The encoder owns segment production, seeking, throttling and cleanup.
public sealed class HomeAudioService(IMemoryCache cache, ILibraryManager library)
{
    private static readonly object ItemLock = new();
    private const string Prefix = "home_audio_sources:";

    public void Register(Guid itemId, MediaSourceInfo source)
    {
        // Do not retain a user's token or mutate the original probe metadata.
        var clone = JsonSerializer.Deserialize<MediaSourceInfo>(JsonSerializer.Serialize(source))!;
        clone.Path = clone.Path.Replace("&save=1", "", StringComparison.Ordinal);
        clone.TranscodingUrl = null;
        clone.SupportsTranscoding = true;
        clone.SupportsDirectStream = true;
        clone.SupportsProbing = false;
        var sources = cache.GetOrCreate(Prefix + itemId, entry =>
        {
            entry.SlidingExpiration = TimeSpan.FromHours(4);
            return new ConcurrentDictionary<string, MediaSourceInfo>();
        })!;
        sources[source.Id] = clone;
    }

    public IEnumerable<MediaSourceInfo> Sources(Guid itemId)
    {
        if (!cache.TryGetValue<ConcurrentDictionary<string, MediaSourceInfo>>(Prefix + itemId, out var sources) || sources is null)
            return Array.Empty<MediaSourceInfo>();
        return sources.Values.Select(s => JsonSerializer.Deserialize<MediaSourceInfo>(JsonSerializer.Serialize(s))!).ToArray();
    }

    public void EnsureIndexed(Guid id, string key, BaseItemDto? dto, long runtime)
    {
        if (library.GetItemById(id) is not null) return;
        // Browse IDs already use Jellyfin's canonical filesystem IDs. Persist
        // the existing .strm now rather than racing the asynchronous scan.
        lock (ItemLock)
        {
            if (library.GetItemById(id) is not null) return;
            var parts = key.Split(':');
            if (parts.Length < 2 || !Regex.IsMatch(parts[1], "^tt[0-9]{5,12}$")) throw new InvalidOperationException("Invalid catalogue identity");
            BaseItem item;
            BaseItem? parent;
            if (parts[0] == "movie")
            {
                var path = $"/catalogue/movies/{parts[1]}/movie.strm";
                if (!File.Exists(path)) throw new InvalidOperationException("Catalogue movie is not persisted yet");
                parent = library.GetItemById(Guid.Parse("<ONLINE_MOVIES_LIBRARY_ID>"));
                item = new Movie { Id = id, Path = path };
            }
            else if (parts[0] == "series" && parts.Length == 4 && int.TryParse(parts[2], out var season) && int.TryParse(parts[3], out var episode))
            {
                var seriesPath = $"/catalogue/shows/{parts[1]}";
                var episodePath = $"{seriesPath}/Season {season:D2}/S{season:D2}E{episode:D2}.strm";
                if (!File.Exists(episodePath)) throw new InvalidOperationException("Catalogue episode is not persisted yet");
                var seriesId = library.GetNewItemId(seriesPath, typeof(Series));
                var series = library.GetItemById(seriesId) as Series;
                if (series is null)
                {
                    series = new Series { Id = seriesId, Path = seriesPath, Name = dto?.SeriesName ?? parts[1] };
                    series.ProviderIds["Imdb"] = parts[1];
                    library.CreateItem(series, library.GetItemById(Guid.Parse("<ONLINE_SHOWS_LIBRARY_ID>")));
                }
                var seasonPath = $"{seriesPath}/Season {season:D2}";
                var seasonId = library.GetNewItemId(seasonPath, typeof(Season));
                parent = library.GetItemById(seasonId);
                if (parent is null)
                {
                    var created = new Season { Id = seasonId, Path = seasonPath, Name = $"Season {season}", IndexNumber = season, SeriesId = seriesId };
                    library.CreateItem(created, series);
                    parent = created;
                }
                item = new Episode { Id = id, Path = episodePath, IndexNumber = episode, ParentIndexNumber = season, SeriesId = seriesId, SeasonId = seasonId };
            }
            else throw new InvalidOperationException("Invalid catalogue type");
            if (parent is null) throw new InvalidOperationException("Catalogue library is missing");
            item.Name = dto?.Name ?? parts[1];
            item.RunTimeTicks = runtime;
            item.PremiereDate = dto?.PremiereDate;
            item.ProductionYear = dto?.ProductionYear;
            if (parts[0] == "movie") item.ProviderIds["Imdb"] = parts[1];
            library.CreateItem(item, parent);
        }
    }

    public static bool NeedsConversion(MediaStream audio) => audio.Codec != "aac" || !string.Equals(audio.Profile, "LC", StringComparison.OrdinalIgnoreCase);

    private static string Auth(HttpRequest request, string field)
    {
        if (field == "Token")
        {
            var direct = request.Query["api_key"].ToString();
            if (string.IsNullOrEmpty(direct)) direct = request.Headers["X-Emby-Token"].ToString();
            if (string.IsNullOrEmpty(direct)) direct = request.Headers["X-MediaBrowser-Token"].ToString();
            if (!string.IsNullOrEmpty(direct)) return direct;
        }
        else if (!string.IsNullOrEmpty(request.Query[field])) return request.Query[field].ToString();
        foreach (var header in new[] { "Authorization", "X-Emby-Authorization" })
        {
            var value = request.Headers[header].ToString();
            var match = Regex.Match(value, "(?:^|[ ,])" + field + "=\"([^\"]+)\"", RegexOptions.IgnoreCase);
            if (match.Success) return match.Groups[1].Value;
            if (field == "Token" && value.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)) return value[7..].Trim();
        }
        return "";
    }

    public static void Configure(MediaSourceInfo source, Guid itemId, string playSession, HttpRequest request)
    {
        var audio = source.MediaStreams.FirstOrDefault(s => s.Type == MediaStreamType.Audio && s.Index == source.DefaultAudioStreamIndex)
            ?? source.MediaStreams.FirstOrDefault(s => s.Type == MediaStreamType.Audio);
        if (audio is null || !NeedsConversion(audio)) return;
        var token = Auth(request, "Token");
        if (string.IsNullOrEmpty(token)) throw new InvalidOperationException("Playback authorization missing");
        var parameters = new Dictionary<string, string>
        {
            ["MediaSourceId"] = source.Id, ["PlaySessionId"] = playSession,
            ["VideoCodec"] = "copy", ["AudioCodec"] = "aac", ["AudioBitRate"] = "192000",
            ["AudioSampleRate"] = "48000", ["TranscodingMaxAudioChannels"] = "2",
            ["AllowAudioStreamCopy"] = "false", ["AllowVideoStreamCopy"] = "true", ["EnableAutoStreamCopy"] = "false",
            ["AudioStreamIndex"] = audio.Index.ToString(), ["SubtitleStreamIndex"] = "-1",
            ["SegmentContainer"] = "ts", ["SegmentLength"] = "4", ["MinSegments"] = "1",
            ["CpuCoreLimit"] = "1", ["EnableAdaptiveBitrateStreaming"] = "false", ["api_key"] = token
        };
        var device = Auth(request, "DeviceId");
        if (!string.IsNullOrEmpty(device)) parameters["DeviceId"] = device;
        source.TranscodingUrl = $"Videos/{itemId:N}/master.m3u8?" + string.Join('&', parameters.Select(p => Uri.EscapeDataString(p.Key) + "=" + Uri.EscapeDataString(p.Value)));
        source.TranscodingContainer = "ts";
        source.TranscodingSubProtocol = Jellyfin.Data.Enums.MediaStreamProtocol.hls;
        source.Name = (source.Name ?? "Video") + " · AAC-LC audio";
        source.SupportsDirectPlay = false;
        source.SupportsDirectStream = false;
        source.SupportsTranscoding = true;
        // HLS carries video and LC audio. Text subtitles remain separate and
        // selectable; no subtitle burning or video encoding is requested.
        foreach (var sub in source.MediaStreams.Where(s => s.Type == MediaStreamType.Subtitle && s.IsTextSubtitleStream && (!s.IsExternal || !string.IsNullOrEmpty(s.Path))))
        {
            sub.IsExternal = true;
            sub.DeliveryMethod = MediaBrowser.Model.Dlna.SubtitleDeliveryMethod.External;
            sub.SupportsExternalStream = true;
            sub.DeliveryUrl = $"/Videos/{itemId:N}/{source.Id}/Subtitles/{sub.Index}/0/Stream.vtt?api_key={Uri.EscapeDataString(token)}";
            sub.Codec = "webvtt";
        }
    }
}

public sealed class HomeMediaSourceProvider(HomeAudioService sources) : IMediaSourceProvider
{
    public Task<IEnumerable<MediaSourceInfo>> GetMediaSources(BaseItem item, CancellationToken cancellationToken)
        => Task.FromResult(sources.Sources(item.Id));
    public Task<ILiveStream> OpenMediaSource(string openToken, List<ILiveStream> currentLiveStreams, CancellationToken cancellationToken)
        => throw new NotSupportedException("Verified HTTP sources do not require opening");
}
