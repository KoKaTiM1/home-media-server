using System.Reflection;
using System.Security.Claims;
using System.Text.Json;
using Jellyfin.Data;
using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.DynamicLibrary.Filters;
using Jellyfin.Plugin.DynamicLibrary.Services;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.AspNetCore.Mvc.ModelBinding;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.Caching.Memory;
using Microsoft.Extensions.Logging.Abstractions;

static class RecheckChecks
{
    static void Check(bool ok, string message) { if (!ok) throw new Exception(message); }
    public static async Task Run()
    {
        Check(HomeRecheckDeleteFilter.ScopeFromPath("/catalogue/movies/tt88888888/movie.strm") == "movie:tt88888888", "Movie scope");
        Check(HomeRecheckDeleteFilter.ScopeFromPath("/catalogue/shows/tt88888888") == "series:tt88888888", "Whole series scope");
        Check(HomeRecheckDeleteFilter.ScopeFromPath("/catalogue/shows/tt88888888/Season 01") == "series:tt88888888:1", "Season scope");
        Check(HomeRecheckDeleteFilter.ScopeFromPath("/catalogue/shows/tt88888888/Season 01/S01E02.strm") == "series:tt88888888:1:2", "Episode scope");
        foreach (var path in new[] { "/media/movies/real.mp4", "/downloads/movie.mkv", "/catalogue/movies/tt88888888/movie.mp4", "/catalogue/shows/tt88888888/Season 01/S02E01.strm", "/catalogue/movies/../movie.strm" })
            Check(HomeRecheckDeleteFilter.ScopeFromPath(path) is null, "Keep local/invalid paths outside recheck handling");
        var library = DispatchProxy.Create<ILibraryManager, LibraryProxy>();
        var backing = (LibraryProxy)(object)library;
        using var memory = new MemoryCache(new MemoryCacheOptions());
        var cache = new DynamicItemCache(memory, NullLogger<DynamicItemCache>.Instance);
        var root = Path.Combine(Path.GetTempPath(), "recheck-" + Guid.NewGuid().ToString("N"));
        var fixture = Path.Combine(root,"actual-movie.strm");Directory.CreateDirectory(root);File.WriteAllText(fixture,"preserve");
        var online = Guid.NewGuid();var local = Guid.NewGuid();
        backing.Items[online] = new Movie { Id = online, Path = "/catalogue/movies/tt88888888/movie.strm" };
        backing.Items[local] = new Movie { Id = local, Path = "/media/movies/real.mp4" };
        var userManager = DispatchProxy.Create<IUserManager, RecheckUserProxy>();
        var user = ((RecheckUserProxy)(object)userManager).User;
        var store = new HomeRecheckStore(Path.Combine(root,"pending"));
        var filter = new HomeRecheckDeleteFilter(library,userManager,cache,store,NullLogger<HomeRecheckDeleteFilter>.Instance);
        ActionExecutingContext Context(Dictionary<string,object?> args, bool apiKey = true, bool authenticated = true)
        {
            var http = new DefaultHttpContext();http.Request.Method="DELETE";
            var claims = apiKey ? new[] {new Claim("Jellyfin-IsApiKey","true")} : new[] {new Claim("Jellyfin-UserId",user.Id.ToString())};
            http.User=new ClaimsPrincipal(new ClaimsIdentity(claims,authenticated ? "fixture" : null));
            var action = new ControllerActionDescriptor {ControllerName="Library",ActionName=args.ContainsKey("ids") ? "DeleteItems" : "DeleteItem"};
            return new ActionExecutingContext(new ActionContext(http,new RouteData(),action,new ModelStateDictionary()),new List<IFilterMetadata>(),args,new object());
        }
        var nativeCalls=0;
        ActionExecutionDelegate Next(ActionExecutingContext c) => () =>
        {
            nativeCalls++;
            return Task.FromResult(new ActionExecutedContext(c,c.Filters,new object()) {Result=new NoContentResult()});
        };
        try
        {
            var request=Context(new() {{"itemId",online}});await filter.OnActionExecutionAsync(request,Next(request));
            Check(request.Result is NoContentResult && nativeCalls==0,"Online Delete intercepts before native deletion");
            var events=Directory.GetFiles(Path.Combine(root,"pending"),"*.json");Check(events.Length==1,"One durable event");
            using var json=JsonDocument.Parse(File.ReadAllText(events[0]));Check(json.RootElement.GetProperty("keys")[0].GetString()=="movie:tt88888888","Correct report scope persisted");
            Check(File.ReadAllText(fixture)=="preserve" && backing.Items.ContainsKey(online),"Existing files and native records remain");
            var blocked=Context(new() {{"itemId",online}},authenticated:false);await filter.OnActionExecutionAsync(blocked,Next(blocked));
            Check(blocked.Result is UnauthorizedResult && nativeCalls==0,"Anonymous request cannot queue reports");
            var previewPath="/catalogue/movies/tt77777777/movie.strm";
            var previewId=library.GetNewItemId(previewPath,typeof(Movie));
            cache.StoreItem(new BaseItemDto {Id=previewId,Type=BaseItemKind.Movie,ProviderIds=new() {{"Imdb","tt77777777"},{"DynamicLibrary","pending:imdb:tt77777777"}}});
            user.SetPermission(PermissionKind.EnableContentDeletion,false);
            var forbidden=Context(new() {{"itemId",previewId}},apiKey:false);forbidden.HttpContext.Request.QueryString=new QueryString("?userId="+Guid.NewGuid());
            await filter.OnActionExecutionAsync(forbidden,Next(forbidden));
            Check(forbidden.Result is UnauthorizedResult && nativeCalls==0,"Deletion permission enforced; query userId cannot elevate");
            user.SetPermission(PermissionKind.EnableContentDeletion,true);user.SetPermission(PermissionKind.EnableAllFolders,true);
            var allowed=Context(new() {{"itemId",previewId}},apiKey:false);await filter.OnActionExecutionAsync(allowed,Next(allowed));
            Check(allowed.Result is NoContentResult && nativeCalls==0,"Authorized canonical preview can be reported before indexing");
            var mixed=Context(new() {{"ids",new[] {online,local}}});await filter.OnActionExecutionAsync(mixed,Next(mixed));
            Check(mixed.Result is BadRequestObjectResult && nativeCalls==0,"Mixed batches cannot fall through to real deletion");
            var normal=Context(new() {{"itemId",local}});await filter.OnActionExecutionAsync(normal,Next(normal));Check(nativeCalls==1,"Local delete keeps native handling");
            var badStore=new HomeRecheckStore(fixture);
            var failing=new HomeRecheckDeleteFilter(library,userManager,cache,badStore,NullLogger<HomeRecheckDeleteFilter>.Instance);
            var failure=Context(new() {{"itemId",online}});await failing.OnActionExecutionAsync(failure,Next(failure));
            Check(failure.Result is ObjectResult {StatusCode:503} && nativeCalls==1,"A failed queue write never falls back to deleting files");
        }
        finally {Directory.Delete(root,true);}
        Console.WriteLine("PASS: online Delete durable recheck/no native deletion, canonical preview, movie/show/season/episode scope, auth/permissions, mixed batch and write-failure guards");
    }
}
public class RecheckUserProxy : DispatchProxy
{
    public User User {get;}=new("fixture","fixture","fixture");
    protected override object? Invoke(MethodInfo? method,object?[]? args)
        => method!.Name=="GetUserById" ? User : throw new NotSupportedException(method.Name);
}
