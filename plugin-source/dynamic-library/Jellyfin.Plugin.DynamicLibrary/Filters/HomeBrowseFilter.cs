using System.Net.Http.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Globalization;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.DynamicLibrary.Providers;
using Jellyfin.Plugin.DynamicLibrary.Services;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.DynamicLibrary.Filters;

// Online library browsing is remote and paged. Only opened titles are persisted;
// loading a page never creates every episode of every show on that page.
public sealed class HomeBrowseFilter(ILibraryManager library, SearchResultFactory factory,
    IHttpClientFactory clients, IUserManager users, IUserDataManager userData,
    ILogger<HomeBrowseFilter> logger) : IAsyncActionFilter
{
    private sealed class Preview
    {
        [JsonPropertyName("id")] public string Id { get; set; } = "";
        [JsonPropertyName("type")] public string? Kind { get; set; }
        [JsonPropertyName("name")] public string Name { get; set; } = "";
        [JsonPropertyName("originalName")] public string? OriginalName { get; set; }
        [JsonPropertyName("poster")] public string? Poster { get; set; }
        [JsonPropertyName("background")] public string? Background { get; set; }
        [JsonPropertyName("description")] public string? Description { get; set; }
        [JsonPropertyName("releaseInfo")] public string? ReleaseInfo { get; set; }
        [JsonPropertyName("released")] public DateTime? Released { get; set; }
        [JsonPropertyName("genres")] public List<string>? Genres { get; set; }
        [JsonPropertyName("imdbRating")] public string? Rating { get; set; }
    }

    private sealed class Page
    {
        [JsonPropertyName("metas")] public List<Preview> Metas { get; set; } = new();
        [JsonPropertyName("total")] public int Total { get; set; }
    }

    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        var request = context.HttpContext.Request;
        if (context.ActionDescriptor is not ControllerActionDescriptor action)
        {
            await next(); return;
        }
        var search = request.Query["searchTerm"].ToString();
        var monthSearch = Regex.IsMatch(search, @"^\s*(19|20)\d{2}-\d{2}(?:\s*\.\.\s*(19|20)\d{2}-\d{2})?\s*$");
        var yearSearch = Regex.IsMatch(search, @"^\s*(19|20)\d{2}(?:\s*[-–—]\s*(19|20)\d{2})?\s*$");
        var folder = Guid.TryParse(request.Query["parentId"], out var parentId) ? library.GetItemById(parentId) as CollectionFolder : null;
        var kind = folder?.PhysicalLocations.Contains("/catalogue/movies") == true ? "movie"
            : folder?.PhysicalLocations.Contains("/catalogue/shows") == true ? "series" : null;
        if (kind is null && folder is null && (yearSearch || monthSearch) && !request.Query.ContainsKey("parentId"))
        {
            var included = request.Query["includeItemTypes"].ToString();
            kind = included.Equals("Movie", StringComparison.OrdinalIgnoreCase) ? "movie"
                : included.Equals("Series", StringComparison.OrdinalIgnoreCase) ? "series" : "all";
        }
        if (kind is null) { await next(); return; }
        if (action.ActionName.Contains("QueryFilters", StringComparison.Ordinal))
        {
            context.Result = new OkObjectResult(new
            {
                Years = Enumerable.Range(1920, DateTime.UtcNow.Year - 1919).Reverse().ToArray(),
                Genres = Array.Empty<string>(), Tags = Array.Empty<string>()
            });
            return;
        }
        if (action.ActionName is not ("GetItems" or "GetItemsByUserIdLegacy")
            || request.Query.ContainsKey("ids") || request.Query.ContainsKey("isFavorite")
            || request.Query["filters"].ToString().Contains("Favorite", StringComparison.OrdinalIgnoreCase)
            || request.Query.ContainsKey("genres") || request.Query.ContainsKey("genreIds"))
        {
            await next(); return;
        }
        if (search.Length > 0 && !yearSearch && !monthSearch)
        {
            await next(); return;
        }
        var year = search.Length > 0 && !monthSearch ? search : request.Query["years"].ToString();
        var month = monthSearch ? search : request.Query["month"].ToString();
        var selectedSort = context.HttpContext.Items["HomeSecondarySort"]?.ToString()
            ?? request.Query["SortBy"].ToString().Split(',').FirstOrDefault(s => s.Length > 0 && !s.Equals("ProductionYear", StringComparison.OrdinalIgnoreCase)) ?? "PremiereDate";
        var selectedOrder = context.HttpContext.Items["HomeSecondaryOrder"]?.ToString()
            ?? request.Query["SortOrder"].ToString().Split(',').FirstOrDefault(s => s.Length > 0) ?? "Descending";
        var start = int.TryParse(request.Query["startIndex"], out var offset) ? Math.Max(0, offset) : 0;
        var limit = int.TryParse(request.Query["limit"], out var size) ? Math.Clamp(size, 1, 1000) : 100;
        try
        {
            var client = clients.CreateClient();
            client.Timeout = TimeSpan.FromSeconds(25);
            var url = $"http://catalogue-bridge:8092/browse/{kind}?offset={start}&limit={limit}";
            if (year.Length > 0) url += "&year=" + Uri.EscapeDataString(year);
            if (month.Length > 0) url += "&month=" + Uri.EscapeDataString(month);
            url += "&sort=" + Uri.EscapeDataString(selectedSort) + "&order=" + Uri.EscapeDataString(selectedOrder);
            var page = await client.GetFromJsonAsync<Page>(url, context.HttpContext.RequestAborted);
            if (page is null) throw new InvalidOperationException("Missing browse page");
            var result = new List<BaseItemDto>();
            var userId = request.Query["userId"].ToString();
            if (context.ActionArguments.TryGetValue("userId", out var argument) && argument is Guid argumentId && argumentId != Guid.Empty)
                userId = argumentId.ToString();
            if (string.IsNullOrEmpty(userId))
                userId = context.HttpContext.User.Claims.FirstOrDefault(c => c.Type is "UserId" or "Jellyfin-UserId")?.Value ?? "";
            var user = Guid.TryParse(userId, out var parsedUser) && parsedUser != Guid.Empty ? users.GetUserById(parsedUser) : null;
            foreach (var preview in page.Metas)
            {
                int? productionYear = int.TryParse(preview.ReleaseInfo?.Split('-')[0], out var value) ? value : preview.Released?.Year;
                var dto = await factory.CreateDtoFromCatalogItemAsync(new CatalogItem
                {
                    Id = preview.Id, ImdbId = preview.Id, Source = CatalogSource.Stremio,
                    Type = (preview.Kind ?? kind) == "movie" ? CatalogContentType.Movie : CatalogContentType.Series,
                    Name = preview.Name, OriginalName = preview.OriginalName,
                    PosterUrl = preview.Poster, BackdropUrl = preview.Background,
                    Overview = preview.Description, Year = productionYear,
                    Rating = double.TryParse(preview.Rating, NumberStyles.Float, CultureInfo.InvariantCulture, out var score) ? score : null,
                    ReleaseDate = preview.Released, Genres = preview.Genres ?? new()
                }, context.HttpContext.RequestAborted);
                var native = library.GetItemById(dto.Id);
                if (user is not null && native is not null)
                {
                    // Native series DTO enrichment supplies the per-user unwatched
                    // badge and recursive counts, just as Recently Added does.
                    var data = userData.GetUserDataDto(native, dto, user,
                        new DtoOptions(false)
                        {
                            EnableImages = false,
                            Fields = new[] { ItemFields.ChildCount, ItemFields.RecursiveItemCount }
                        });
                    if (data is not null) dto.UserData = data;
                }
                result.Add(dto);
            }
            context.Result = new OkObjectResult(new QueryResult<BaseItemDto>
            {
                Items = result.ToArray(), TotalRecordCount = page.Total, StartIndex = start
            });
            return;
        }
        catch (Exception error)
        {
            // A provider outage must not make the existing indexed library disappear.
            logger.LogWarning(error, "Online browse unavailable; using indexed catalogue");
        }
        await next();
    }
}
