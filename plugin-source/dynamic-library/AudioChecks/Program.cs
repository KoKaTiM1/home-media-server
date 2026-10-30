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
var localized = new BaseItemDto { Id=Guid.NewGuid(), Name="English", Overview="English overview", ProductionYear=2001,
    UserData=new UserItemDataDto { Key="locale-fixture", IsFavorite=true, Played=true, PlaybackPositionTicks=123 },
    ImageTags=new Dictionary<ImageType,string> { [ImageType.Primary]="unchanged-poster" } };
var localizedId=localized.Id;var localizedState=localized.UserData;
Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.ApplySavedLabels(localized,"שם בעברית",null,"English");
Check(localized.Name=="שם בעברית" && localized.Overview=="English overview" && localized.OriginalTitle=="English", "Title-only Hebrew keeps English overview");
Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.ApplySavedLabels(localized,null,"תיאור בעברית",null);
Check(localized.Name=="שם בעברית" && localized.Overview=="תיאור בעברית" && localized.Id==localizedId && localized.ProductionYear==2001
    && ReferenceEquals(localized.UserData,localizedState) && localized.ImageTags[ImageType.Primary]=="unchanged-poster", "Description update preserves identity, artwork and user state");
Console.WriteLine("PASS: cached Hebrew fields independently preserve English fallback, poster, identity and user state");
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
var originalPlayback = new Uri("http://example.invalid:8092/play/movie:tt00000000?sig=old");
var verifiedPlayback = "http://example.invalid:8092/play/movie:tt00000000?source=" + new string('a',64) + "&sig=" + new string('b',64);
Check(Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.VerifiedPlaybackPath(originalPlayback,verifiedPlayback)==verifiedPlayback, "Exact source URL retained");
foreach (var invalid in new[] { null, originalPlayback.AbsoluteUri, verifiedPlayback.Replace("example.invalid","other.invalid"), verifiedPlayback.Replace("tt00000000","tt00000001"), verifiedPlayback.Replace("source=", "untrusted=") })
{
    var rejected=false;
    try { Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter.VerifiedPlaybackPath(originalPlayback,invalid); }
    catch (InvalidOperationException) { rejected=true; }
    Check(rejected,"Unbound or cross-title source rejected");
}
Console.WriteLine("PASS: exact verified playback binding and native HLS source identity");
var seriesProgress = new BaseItemDto
{
    Type = Jellyfin.Data.Enums.BaseItemKind.Series, RecursiveItemCount = 2,
    UserData = new UserItemDataDto { Key = "fixture-watch", Played = true, UnplayedItemCount = 0, IsFavorite = true, PlayCount = 4 }
};
HomeWatchStateService.Apply(seriesProgress, 10);
Check(!seriesProgress.UserData.Played && seriesProgress.UserData.UnplayedItemCount == 8, "Two verified episodes cannot complete a ten-episode show");
Check(seriesProgress.UserData.PlayCount == 4 && seriesProgress.UserData.IsFavorite, "Aggregate correction preserves account fields");
HomeWatchStateService.Apply(seriesProgress, 10);
Check(seriesProgress.UserData.UnplayedItemCount == 8, "Aggregate correction is idempotent");
seriesProgress.RecursiveItemCount = 10;seriesProgress.UserData.UnplayedItemCount = 0;
HomeWatchStateService.Apply(seriesProgress, 10);
Check(seriesProgress.UserData.Played, "All released episodes watched gives whole-show checkmark");
HomeWatchStateService.Apply(seriesProgress, 11);
Check(!seriesProgress.UserData.Played && seriesProgress.UserData.UnplayedItemCount == 1, "New released episode removes completion checkmark");
var movieProgress = new BaseItemDto { Type = Jellyfin.Data.Enums.BaseItemKind.Movie, UserData = new UserItemDataDto { Key = "fixture-watch", Played = true } };
HomeWatchStateService.Apply(movieProgress, 12);
Check(movieProgress.UserData.Played, "Movie watch state unchanged");
Console.WriteLine("PASS: whole-show completion, missing episodes, new episodes, idempotence, movie and account state preservation");
var hebrewOptions = new MediaSourceInfo
{
    MediaStreams = new List<MediaStream>
    {
        new() { Type = MediaStreamType.Subtitle, Index = 2, Language = "heb", IsExternal = true, Title = "HEB (downloaded)", DeliveryUrl = "/first?variant=aaaaaaaaaaaaaaaa" },
        new() { Type = MediaStreamType.Subtitle, Index = 3, Language = "heb", IsExternal = true, Title = "HEB (downloaded)", DeliveryUrl = "/second?variant=bbbbbbbbbbbbbbbb" },
        new() { Type = MediaStreamType.Subtitle, Index = 4, Language = "eng", Title = "English" }
    }, DefaultSubtitleStreamIndex = -1
};
HomeSubtitleTracks.Label(hebrewOptions);
Check(hebrewOptions.MediaStreams[0].Title == "עברית 1" && hebrewOptions.MediaStreams[1].Title == "עברית 2", "Hebrew choices have clear Hebrew names");
Check(hebrewOptions.MediaStreams[0].DisplayTitle.StartsWith("עברית 1") && hebrewOptions.MediaStreams[1].DisplayTitle.StartsWith("עברית 2"), "Serialized display names start in Hebrew");
Check(hebrewOptions.MediaStreams[0].Language == "heb" && hebrewOptions.MediaStreams[1].Language == "heb", "Language identifiers remain valid for automatic selection");
Check(hebrewOptions.MediaStreams[0].DeliveryUrl != hebrewOptions.MediaStreams[1].DeliveryUrl, "Hebrew choices retain distinct URLs");
Check(hebrewOptions.DefaultSubtitleStreamIndex == -1 && hebrewOptions.MediaStreams[2].Title == "English", "Explicit None and other languages preserved");
var relayOne = HomeSubtitleTracks.RelayUrl(new Uri("http://catalogue-bridge:8092/sub/movie:tt00000000?sig=" + new string('a',64) + "&lang=heb&variant=aaaaaaaaaaaaaaaa"), "heb");
var relayTwo = HomeSubtitleTracks.RelayUrl(new Uri("http://catalogue-bridge:8092/sub/movie:tt00000000?sig=" + new string('a',64) + "&lang=heb&variant=bbbbbbbbbbbbbbbb"), "heb");
Check(relayOne != relayTwo && relayOne.Contains("variant=aaaaaaaaaaaaaaaa") && relayTwo.Contains("variant=bbbbbbbbbbbbbbbb"), "Playback response relay preserves distinct variant identities");
var subFactory = new SubtitleFactory();
var subController = new Jellyfin.Plugin.DynamicLibrary.Api.HomeCatalogueSubtitleController(subFactory)
{
    ControllerContext = new Microsoft.AspNetCore.Mvc.ControllerContext { HttpContext = new DefaultHttpContext() }
};
var firstSub = await subController.Get("movie:tt00000000", "heb", "vtt", new string('a',64), "aaaaaaaaaaaaaaaa");
var firstText = (firstSub as Microsoft.AspNetCore.Mvc.ContentResult)?.Content;
var secondSub = await subController.Get("movie:tt00000000", "heb", "vtt", new string('a',64), "bbbbbbbbbbbbbbbb");
var secondText = (secondSub as Microsoft.AspNetCore.Mvc.ContentResult)?.Content;
Check(firstText?.StartsWith("WEBVTT") == true && firstText.Contains("ראשון") && secondText?.Contains("שני") == true, "Two Hebrew variants reach distinct VTT tracks through native relay");
var invalidSub = await subController.Get("movie:tt00000000", "heb", "vtt", new string('a',64), "../../secret");
Check(invalidSub is Microsoft.AspNetCore.Mvc.BadRequestResult && subFactory.Handler.Requests == 2, "Invalid variant rejected before network");
Console.WriteLine("PASS: two Hebrew labels/display names, stable language IDs, distinct variant relay/VTT, None/English preservation and invalid variant rejection");
await RecheckChecks.Run();
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
var imageFactory = new ImageFactory();
using (var imageCache = new HomeImageService(imageFactory))
{
    var poster = "http://catalogue-bridge:8092/poster/movie/tt00000000.jpg?style=4&badge=HE";
    Check(HomeImageService.IsLocalPoster(poster), "Identify local poster route");
    Check(!HomeImageService.IsLocalPoster("https://example.invalid/picture.jpg"), "Leave external images unchanged");
    var images = await Task.WhenAll(Enumerable.Range(0, 12).Select(_ => imageCache.GetAsync(poster, CancellationToken.None)));
    Check(imageFactory.Handler.Requests == 1, "Simultaneous poster requests share one fetch");
    Check(images.All(i => i.ETag == images[0].ETag), "Stable image content tags");
    var changed = await imageCache.GetAsync(poster.Replace("badge=HE", "badge=EN"), CancellationToken.None);
    Check(imageFactory.Handler.Requests == 2 && changed.ETag != images[0].ETag, "Changed badge fetches fresh image bytes");
    using var cancelled = new CancellationTokenSource();cancelled.Cancel();
    try { await imageCache.GetAsync(poster, cancelled.Token);throw new Exception("Cancelled poster request accepted"); }
    catch (OperationCanceledException) { }
}
Console.WriteLine("PASS: bounded artwork cache, shared concurrent fetch, changed image identity, cancellation");
Directory.CreateDirectory("/catalogue");
var foreground = new HomeForegroundService();
var navigation = new DefaultHttpContext();
navigation.Request.Method = "GET";
navigation.User = new System.Security.Claims.ClaimsPrincipal(new System.Security.Claims.ClaimsIdentity(new[] { new System.Security.Claims.Claim("role", "test") }, "fixture"));
foreground.Touch(navigation.Request, "Items", "GetItems");
Check(!File.Exists("/catalogue/.foreground-read-until"), "Maintenance API calls must not pause validation");
navigation.Request.Headers["X-Emby-Authorization"] = "MediaBrowser Client=\"Fixture\"";
foreground.Touch(navigation.Request, "Items", "GetItems");
var lease = long.Parse(File.ReadAllText("/catalogue/.foreground-read-until"));
Check(lease > DateTimeOffset.UtcNow.ToUnixTimeSeconds() && lease <= DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 30, "Navigation lease is bounded to 30 seconds");
File.Delete("/catalogue/.foreground-read-until");
foreground.Touch(navigation.Request, "Image", "GetItemImage");
Check(!File.Exists("/catalogue/.foreground-read-until"), "Image requests must not extend navigation lease");
Console.WriteLine("PASS: bounded navigation priority, maintenance and images do not pause workers");
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


