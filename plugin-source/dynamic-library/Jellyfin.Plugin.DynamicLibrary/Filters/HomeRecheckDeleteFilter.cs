using System.Security.Claims;
using System.Text.RegularExpressions;
using Jellyfin.Data;
using Jellyfin.Data.Enums;
using Jellyfin.Database.Implementations.Entities;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Plugin.DynamicLibrary.Services;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.DynamicLibrary.Filters;

// Online Delete means report/recheck. Never call native DeleteItem for these
// virtual records: STRM metadata, images, downloaded copies and UserData stay.
public sealed class HomeRecheckDeleteFilter(ILibraryManager library, IUserManager users,
    DynamicItemCache cache, HomeRecheckStore reports, ILogger<HomeRecheckDeleteFilter> logger)
    : IAsyncActionFilter, IAsyncAlwaysRunResultFilter, IOrderedFilter
{
    public int Order => -90;

    public static string? ScopeFromPath(string? path)
    {
        if (path is null) return null;
        var movie = Regex.Match(path, @"^/catalogue/movies/(tt\d{5,12})/movie\.strm$");
        if (movie.Success) return "movie:" + movie.Groups[1].Value;
        var show = Regex.Match(path, @"^/catalogue/shows/(tt\d{5,12})(?:/Season (\d{2,3})(?:/S(\d{2,3})E(\d{2,4})\.strm)?)?$");
        if (!show.Success) return null;
        var scope = "series:" + show.Groups[1].Value;
        if (!show.Groups[2].Success) return scope;
        var season = int.Parse(show.Groups[2].Value);
        if (season > 300) return null;
        scope += ":" + season;
        if (!show.Groups[3].Success) return scope;
        var episode = int.Parse(show.Groups[4].Value);
        return int.Parse(show.Groups[3].Value) == season && episode is > 0 and <= 2000 ? scope + ":" + episode : null;
    }

    public string? ScopeFor(Guid id) => Resolve(id);

    private string? Resolve(Guid id, BaseItemDto? candidate = null)
    {
        var native = library.GetItemById(id);
        if (native is not null) return ScopeFromPath(native.Path);
        var dto = candidate ?? cache.GetItem(id);
        if (dto is null || !SearchResultFactory.IsDynamicItem(dto)) return null;
        var imdb = dto.ProviderIds?.GetValueOrDefault("Imdb");
        if (imdb is null || !Regex.IsMatch(imdb, @"^tt\d{5,12}$")) return null;
        var path = dto.Type == BaseItemKind.Movie ? $"/catalogue/movies/{imdb}/movie.strm"
            : dto.Type == BaseItemKind.Series ? $"/catalogue/shows/{imdb}" : null;
        var type = dto.Type == BaseItemKind.Movie ? typeof(Movie) : typeof(Series);
        return path is not null && library.GetNewItemId(path, type) == id ? ScopeFromPath(path) : null;
    }

    private (User? User, bool ApiKey) Principal(ClaimsPrincipal principal)
    {
        if (principal.Identity?.IsAuthenticated != true) return (null, false);
        var apiKey = string.Equals(principal.FindFirst("Jellyfin-IsApiKey")?.Value, "true", StringComparison.OrdinalIgnoreCase);
        var claim = principal.FindFirst("Jellyfin-UserId")?.Value;
        var user = Guid.TryParse(claim, out var id) && id != Guid.Empty ? users.GetUserById(id) : null;
        return (user, apiKey);
    }

    private bool Permitted(Guid id, User? user, bool apiKey)
    {
        if (apiKey && user is null) return true; // Matches native LibraryController API-key handling.
        if (user is null) return false;
        var item = library.GetItemById(id);
        if (item is not null)
        {
            var visible = library.GetItemById<BaseItem>(id, user);
            return visible is not null && visible.CanDelete(user);
        }
        return user.HasPermission(PermissionKind.EnableContentDeletion) && user.HasPermission(PermissionKind.EnableAllFolders);
    }

    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        if (context.ActionDescriptor is not ControllerActionDescriptor action || action.ControllerName != "Library"
            || action.ActionName is not ("DeleteItem" or "DeleteItems")) { await next(); return; }
        var ids = context.ActionArguments.TryGetValue("itemId", out var single) && single is Guid itemId
            ? new[] { itemId }
            : context.ActionArguments.TryGetValue("ids", out var batch) && batch is Guid[] multiple ? multiple : Array.Empty<Guid>();
        var scopes = ids.Select(id => (Id: id, Scope: Resolve(id))).ToArray();
        if (!scopes.Any(s => s.Scope is not null)) { await next(); return; }
        // An all-online batch is one durable event. Never fall through with a
        // mixed batch, where native code could remove online files silently.
        if (scopes.Any(s => s.Scope is null) || scopes.Length > 100)
        { context.Result = new BadRequestObjectResult("Report online titles separately from local files"); return; }
        var (user, apiKey) = Principal(context.HttpContext.User);
        if (context.HttpContext.User.Identity?.IsAuthenticated != true || scopes.Any(s => !Permitted(s.Id, user, apiKey)))
        { context.Result = new UnauthorizedResult(); return; }
        try
        {
            reports.Request(scopes.Select(s => s.Scope!));
            logger.LogInformation("Online source recheck queued for {Count} selected items", scopes.Length);
            context.Result = new NoContentResult();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            logger.LogWarning("Online recheck could not be saved ({Reason})", error.GetType().Name);
            context.Result = new ObjectResult("Recheck could not be saved; no files were removed") { StatusCode = 503 };
        }
    }

    public async Task OnResultExecutionAsync(ResultExecutingContext context, ResultExecutionDelegate next)
    {
        if (context.Result is ObjectResult result)
        {
            if (result.Value is QueryResult<BaseItemDto> materialized) materialized.Items = materialized.Items.ToArray();
            else if (result.Value is IEnumerable<BaseItemDto> deferred) result.Value = deferred.ToArray();
            var entries = result.Value switch
            {
                QueryResult<BaseItemDto> page => page.Items,
                BaseItemDto item => new[] { item },
                IEnumerable<BaseItemDto> list => list,
                _ => Array.Empty<BaseItemDto>()
            };
            var (user, apiKey) = Principal(context.HttpContext.User);
            foreach (var item in entries)
                if (Resolve(item.Id, item) is not null) item.CanDelete = Permitted(item.Id, user, apiKey);
        }
        await next();
    }
}
