using Jellyfin.Data.Enums;
using Jellyfin.Plugin.DynamicLibrary.Extensions;
using Jellyfin.Plugin.DynamicLibrary.Services;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Querying;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.DynamicLibrary.Filters;

public class SearchActionFilter : IAsyncActionFilter, IOrderedFilter
{
    private readonly DynamicLibraryService _dynamicLibraryService;
    private readonly ILibraryManager _libraryManager;
    private readonly ILogger<SearchActionFilter> _logger;
    private readonly HomeCatalogueResultFilter _homeResults;

    public SearchActionFilter(
        DynamicLibraryService dynamicLibraryService,
        ILibraryManager libraryManager,
        ILogger<SearchActionFilter> logger,
        HomeCatalogueResultFilter homeResults)
    {
        _dynamicLibraryService = dynamicLibraryService;
        _libraryManager = libraryManager;
        _logger = logger;
        _homeResults = homeResults;
    }

    public int Order => 1;

    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        // Check if plugin is enabled
        if (!_dynamicLibraryService.IsEnabled)
        {
            await next();
            return;
        }

        // Check if this is a search action
        if (!context.IsApiSearchAction())
        {
            await next();
            return;
        }

        // Check for searchTerm
        if (!context.TryGetActionArgument<string>("searchTerm", out var searchTerm)
            || string.IsNullOrWhiteSpace(searchTerm))
        {
            await next();
            return;
        }

        // Get requested item types
        var requestedTypes = context.GetRequestedItemTypes();
        if (requestedTypes.Count == 0)
        {
            await next();
            return;
        }

        // Get pagination parameters
        context.TryGetActionArgument("startIndex", out var startIndex, 0);
        context.TryGetActionArgument("limit", out var limit, 25);

        var page = HomeSearch.Plan(startIndex, limit);
        context.HttpContext.Items[HomeSearch.PageKey] = page;
        // Retrieve a common native prefix, then paginate the combined result
        // once. Very broad searches retain native paging without truncating it.
        if (context.ActionArguments.ContainsKey("startIndex")) context.ActionArguments["startIndex"] = 0;
        if (context.ActionArguments.ContainsKey("limit")) context.ActionArguments["limit"] = page.Fetch;
        if (context.ActionArguments.ContainsKey("recursive") && !context.HttpContext.Request.Query.ContainsKey("recursive"))
            context.ActionArguments["recursive"] = true;
        var resultContext = await next();
        if (resultContext.Result is not ObjectResult objectResult || objectResult.Value is not QueryResult<BaseItemDto> original) return;
        var native = await _homeResults.FilterSearchReadyAsync(original.Items,
            HomeCatalogueResultFilter.IsFavoritesQuery(context.HttpContext.Request.Query), context.HttpContext.RequestAborted);
        var results = native.ToList();
        var total = Math.Max(native.Count, original.TotalRecordCount - (original.Items.Count - native.Count));
        try
        {
            if (original.TotalRecordCount <= page.Fetch)
            {
                var dynamic = await _dynamicLibraryService.SearchAsync(searchTerm, requestedTypes, context.HttpContext.RequestAborted);
                results = HomeSearch.Merge(native, dynamic, searchTerm);
                total = results.Count;
            }
        }
        catch (Exception error)
        {
            // Retain the requested native page even when catalogue search fails.
            _logger.LogWarning("Home search merge deferred ({Category})", error.GetType().Name);
        }
        resultContext.Result = new OkObjectResult(new QueryResult<BaseItemDto>
        {
            Items = results.Skip(page.Start).Take(page.Limit).ToArray(),
            TotalRecordCount = total
        });
    }

    /// <summary>
    /// Check if an item with the given provider ID exists in Jellyfin's library.
    /// </summary>
    private bool ItemExistsInLibrary(string providerName, string providerId)
    {
        try
        {
            var query = new InternalItemsQuery
            {
                HasAnyProviderId = new Dictionary<string, string> { { providerName, providerId } },
                Recursive = true,
                Limit = 1 // We only need to know if at least one exists
            };

            var result = _libraryManager.GetItemsResult(query);
            return result.TotalRecordCount > 0;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Error checking library for {Provider}:{Id}", providerName, providerId);
            return false;
        }
    }
}