BaseItemDto SearchFixture(string imdb, string name, int year) => new()
{
    Id = Guid.NewGuid(), Type = Jellyfin.Data.Enums.BaseItemKind.Movie, Name = name, ProductionYear = year,
    ProviderIds = new Dictionary<string, string> { ["Imdb"] = imdb }
};
var localFurious = SearchFixture("tt1000001", "The Furious", 2026);
localFurious.UserData = new() { Key = "fixture-search", IsFavorite = true, PlaybackPositionTicks = 12345 };
var canonicalFurious = SearchFixture("tt1000001", "The Furious", 2025);
var distinctFurious = SearchFixture("tt1000002", "The Furious", 2026);
var partialFurious = SearchFixture("tt1000003", "The Fast and the Furious", 2001);
var searchMerged = HomeSearch.Merge(new[] { partialFurious, localFurious }, new[] { canonicalFurious, distinctFurious }, "The Furious");
Check(searchMerged.Count == 3, "Search identity dedup preserves distinct releases");
Check(searchMerged.IndexOf(partialFurious) == 2, "Search relevance precedes native/provider origin");
Check(searchMerged.Single(i => i.Id == localFurious.Id).ProductionYear == 2025, "Same-identity canonical year corrects response");
Check(localFurious.UserData.IsFavorite && localFurious.UserData.PlaybackPositionTicks == 12345, "Search retains native favorite and progress");
var yearMerged = HomeSearch.Merge(new[] { partialFurious, localFurious }, new[] { distinctFurious }, "The Furious 2025");
Check(yearMerged[0].Id == localFurious.Id, "Explicit year affects relevance");
var manySearch = Enumerable.Range(0, 60).Select(i => SearchFixture("tt" + (2000000+i), "Fixture " + i.ToString("D2"), 2025)).ToArray();
var allSearch = HomeSearch.Merge(manySearch.Take(30), manySearch.Skip(20), "Fixture");
var searchPage1 = allSearch.Skip(0).Take(25).ToArray();
var searchPage2 = allSearch.Skip(25).Take(25).ToArray();
Check(allSearch.Count == 60 && searchPage1.Select(i => i.Id).Intersect(searchPage2.Select(i => i.Id)).Count() == 0, "Combined search paginates once without duplicates");
Check(HomeSearch.Plan(25,25) == new HomeSearch.Page(25,25,5000), "Native prefix retained for combined search page");
Check(HomeSearch.Plan(int.MaxValue,25).Fetch == int.MaxValue, "Search paging arithmetic does not overflow");
Console.WriteLine("PASS: search identity/year/relevance/native-state/pagination checks");


