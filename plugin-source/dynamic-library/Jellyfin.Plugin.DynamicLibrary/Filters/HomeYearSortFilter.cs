using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Primitives;

namespace Jellyfin.Plugin.DynamicLibrary.Filters;

// Run before query model binding so sorting occurs in the database BEFORE paging.
// Episodes, seasons, search relevance, resume and next-up retain their own order.
public sealed class HomeYearSortFilter(ILibraryManager library) : IAsyncResourceFilter
{
    public async Task OnResourceExecutionAsync(ResourceExecutingContext context, ResourceExecutionDelegate next)
    {
        var request = context.HttpContext.Request;
        var query = request.Query;
        // Clients can retain collection IDs from before a supported library
        // rename. Translate only our two known IDs, before model binding.
        if (Guid.TryParse(query["parentId"], out var legacyParent))
        {
            var replacement = legacyParent.ToString("N") switch
            {
                "<LEGACY_MOVIES_LIBRARY_ID>" => "<ONLINE_MOVIES_LIBRARY_ID>",
                "<LEGACY_SHOWS_LIBRARY_ID>" => "<ONLINE_SHOWS_LIBRARY_ID>",
                _ => null
            };
            if (replacement is not null && library.GetItemById(Guid.Parse(replacement)) is CollectionFolder current
                && current.PhysicalLocations.Any(p => p is "/catalogue/movies" or "/catalogue/shows"))
            {
                var rewritten = query.ToDictionary(p => p.Key, p => p.Value, StringComparer.OrdinalIgnoreCase);
                rewritten["parentId"] = replacement;
                request.Query = new QueryCollection(rewritten);
                query = request.Query;
            }
        }
        if (HttpMethods.IsGet(request.Method)
            && context.ActionDescriptor is ControllerActionDescriptor action
            && (action.ActionName == "GetItems" || action.ActionName == "GetItemsByUserIdLegacy")
            && !query.ContainsKey("searchTerm") && !query.ContainsKey("ids"))
        {
            var types = query["includeItemTypes"].ToString().Split(',', StringSplitOptions.RemoveEmptyEntries);
            var titleTypes = types.Length > 0 && types.All(t =>
                t.Equals("Movie", StringComparison.OrdinalIgnoreCase) || t.Equals("Series", StringComparison.OrdinalIgnoreCase));
            var titleLibrary = false;
            if (Guid.TryParse(query["parentId"], out var parent))
            {
                var item = library.GetItemById(parent);
                if (item is CollectionFolder folder)
                {
                    var kind = folder.CollectionType?.ToString();
                    titleLibrary = string.Equals(kind, "movies", StringComparison.OrdinalIgnoreCase)
                        || string.Equals(kind, "tvshows", StringComparison.OrdinalIgnoreCase);
                }
            }
            if (titleTypes || titleLibrary)
            {
                var requested = query["SortBy"].ToString().Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                var requestedOrders = query["SortOrder"].ToString().Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                var secondaryIndex = Array.FindIndex(requested, s => !s.Equals("ProductionYear", StringComparison.OrdinalIgnoreCase));
                var secondary = secondaryIndex >= 0 ? requested[secondaryIndex] : "votes";
                var direction = requestedOrders.Length > 0 ? requestedOrders[Math.Min(Math.Max(0, secondaryIndex), requestedOrders.Length - 1)]
                    : secondary is "SortName" or "Runtime" or "OfficialRating" ? "Ascending" : "Descending";
                context.HttpContext.Items["HomeSecondarySort"] = secondary;
                context.HttpContext.Items["HomeSecondaryOrder"] = direction;
                var values = query.ToDictionary(p => p.Key, p => p.Value, StringComparer.OrdinalIgnoreCase);
                values["SortBy"] = new StringValues("ProductionYear," + (secondary == "votes" ? "SortName" : secondary));
                values["SortOrder"] = new StringValues("Descending," + direction);
                request.Query = new QueryCollection(values);
            }
        }
        await next();
    }
}
