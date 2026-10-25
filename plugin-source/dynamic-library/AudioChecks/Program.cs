using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using Jellyfin.Plugin.DynamicLibrary.Services;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Caching.Memory;

static void Check(bool condition, string name) { if (!condition) throw new Exception(name); }
static MediaSourceInfo Source(string profile) => new()
{
    Id = "fixture-source", Path = "http://<SERVER_LAN_IP>:8092/play/movie:tt1254207?fixture=1", Name = "Fixture",
    SupportsDirectPlay = true, DefaultAudioStreamIndex = 1, DefaultSubtitleStreamIndex = 2,
    MediaStreams = new List<MediaStream> {
        new() { Index = 0, Type = MediaStreamType.Video, Codec = "h264" },
        new() { Index = 1, Type = MediaStreamType.Audio, Codec = "aac", Profile = profile },
        new() { Index = 2, Type = MediaStreamType.Subtitle, Codec = "srt", Language = "heb" }
    }
};
var favoriteContext = new DefaultHttpContext();
favoriteContext.Request.QueryString = new QueryString("?Filters=IsFavorite");
Check(Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.IsFavoritesQuery(favoriteContext.Request.Query), "Native favorites filter");
favoriteContext.Request.QueryString = new QueryString("?isFavorite=true");
Check(Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.IsFavoritesQuery(favoriteContext.Request.Query), "Explicit favorite query");
favoriteContext.Request.QueryString = new QueryString("?isFavorite=false&Filters=IsUnplayed");
Check(!Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.IsFavoritesQuery(favoriteContext.Request.Query), "False favorite query stays filtered");
var noSources = new HashSet<string>();
Check(Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.KeepInResults(true, "movie:tt00000000", noSources), "Unavailable favorites stay visible");
Check(!Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.KeepInResults(false, "movie:tt00000000", noSources), "Unavailable ordinary titles stay hidden");
Check(Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.KeepInResults(false, null, noSources), "Local media stays visible");
Console.WriteLine("PASS: favorites query variants, unavailable favorites preserved, normal ready-only filtering");
var context = new DefaultHttpContext();
context.Request.Headers["Authorization"] = "MediaBrowser Client=\"Fixture\", DeviceId=\"test-device\", Token=\"test-playback-token\"";
var id = Guid.NewGuid();
var direct = Source("LC");
HomeAudioService.Configure(direct, id, "test-session", context.Request);
Check(direct.SupportsDirectPlay && direct.TranscodingUrl is null, "AAC-LC must stay direct");
var source = Source("Main");
using var memory = new MemoryCache(new MemoryCacheOptions());
var sources = new HomeAudioService(memory, null!);
sources.Register(id, source);
HomeAudioService.Configure(source, id, "test-session", context.Request);
Check(!source.SupportsDirectPlay && !source.SupportsDirectStream && source.SupportsTranscoding, "Main must use conversion");
Check(source.TranscodingUrl!.Contains("VideoCodec=copy") && source.TranscodingUrl.Contains("AllowAudioStreamCopy=false"), "Audio only; never video encode");
Check(source.TranscodingUrl.Contains("AudioCodec=aac") && source.TranscodingUrl.Contains("DeviceId=test-device"), "AAC target and device identity");
Check(source.TranscodingUrl.Contains("api_key=test-playback-token"), "Use caller authorization");
var sub = source.MediaStreams.Single(s => s.Type == MediaStreamType.Subtitle);
Check(sub.IsExternal && sub.Codec == "webvtt" && sub.DeliveryUrl.Contains("/Subtitles/2/0/Stream.vtt"), "Embedded text subtitle delivery");
var original = sources.Sources(id).Single();
Check(original.MediaStreams.Single(s => s.Type == MediaStreamType.Subtitle).Codec == "srt", "Provider must retain original metadata");
Check(original.TranscodingUrl is null, "Provider must not cache user's token");
Console.WriteLine("PASS: LC direct, Main audio-only HLS, authorization, device cleanup identity, subtitle mapping, immutable provider source");

// A newly browsed title may precede the asynchronous library scan. Exercise
// creation against the real library interface without touching a real DB.
var proxy = DispatchProxy.Create<ILibraryManager, LibraryProxy>();
var backing = (LibraryProxy)(object)proxy;
var rootMovie = Guid.Parse("<ONLINE_MOVIES_LIBRARY_ID>");
var rootShow = Guid.Parse("<ONLINE_SHOWS_LIBRARY_ID>");
backing.Items[rootMovie] = new Folder { Id = rootMovie, Name = "Movies" };
backing.Items[rootShow] = new Folder { Id = rootShow, Name = "Shows" };
var fixture = "/catalogue/movies/tt00000000/movie.strm";
Check(!File.Exists(fixture),"Run fixture checks in a clean SDK container");
Directory.CreateDirectory(Path.GetDirectoryName(fixture)!);File.WriteAllText(fixture,"fixture");
var indexer = new HomeAudioService(memory, proxy);
var movieId = Guid.NewGuid();
indexer.EnsureIndexed(movieId,"movie:tt00000000",new BaseItemDto { Name = "Fixture movie" },600000000);
Check(backing.Items[movieId].ParentId==rootMovie && backing.Items[movieId].RunTimeTicks==600000000,"Canonical movie parent and duration");
var seriesPath="/catalogue/shows/tt00000000";
var episodePath=seriesPath+"/Season 01/S01E01.strm";
Check(!File.Exists(episodePath),"Run episode fixture checks in a clean SDK container");
Directory.CreateDirectory(Path.GetDirectoryName(episodePath)!);File.WriteAllText(episodePath,"fixture");
var episodeId=Guid.NewGuid();
indexer.EnsureIndexed(episodeId,"series:tt00000000:1:1",new BaseItemDto { Name="Pilot",SeriesName="Fixture series" },300000000);
var indexedEpisode=(Episode)backing.Items[episodeId];
Check(backing.Items.ContainsKey(indexedEpisode.SeriesId) && backing.Items.ContainsKey(indexedEpisode.SeasonId),"Series/season indexes created");
Check(indexedEpisode.ParentIndexNumber==1 && indexedEpisode.IndexNumber==1,"Episode indexes preserved");
var count=backing.Items.Count;indexer.EnsureIndexed(movieId,"movie:tt00000000",null,1);
Check(backing.Items.Count==count && backing.Items[movieId].RunTimeTicks==600000000,"Existing item never overwritten");
Console.WriteLine("PASS: first-open movie/series/season/episode persistence and no overwrite");

public class LibraryProxy : DispatchProxy
{
    public Dictionary<Guid,BaseItem> Items { get; } = new();
    protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
    {
        args ??= Array.Empty<object>();
        if (targetMethod!.Name=="GetItemById") return Items.GetValueOrDefault((Guid)args[0]!);
        if (targetMethod.Name=="GetNewItemId") return new Guid(MD5.HashData(Encoding.UTF8.GetBytes(args[0]+":"+args[1])));
        if (targetMethod.Name=="CreateItem")
        {
            var item=(BaseItem)args[0]!;var parent=args[1] as BaseItem;
            item.ParentId=parent?.Id ?? Guid.Empty;Items.Add(item.Id,item);return null;
        }
        throw new NotSupportedException(targetMethod.Name);
    }
}