var readyCache = new DynamicItemCache(memory, Microsoft.Extensions.Logging.Abstractions.NullLogger<DynamicItemCache>.Instance);
var homeFilter = new Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter(new ReadyFactory(),
    Microsoft.Extensions.Logging.Abstractions.NullLogger<Jellyfin.Plugin.DynamicLibrary.Filters.HomeCatalogueResultFilter>.Instance,
    null!, proxy, readyCache, indexer);
foreach (var item in new[] { localFurious, distinctFurious, partialFurious })
{
    var nativeMovie = new MediaBrowser.Controller.Entities.Movies.Movie { Id = item.Id, Name = item.Name, Path = "/catalogue/movies/" + item.ProviderIds["Imdb"] + "/movie.strm" };
    nativeMovie.ProviderIds["Imdb"] = item.ProviderIds["Imdb"];backing.Items[item.Id] = nativeMovie;
}
var readyNative = await homeFilter.FilterSearchReadyAsync(new[] { partialFurious, localFurious, distinctFurious }, false, CancellationToken.None);
var firstReadyPage = HomeSearch.Merge(readyNative, Array.Empty<BaseItemDto>(), "The Furious").Take(2).ToArray();
Check(firstReadyPage.Length == 2 && firstReadyPage.All(i => i.Id != partialFurious.Id), "Unavailable native hits filtered before paging, no empty first page");
Check((await homeFilter.FilterSearchReadyAsync(new[] { partialFurious, localFurious }, true, CancellationToken.None)).Count == 2, "Explicit favorite search preserves unavailable saved intent");
var action = new Microsoft.AspNetCore.Mvc.ActionContext(new DefaultHttpContext(), new Microsoft.AspNetCore.Routing.RouteData(),
    new Microsoft.AspNetCore.Mvc.Controllers.ControllerActionDescriptor { ActionName = "GetSearchHints", ControllerName = "Search" },
    new Microsoft.AspNetCore.Mvc.ModelBinding.ModelStateDictionary());
