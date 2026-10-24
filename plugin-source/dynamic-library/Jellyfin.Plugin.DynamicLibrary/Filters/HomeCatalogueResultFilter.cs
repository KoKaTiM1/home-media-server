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
    DynamicLibraryService catalogues, ILibraryManager library, DynamicItemCache itemCache) : IAsyncAlwaysRunResultFilter, IAsyncActionFilter
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
    }

    private sealed class TitleLabels
    {
        public string? Hebrew { get; set; }
        public string? English { get; set; }
        public List<MediaUrl>? Trailers { get; set; }
    }

    public async Task OnResultExecutionAsync(ResultExecutingContext context, ResultExecutionDelegate next)
    {
        if (context.Result is ObjectResult titleResult && titleResult.Value is BaseItemDto title
            && title.Type is BaseItemKind.Movie or BaseItemKind.Series
            && title.ProviderIds?.TryGetValue("Imdb", out var titleImdb) == true)
        {
            try
            {
                var labelsClient = clients.CreateClient();
                labelsClient.Timeout = TimeSpan.FromSeconds(2);
                var kind = title.Type == BaseItemKind.Movie ? "movie" : "series";
                var labels = await labelsClient.GetFromJsonAsync<TitleLabels>(
                    "http://catalogue-bridge:8092/label/" + Uri.EscapeDataString(titleImdb) + "?kind=" + kind, context.HttpContext.RequestAborted);
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
            try
            {
                var types = new HashSet<BaseItemKind> { BaseItemKind.Movie, BaseItemKind.Series };
                if (context.HttpContext.Request.Query.TryGetValue("includeItemTypes", out var included))
                {
                    types.RemoveWhere(t => !included.ToString().Split(',').Contains(t.ToString(), StringComparer.OrdinalIgnoreCase));
                }
                var remote = await catalogues.SearchAsync(term.ToString(), types,
                    context.HttpContext.RequestAborted);
                var present = hints.SearchHints.Select(h => (h.Name.ToLowerInvariant(), h.ProductionYear)).ToHashSet();
                var presentIds = hints.SearchHints.Select(h => h.Id).ToHashSet();
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
                hintResult.Value = new SearchHintResult(
                    hints.SearchHints.Concat(extras).Take(limit).ToArray(),
                    hints.TotalRecordCount + extras.Length);
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
                if (!Uri.TryCreate(source.Path, UriKind.Absolute, out var uri)
                    || uri.Port != 8092 || uri.Host != "<SERVER_LAN_IP>"
                    || !uri.AbsolutePath.StartsWith("/play/", StringComparison.Ordinal)) continue;
                try
                {
                    var infoUri = source.Path.Replace("/play/", "/info/", StringComparison.Ordinal)
                        .Replace("&save=1", "", StringComparison.Ordinal);
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
                    source.DefaultAudioStreamIndex = (audio.FirstOrDefault(s => s.Language == "eng")
                        ?? audio.FirstOrDefault(s => s.Language == "heb") ?? audio.FirstOrDefault())?.Index;
                    if (context.HttpContext.Items["HomeAudioStreamIndex"] is int requestedAudio
                        && audio.Any(s => s.Index == requestedAudio)) source.DefaultAudioStreamIndex = requestedAudio;
                    source.SupportsProbing = false;
                    source.SupportsTranscoding = false;
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
