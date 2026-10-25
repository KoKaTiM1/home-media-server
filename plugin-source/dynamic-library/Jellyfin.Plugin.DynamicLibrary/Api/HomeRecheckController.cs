using Jellyfin.Plugin.DynamicLibrary.Filters;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;

namespace Jellyfin.Plugin.DynamicLibrary.Api;

// Read-only diagnostics let deployment verify interception before exercising
// native DELETE. No unauthenticated mutation endpoint is introduced.
[ApiController]
[Authorize]
[Route("HomeCatalogue/Rechecks")]
public sealed class HomeRecheckController(HomeRecheckDeleteFilter filter, IOptions<MvcOptions> mvc) : ControllerBase
{
    [HttpGet("Capabilities")]
    public IActionResult Get([FromQuery] Guid? itemId = null) => Ok(new
    {
        Registered = mvc.Value.Filters.OfType<ServiceFilterAttribute>().Any(f => f.ServiceType == typeof(HomeRecheckDeleteFilter)),
        Mode = "online-delete-recheck",
        Version = typeof(HomeRecheckController).Assembly.GetName().Version?.ToString(),
        Scope = itemId.HasValue ? filter.ScopeFor(itemId.Value) : null
    });
}
