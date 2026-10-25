using System.Diagnostics;
using Jellyfin.Plugin.DynamicLibrary.Services;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.DynamicLibrary.Filters;

// Cover action processing, result enrichment and serialization. Query strings,
// action arguments, user identities and authorization are never logged.
public sealed class RequestLoggerFilter(ILogger<RequestLoggerFilter> logger, HomeForegroundService foreground)
    : IAsyncActionFilter, IAsyncAlwaysRunResultFilter, IOrderedFilter
{
    public int Order => -100;
    private const string Started = "HomeTimingStarted";
    private static readonly HashSet<string> Controllers = new(StringComparer.OrdinalIgnoreCase)
        { "Items", "UserLibrary", "TvShows", "UserViews", "Image", "Images", "Library" };

    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        context.HttpContext.Items[Started] = Stopwatch.GetTimestamp();
        if (context.ActionDescriptor is ControllerActionDescriptor action)
            foreground.Touch(context.HttpContext.Request, action.ControllerName, action.ActionName);
        await next();
    }

    public async Task OnResultExecutionAsync(ResultExecutingContext context, ResultExecutionDelegate next)
    {
        try { await next(); }
        finally
        {
            if (context.ActionDescriptor is ControllerActionDescriptor action && Controllers.Contains(action.ControllerName)
                && context.HttpContext.Items[Started] is long started)
            {
                var elapsed = Stopwatch.GetElapsedTime(started).TotalMilliseconds;
                var image = action.ControllerName is "Image" or "Images";
                if (!image || elapsed >= 250)
                    logger.LogInformation("[HomeTiming] {Controller}/{Action} {Milliseconds}ms status={Status}",
                        action.ControllerName, action.ActionName, (int)elapsed, context.HttpContext.Response.StatusCode);
            }
        }
    }
}
