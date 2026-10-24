using System.Text;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.DynamicLibrary.Api;

// Native clients resolve DeliveryUrl against Jellyfin's base address. Proxy
// only signed catalogue subtitles; the bridge verifies the item HMAC.
[ApiController]
[AllowAnonymous]
[Route("HomeCatalogue/Subtitles")]
public sealed class HomeCatalogueSubtitleController(IHttpClientFactory clients) : ControllerBase
{
    [HttpGet("{key}/{language}.{format}")]
    public async Task<IActionResult> Get(string key, string language, string format, [FromQuery] string sig)
    {
        if (!Regex.IsMatch(key, @"^(movie:tt\d{5,12}|series:tt\d{5,12}:\d{1,3}:\d{1,4})$")
            || !Regex.IsMatch(sig, "^[a-f0-9]{64}$")
            || language is not ("heb" or "eng") || format is not ("srt" or "vtt")) return BadRequest();
        var client = clients.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(10);
        var url = $"http://catalogue-bridge:8092/sub/{Uri.EscapeDataString(key)}?sig={sig}&lang={language}";
        using var response = await client.GetAsync(url, HttpContext.RequestAborted);
        if (!response.IsSuccessStatusCode) return StatusCode((int)response.StatusCode);
        var bytes = await response.Content.ReadAsByteArrayAsync(HttpContext.RequestAborted);
        if (format == "srt") return File(bytes, "application/x-subrip; charset=utf-8");
        var text = Encoding.UTF8.GetString(bytes).TrimStart('\uFEFF').Replace("\r\n", "\n");
        text = Regex.Replace(text, @"(\d{2}:\d{2}:\d{2}),(\d{3})", "$1.$2");
        return Content("WEBVTT\n\n" + text, "text/vtt; charset=utf-8", Encoding.UTF8);
    }
}
