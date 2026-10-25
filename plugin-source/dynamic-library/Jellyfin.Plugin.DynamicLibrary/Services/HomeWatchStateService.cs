using Jellyfin.Data.Enums;
using MediaBrowser.Model.Dto;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

// Response-only correction: indexed episodes can be a subset of the known
// released show. Never persist inferred progress or mark missing episodes read.
public static class HomeWatchStateService
{
    public static void Apply(BaseItemDto dto, int releasedEpisodes)
    {
        if (dto.Type != BaseItemKind.Series || releasedEpisodes <= 0 || dto.UserData is null) return;
        var nativeTotal = dto.RecursiveItemCount;
        var unplayed = dto.UserData.UnplayedItemCount;
        if (nativeTotal is null || unplayed is null)
        {
            // Without a native denominator we cannot prove whole-show completion.
            dto.UserData.Played = false;
            dto.UserData.UnplayedItemCount = Math.Max(unplayed ?? 0, releasedEpisodes);
            dto.RecursiveItemCount = Math.Max(nativeTotal ?? 0, releasedEpisodes);
            return;
        }
        var total = Math.Max(nativeTotal.Value, releasedEpisodes);
        var watched = Math.Clamp(nativeTotal.Value - unplayed.Value, 0, nativeTotal.Value);
        dto.RecursiveItemCount = total;
        dto.UserData.UnplayedItemCount = total - watched;
        dto.UserData.Played = total > 0 && watched == total;
        dto.UserData.PlayedPercentage = total > 0 ? 100.0 * watched / total : 0;
    }
}
