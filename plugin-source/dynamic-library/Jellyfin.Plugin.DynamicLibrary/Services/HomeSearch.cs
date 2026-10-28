using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using MediaBrowser.Model.Dto;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

public static class HomeSearch
{
    public const string PageKey = "HomeMediaSearchPage";
    public sealed record Page(int Start, int Limit, int Fetch);
    public static Page Plan(int start, int limit)
    {
        start = Math.Max(0, start); limit = Math.Max(1, limit);
        return new(start, limit, (int)Math.Max(5000L, Math.Min(int.MaxValue, (long)start + limit)));
    }
    private static string[] Words(string? value)
    {
        var text = new string((value ?? "").Normalize(NormalizationForm.FormD)
            .Where(c => CharUnicodeInfo.GetUnicodeCategory(c) != UnicodeCategory.NonSpacingMark).ToArray());
        return Regex.Matches(text.ToLowerInvariant(), @"[\p{L}\p{N}]+").Select(m => m.Value).ToArray();
    }
    public static int Score(string? name, int? year, string query)
    {
        var terms = Words(query); var wanted = terms.Where(t => !Regex.IsMatch(t, @"^(19|20)\d{2}$")).ToArray();
        var title = Words(name); var phrase = string.Join(' ', title); var match = string.Join(' ', wanted);
        var count = wanted.Count(w => title.Any(t => t.StartsWith(w, StringComparison.Ordinal)));
        return (match.Length > 0 && phrase == match ? 1000
            : match.Length > 0 && phrase.StartsWith(match, StringComparison.Ordinal) ? 600
            : wanted.Length > 0 && count == wanted.Length ? 300 : 0)
            + count * 10 + (year is not null && terms.Contains(year.Value.ToString(CultureInfo.InvariantCulture)) ? 100 : 0);
    }
    private static string Identity(BaseItemDto item)
    {
        foreach (var provider in new[] { "Imdb", "Tmdb", "Tvdb" })
            if (item.ProviderIds?.TryGetValue(provider, out var id) == true && !string.IsNullOrEmpty(id))
                return $"{item.Type}:{provider}:{id.ToLowerInvariant()}";
        return $"{item.Type}:Guid:{item.Id}";
    }
    public static List<BaseItemDto> Merge(IEnumerable<BaseItemDto> native, IEnumerable<BaseItemDto> catalogue, string query)
    {
        var items = new List<BaseItemDto>(); var identities = new Dictionary<string, BaseItemDto>();
        var ids = new Dictionary<Guid, BaseItemDto>(); var scores = new Dictionary<BaseItemDto, int>();
        foreach (var item in native.Concat(catalogue))
        {
            var identity = Identity(item);
            var score = Math.Max(Score(item.Name, item.ProductionYear, query), Score(item.OriginalTitle, item.ProductionYear, query));
            if (identities.TryGetValue(identity, out var existing) || (item.Id != Guid.Empty && ids.TryGetValue(item.Id, out existing)))
            {
                // Catalogue year is canonical for the same identity. This is a
                // response DTO only; preserve native ID, watched state/favorite.
                if (item.ProductionYear is not null) existing.ProductionYear = item.ProductionYear;
                scores[existing] = Math.Max(scores[existing], score);
                continue;
            }
            identities[identity] = item;
            if (item.Id != Guid.Empty) ids[item.Id] = item;
            scores[item] = score; items.Add(item);
        }
        return items.OrderByDescending(i => scores[i]).ThenByDescending(i => i.ProductionYear)
            .ThenBy(i => i.Name, StringComparer.OrdinalIgnoreCase).ThenBy(i => i.Id).ToList();
    }
}