var executing = new Microsoft.AspNetCore.Mvc.Filters.ActionExecutingContext(action, Array.Empty<Microsoft.AspNetCore.Mvc.Filters.IFilterMetadata>(),
    new Dictionary<string, object?> { ["searchTerm"] = "The Furious" }, new object());
Check(Jellyfin.Plugin.DynamicLibrary.Extensions.ActionContextExtensions.IsApiSearchAction(executing), "Installed Jellyfin 10.11 GetSearchHints endpoint recognized");
Console.WriteLine("PASS: native readiness before search paging and installed TV search action");

// Connection selection preserves every byte of the signed path/query.
var remoteContext = new Microsoft.AspNetCore.Http.DefaultHttpContext();
remoteContext.Request.Host = new Microsoft.AspNetCore.Http.HostString("remote.example.invalid", 8096);
var boundMedia = "http://example.invalid:8092/play/movie:tt00000000?source=" + new string('a',64) + "&sig=" + new string('b',64) + "&audio=1";
var remoteMedia = HomeConnectionPaths.Rewrite(boundMedia, remoteContext.Request, new[] { "remote.example.invalid" });
if (new Uri(remoteMedia).Host != "remote.example.invalid" || new Uri(remoteMedia).PathAndQuery != new Uri(boundMedia).PathAndQuery) throw new Exception("Remote playback binding changed");
if (HomeConnectionPaths.Rewrite(boundMedia, remoteContext.Request, Array.Empty<string>()) != boundMedia) throw new Exception("Unconfigured host accepted");
remoteContext.Request.Host = new Microsoft.AspNetCore.Http.HostString("local.example",8096);
if (HomeConnectionPaths.Rewrite(boundMedia, remoteContext.Request, new[] { "remote.example.invalid" }) != boundMedia) throw new Exception("LAN source changed");
Console.WriteLine("Connection routing checks passed");

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

public sealed class ImageFactory : IHttpClientFactory
{
    public readonly ImageHandler Handler = new();
    public HttpClient CreateClient(string name) => new(Handler, disposeHandler: false);
}
public sealed class ImageHandler : HttpMessageHandler
{
    public int Requests;
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellation)
    {
        Interlocked.Increment(ref Requests);await Task.Delay(25, cancellation);
        return new HttpResponseMessage(System.Net.HttpStatusCode.OK)
        {
            Content = new ByteArrayContent(System.Text.Encoding.UTF8.GetBytes(request.RequestUri!.Query))
        };
    }
}

sealed class SubtitleFactory : IHttpClientFactory
{
    public SubtitleHandler Handler { get; } = new();
    public HttpClient CreateClient(string name) => new(Handler, disposeHandler: false);
}
sealed class SubtitleHandler : HttpMessageHandler
{
    public int Requests;
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        Requests++;
        var text = request.RequestUri!.Query.Contains("variant=aaaaaaaaaaaaaaaa") ? "ראשון" : "שני";
        return Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.OK)
        { Content = new StringContent("1\n00:00:01,000 --> 00:00:02,000\n" + text + "\n", Encoding.UTF8) });
    }
}

sealed class ReadyFactory : IHttpClientFactory
{
    public HttpClient CreateClient(string name) => new(new ReadyHandler());
}
sealed class ReadyHandler : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        => Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.OK)
        { Content = new StringContent("{\"titles\":[\"movie:tt1000001\",\"movie:tt1000002\"]}", Encoding.UTF8, "application/json") });
}

