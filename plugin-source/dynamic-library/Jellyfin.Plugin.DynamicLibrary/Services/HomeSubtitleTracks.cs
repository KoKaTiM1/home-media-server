using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;

namespace Jellyfin.Plugin.DynamicLibrary.Services;

public static class HomeSubtitleTracks
{
    public static string RelayUrl(Uri source, string language)
    {
        var key = Uri.UnescapeDataString(source.AbsolutePath[5..]);
        return $"/HomeCatalogue/Subtitles/{Uri.EscapeDataString(key)}/{language}.vtt{source.Query}";
    }

    public static void Label(MediaSourceInfo source)
    {
        var number = 0;
        foreach (var track in source.MediaStreams.Where(s => s.Type == MediaStreamType.Subtitle
            && (string.Equals(s.Language, "heb", StringComparison.OrdinalIgnoreCase)
                || string.Equals(s.Language, "he", StringComparison.OrdinalIgnoreCase)
                || string.Equals(s.Language, "iw", StringComparison.OrdinalIgnoreCase))))
        {
            track.Title = $"עברית {++number}";
            track.LocalizedDefault = "ברירת מחדל";
            track.LocalizedForced = "מאולץ";
            track.LocalizedExternal = "חיצוני";
            track.LocalizedHearingImpaired = "כבדי שמיעה";
        }
    }
}
