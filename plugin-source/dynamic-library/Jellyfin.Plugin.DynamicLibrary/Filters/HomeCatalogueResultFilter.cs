using System.Security.Cryptography;
using System.Text;
using MediaBrowser.Model.Querying;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Search;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.DynamicLibrary.Services;
using MediaBrowser.Model.MediaInfo;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.DynamicLibrary.Filters;

// Local integration: replace upstream's estimated media tracks with verified
// torrent metadata and select Hebrew, then English, before playback begins.
// Applies only to this server's catalogue bridge, never existing local media.
public sealed class HomeCatalogueResultFilter(IHttpClientFactory clients,
    ILogger<HomeCatalogueResultFilter> logger,
    DynamicLibraryService catalogues, ILibraryManager library, DynamicItemCache itemCache, HomeAudioService audioService) : IAsyncAlwaysRunResultFilter, IAsyncActionFilter
{
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        if (context.HttpContext.Request.Query.TryGetValue("MediaSourceId", out var sourceId))
            context.HttpContext.Items["HomeMediaSourceId"] = sourceId.ToString();
        foreach (var argument in context.ActionArguments.Values)
            if (argument?.GetType().GetProperty("MediaSourceId")?.GetValue(argument) is string selectedId)
                context.HttpContext.Items["HomeMediaSourceId"] = selectedId;
        // Track switches and explicit None must survive PlaybackInfo refreshes.
        foreach (var name in new[] { "SubtitleStreamIndex", "AudioStreamIndex" })
        {
            if (int.TryParse(context.HttpContext.Request.Query[name], out var index))
                context.HttpContext.Items["Home" + name] = index;
            foreach (var value in context.ActionArguments.Values)
            {
                if (value?.GetType().GetProperty(name)?.GetValue(value) is int requested)
                    context.HttpContext.Items["Home" + name] = requested;
            }
        }
        await next();
    }

    private sealed class Prepared
    {
        public List<MediaStream> MediaStreams { get; set; } = new();
        public string Container { get; set; } = "mkv";
        public long RunTimeTicks { get; set; }
        public int Bitrate { get; set; }
        public int? DefaultSubtitleStreamIndex { get; set; }
        public int? DefaultAudioStreamIndex { get; set; }
    }

    private sealed class TitleLabels
    {
        public string? Hebrew { get; set; }
        public string? English { get; set; }
        public string? Poster { get; set; }
        public List<MediaUrl>? Trailers { get; set; }
    }

    private bool IsHomeTitle(BaseItemDto title) => title.Type is BaseItemKind.Movie or BaseItemKind.Series
        && (library.GetItemById(title.Id)?.Path?.StartsWith("/catalogue/", StringComparison.Ordinal) == true
            || SearchResultFactory.IsDynamicItem(title));

    private sealed class ReadyResponse
    {
        public List<string> Titles { get; set; } = new();
    }

    private string? HomeKey(Guid id, BaseItemKind type)
    {
        if (type == BaseItemKind.Episode)
        {
            var episode = library.GetItemById(id) as MediaBrowser.Controller.Entities.TV.Episode;
            var cached = itemCache.GetItem(id);
            var seriesId = episode?.SeriesId ?? cached?.SeriesId;
            if (seriesId is null) return null;
            var parentKey = HomeKey(seriesId.Value, BaseItemKind.Series);
            var season = episode?.ParentIndexNumber ?? cached?.ParentIndexNumber;
            var number = episode?.IndexNumber ?? cached?.IndexNumber;
            return parentKey is not null && season.HasValue && number.HasValue ? $"{parentKey}:{season}:{number}" : null;
        }
        if (type is not (BaseItemKind.Movie or BaseItemKind.Series)) return null;
        var native = library.GetItemById(id);
        if (native?.Path?.StartsWith("/catalogue/", StringComparison.Ordinal) != true
            && itemCache.GetImageUrl(id)?.StartsWith("http://<SERVER_LAN_IP>:8092/poster/", StringComparison.Ordinal) != true) return null;
        var imdb = native?.ProviderIds.GetValueOrDefault("Imdb");
        if (string.IsNullOrEmpty(imdb))
        {
            var image = itemCache.GetImageUrl(id);
            if (Uri.TryCreate(image, UriKind.Absolute, out var uri))
                imdb = uri.Segments.LastOrDefault()?.Replace(".jpg", "", StringComparison.Ordinal);
        }
        return string.IsNullOrEmpty(imdb) ? null : (type == BaseItemKind.Movie ? "movie:" : "series:") + imdb;
    }

    private async Task<HashSet<string>> ReadyAsync(IEnumerable<string?> keys, CancellationToken token)
    {
        var ready = new HashSet<string>();
        var client = clients.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(3);
        foreach (var batch in keys.Where(k => k is not null).Distinct().Chunk(100))
        {
            try
            {
                var result = await client.GetFromJsonAsync<ReadyResponse>(
                    "http://catalogue-bridge:8092/ready?keys=" + Uri.EscapeDataString(string.Join(',', batch)), token);
                if (result is not null) ready.UnionWith(result.Titles);
            }
            catch (Exception) { /* Unchecked online titles cannot be advertised as ready. */ }
        }
        return ready;
    }

    private void PreservePoster(BaseItemDto title, string? poster = null)
    {
        if (!IsHomeTitle(title)) return;
        // Bulk native refreshes omit ProviderIds unless explicitly requested.
        // Recover the identity from the actual catalogue item in that case.
        var imdb = title.ProviderIds?.GetValueOrDefault("Imdb")
            ?? library.GetItemById(title.Id)?.ProviderIds.GetValueOrDefault("Imdb");
        if (string.IsNullOrWhiteSpace(imdb)) return;
        var kind = title.Type == BaseItemKind.Movie ? "movie" : "series";
        poster ??= itemCache.GetImageUrl(title.Id) ?? $"http://<SERVER_LAN_IP>:8092/poster/{kind}/{imdb}.jpg";
        var backdrop = itemCache.GetBackdropUrl(title.Id);
        title.ImageTags ??= new();
        title.ImageTags[ImageType.Primary] = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(poster)));
        title.PrimaryImageAspectRatio ??= 0.667;
        if (!string.IsNullOrEmpty(backdrop) && (title.BackdropImageTags is null || title.BackdropImageTags.Length == 0))
            title.BackdropImageTags = new[] { Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(backdrop))) };
        // A native DTO replaces the grid card on Back. Keep its image metadata
        // and proxy URL, rather than replacing a valid poster with a blank DTO.
        itemCache.StoreImageUrl(title.Id, poster);
    }

    public async Task OnResultExecutionAsync(ResultExecutingContext context, ResultExecutionDelegate next)
    {
        if (context.Result is ObjectResult listResult && listResult.Value is QueryResult<BaseItemDto> list)
        {
            // Native Items uses a deferred DTO projection. Materialize once;
            // mutating one enumeration otherwise disappears on serialization.
            var entries = list.Items.ToArray();
            foreach (var entry in entries)
                if (entry.ImageTags?.GetValueOrDefault(ImageType.Primary)?.Length != 64) PreservePoster(entry);
            var keyed = entries.Select(e => (Entry: e, Key: HomeKey(e.Id, e.Type))).ToArray();
            var ready = await ReadyAsync(keyed.Select(e => e.Key), context.HttpContext.RequestAborted);
            var visible = keyed.Where(e => e.Key is null || ready.Contains(e.Key)).Select(e => e.Entry).ToArray();
            list.Items = visible;
            list.TotalRecordCount = Math.Max(visible.Length, list.TotalRecordCount - (entries.Length - visible.Length));
        }
        if (context.Result is ObjectResult titleResult && titleResult.Value is BaseItemDto title
            && title.Type is BaseItemKind.Movie or BaseItemKind.Series
            && title.ProviderIds?.TryGetValue("Imdb", out var titleImdb) == true)
        {
            PreservePoster(title);
            try
            {
                var labelsClient = clients.CreateClient();
                labelsClient.Timeout = TimeSpan.FromSeconds(2);
                var kind = title.Type == BaseItemKind.Movie ? "movie" : "series";
                var labels = await labelsClient.GetFromJsonAsync<TitleLabels>(
                    "http://catalogue-bridge:8092/label/" + Uri.EscapeDataString(titleImdb) + "?kind=" + kind, context.HttpContext.RequestAborted);
                if (labels?.Poster is string poster && poster.StartsWith("http://<SERVER_LAN_IP>:8092/poster/", StringComparison.Ordinal))
                    PreservePoster(title, poster);
                if (labels?.Hebrew is string hebrew && hebrew.Length > 0)
                {
                    var english = labels.English ?? title.OriginalTitle ?? title.Name;
                    title.Name = hebrew + " / " + english;
                    title.OriginalTitle = english;
                }
                if (labels?.Trailers is { Count: > 0 })
                    title.RemoteTrailers = (title.RemoteTrailers ?? Array.Empty<MediaUrl>()).Concat(labels.Trailers)
                        .DistinctBy(t => t.Url).ToArray();
            }
            catch (Exception) { /* A label lookup cannot block title playback. */ }
        }
        // Android TV uses /Search/Hints, whose response is not the Items DTO
        // handled by upstream's SearchActionFilter. Populate that interface too.
        if (context.Result is ObjectResult hintResult && hintResult.Value is SearchHintResult hints
            && context.HttpContext.Request.Query.TryGetValue("searchTerm", out var term)
            && !string.IsNullOrWhiteSpace(term))
        {
            var guardedHints = hints.SearchHints.Select(h => (Hint: h, Key: HomeKey(h.Id, h.Type))).ToArray();
            var allowedHints = await ReadyAsync(guardedHints.Select(h => h.Key), context.HttpContext.RequestAborted);
            var baseHints = guardedHints.Where(h => h.Key is null || allowedHints.Contains(h.Key)).Select(h => h.Hint).ToArray();
            hintResult.Value = new SearchHintResult(baseHints, baseHints.Length);
            try
            {
                var types = new HashSet<BaseItemKind> { BaseItemKind.Movie, BaseItemKind.Series };
                if (context.HttpContext.Request.Query.TryGetValue("includeItemTypes", out var included))
                {
                    types.RemoveWhere(t => !included.ToString().Split(',').Contains(t.ToString(), StringComparer.OrdinalIgnoreCase));
                }
                var remote = await catalogues.SearchAsync(term.ToString(), types,
                    context.HttpContext.RequestAborted);
                var remoteReady = await ReadyAsync(remote.Select(i => HomeKey(i.Id, i.Type)), context.HttpContext.RequestAborted);
                remote = remote.Where(i => HomeKey(i.Id, i.Type) is not string key || remoteReady.Contains(key)).ToList();
                var currentHints = baseHints;
                var present = currentHints.Select(h => (h.Name.ToLowerInvariant(), h.ProductionYear)).ToHashSet();
                var presentIds = currentHints.Select(h => h.Id).ToHashSet();
                var extras = remote.Where(item => !presentIds.Contains(item.Id) && !present.Contains(((item.Name ?? "").ToLowerInvariant(), item.ProductionYear)))
                    .Select(item => new SearchHint
                    {
                        Id = item.Id,
                        Name = item.Name ?? "",
                        Type = item.Type,
                        IsFolder = item.IsFolder,
                        ProductionYear = item.ProductionYear,
                        RunTimeTicks = item.RunTimeTicks,
                        PrimaryImageTag = item.ImageTags?.GetValueOrDefault(ImageType.Primary),
                        MediaType = item.MediaType
                    }).ToArray();
                var limit = int.TryParse(context.HttpContext.Request.Query["limit"], out var n) ? Math.Clamp(n, 1, 100) : 25;
                foreach (var hint in currentHints)
                    if (itemCache.GetImageUrl(hint.Id) is string image && image.StartsWith("http://<SERVER_LAN_IP>:8092/poster/", StringComparison.Ordinal))
                        hint.PrimaryImageTag = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(image)));
                hintResult.Value = new SearchHintResult(
                    currentHints.Concat(extras).Take(limit).ToArray(),
                    currentHints.Length + extras.Length);
            }
            catch (Exception error)
            {
                logger.LogWarning("Home catalogue search unavailable ({Reason})", error.GetType().Name);
            }
        }
        if (context.Result is ObjectResult result && result.Value is PlaybackInfoResponse playback)
        {
            foreach (var source in playback.MediaSources)
            {
                var routeItem = context.RouteData.Values.TryGetValue("itemId", out var rawItemId) && Guid.TryParse(rawItemId?.ToString(), out var nativeId) ? nativeId : Guid.Empty;
                if (routeItem != Guid.Empty && source.MediaStreams.Any(s => s.Type == MediaStreamType.Video)
                    && (source.Path?.StartsWith("/media/", StringComparison.Ordinal) == true || source.Path?.StartsWith("/downloads/", StringComparison.Ordinal) == true)
                    && File.Exists(source.Path))
                {
                    // A display DTO must not mutate the native repository's
                    // subtitle codec/metadata while preparing external VTT.
                    source.MediaStreams = JsonSerializer.Deserialize<List<MediaStream>>(JsonSerializer.Serialize(source.MediaStreams))!;
                    if (context.HttpContext.Items["HomeSubtitleStreamIndex"] is int requestedNativeSub
                        && (requestedNativeSub == -1 || source.MediaStreams.Any(s => s.Type == MediaStreamType.Subtitle && s.Index == requestedNativeSub)))
                        source.DefaultSubtitleStreamIndex = requestedNativeSub;
                    if (context.HttpContext.Items["HomeAudioStreamIndex"] is int requestedNativeAudio && source.MediaStreams.Any(s => s.Type == MediaStreamType.Audio && s.Index == requestedNativeAudio))
                        source.DefaultAudioStreamIndex = requestedNativeAudio;
                    HomeAudioService.Configure(source, routeItem, playback.PlaySessionId ?? Guid.NewGuid().ToString("N"), context.HttpContext.Request);
                    continue;
                }
                if (!Uri.TryCreate(source.Path, UriKind.Absolute, out var uri)
                    || uri.Port != 8092 || uri.Host != "<SERVER_LAN_IP>"
                    || !uri.AbsolutePath.StartsWith("/play/", StringComparison.Ordinal)) continue;
                try
                {
                    var infoUri = source.Path.Replace("/play/", "/info/", StringComparison.Ordinal)
                        .Replace("&save=1", "", StringComparison.Ordinal);
                    if (context.HttpContext.Items["HomeAudioStreamIndex"] is int requestedProbeAudio)
                        infoUri += "&audio=" + requestedProbeAudio;
                    var client = clients.CreateClient();
                    client.Timeout = TimeSpan.FromSeconds(75);
                    var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
                    options.Converters.Add(new JsonStringEnumConverter());
                    var info = await client.GetFromJsonAsync<Prepared>(infoUri, options,
                        context.HttpContext.RequestAborted);
                    if (info is null || info.MediaStreams.Count == 0) throw new InvalidOperationException("No media tracks");
                    if (source.Path.Contains("&save=1", StringComparison.Ordinal)
                        && context.HttpContext.Items["HomeMediaSourceId"] is string selectedMediaSource
                        && selectedMediaSource == source.Id)
                    {
                        var downloadUri = source.Path.Replace("/play/", "/download/", StringComparison.Ordinal);
                        using var queued = await client.PostAsync(downloadUri, new StringContent("{}"), context.HttpContext.RequestAborted);
                        queued.EnsureSuccessStatusCode();
                    }
                    source.MediaStreams = info.MediaStreams;
                    foreach (var stream in source.MediaStreams.Where(s => s.Type == MediaStreamType.Subtitle))
                    {
                        if (!stream.IsExternal)
                        {
                            stream.DeliveryMethod = MediaBrowser.Model.Dlna.SubtitleDeliveryMethod.Embed;
                        }
                        else if (Uri.TryCreate(stream.DeliveryUrl, UriKind.Absolute, out var subtitleUri)
                            && subtitleUri.Host == "<SERVER_LAN_IP>" && subtitleUri.Port == 8092
                            && subtitleUri.AbsolutePath.StartsWith("/sub/", StringComparison.Ordinal))
                        {
                            var key = Uri.UnescapeDataString(subtitleUri.AbsolutePath[5..]);
                            stream.DeliveryUrl = $"/HomeCatalogue/Subtitles/{Uri.EscapeDataString(key)}/{stream.Language}.vtt{subtitleUri.Query}";
                            stream.Codec = "webvtt";
                            stream.DeliveryMethod = MediaBrowser.Model.Dlna.SubtitleDeliveryMethod.External;
                            stream.SupportsExternalStream = true;
                        }
                    }
                    source.Container = info.Container;
                    if (info.RunTimeTicks > 0) source.RunTimeTicks = info.RunTimeTicks;
                    // Completion/watch state is calculated from the indexed
                    // item too. Replace the upstream estimated duration there
                    // so watched cleanup uses the actual movie/episode length.
                    Guid? indexedId = null;
                    if (context.RouteData.Values.TryGetValue("itemId", out var routeId)
                        && Guid.TryParse(routeId?.ToString(), out var parsedId)) indexedId = parsedId;
                    var indexed = indexedId.HasValue ? library.GetItemById(indexedId.Value) : null;
                    if (indexed is null && itemCache.GetAIOStreamsMapping(source.Id) is { } mapping)
                        indexed = library.GetItemById(mapping.ItemId);
                    if (indexed?.Path?.StartsWith("/catalogue/", StringComparison.Ordinal) == true
                        && info.RunTimeTicks > 0 && indexed.RunTimeTicks != info.RunTimeTicks)
                    {
                        indexed.RunTimeTicks = info.RunTimeTicks;
                        await library.UpdateItemAsync(indexed, indexed.GetParent(), ItemUpdateType.MetadataEdit,
                            context.HttpContext.RequestAborted);
                    }
                    source.Bitrate = info.Bitrate;
                    source.DefaultSubtitleStreamIndex = info.DefaultSubtitleStreamIndex;
                    if (context.HttpContext.Items["HomeSubtitleStreamIndex"] is int requestedSubtitle
                        && (requestedSubtitle == -1 || info.MediaStreams.Any(s => s.Type == MediaStreamType.Subtitle && s.Index == requestedSubtitle)))
                        source.DefaultSubtitleStreamIndex = requestedSubtitle;
                    var audio = info.MediaStreams.Where(s => s.Type == MediaBrowser.Model.Entities.MediaStreamType.Audio).ToList();
                    source.DefaultAudioStreamIndex = info.DefaultAudioStreamIndex ?? (audio.FirstOrDefault(s => s.Language == "eng")
                        ?? audio.FirstOrDefault(s => s.Language == "heb") ?? audio.FirstOrDefault())?.Index;
                    if (context.HttpContext.Items["HomeAudioStreamIndex"] is int requestedAudio
                        && audio.Any(s => s.Index == requestedAudio)) source.DefaultAudioStreamIndex = requestedAudio;
                    source.SupportsProbing = false;
                    source.SupportsTranscoding = false;
                    if (indexedId.HasValue)
                    {
                        var key = Uri.UnescapeDataString(uri.AbsolutePath[6..]);
                        audioService.EnsureIndexed(indexedId.Value, key, itemCache.GetItem(indexedId.Value), info.RunTimeTicks);
                        audioService.Register(indexedId.Value, source);
                        HomeAudioService.Configure(source, indexedId.Value, playback.PlaySessionId ?? Guid.NewGuid().ToString("N"), context.HttpContext.Request);
                    }
                }
                catch (Exception error)
                {
                    logger.LogWarning("Home catalogue source unavailable ({Reason})", error.GetType().Name);
                    playback.MediaSources = Array.Empty<MediaSourceInfo>();
                    playback.ErrorCode = MediaBrowser.Model.Dlna.PlaybackErrorCode.NoCompatibleStream;
                    break;
                }
            }
            // Android TV 0.19.10 treats a successful empty response as a stream
            // and dereferences its null MediaSource. Use the documented error.
            if (playback.MediaSources.Count == 0 && playback.ErrorCode is null)
                playback.ErrorCode = MediaBrowser.Model.Dlna.PlaybackErrorCode.NoCompatibleStream;
        }
        await next();
    }
}
