# Home media server — modified Dynamic Library plugin

This is the modified plugin used by **home-media-server**, currently
**1.4.0.19**, tested with **Jellyfin 10.11.11**. Its active configuration uses
the project's local catalogue bridge. The generic upstream feature list does
not describe which providers or services this deployment uses.

## Features provided by this fork

- Shared-startup request timings, bounded reusable artwork caching and a
  30-second navigation lease that lets heavy background reads yield.
- Ready-only online movie/show browsing, paging, Hebrew alias/franchise search,
  release-year/range/month queries and within-year secondary sorting.
- Stable posters with Hebrew/English subtitle markers, native per-user show
  counts and locally saved preview episode-count fallbacks.
- Unavailable favorites retained while normal watch lists stay ready-only.
- Whole-show display progress includes known released episodes awaiting indexing;
  native movie/episode watch history is preserved. Watched indicators are enabled
  in the TV app locally. Details-page bridge preparation warms one idle title.
- Verified bridge torrent sources and original-page access to completed saves.
- AAC-LC direct playback or native audio-only HLS conversion with video copy,
  seeking and selectable external/embedded text subtitles.
- Bounded bridge preparation requests and explicit failure contracts for TV
  clients, instead of successful responses with no compatible media source.
- Native local-file playback and canonical catalogue item persistence without
  overwriting watched/favorite state.

The complete current behavior and limits are in the public repository's
[catalogue guide](../../deploy/media-stack/catalogue/README.md). Background
workers, storage, OMV extensions and retention belong to the surrounding
project; the plugin alone does not install that stack.

## Build and restore

Follow the public root [RESTORE.md](../../RESTORE.md). Configure private
network/storage/library values before building the .NET 9 source. Keep API
keys in runtime secret files, outside the public checkout. Compiled DLLs are
excluded from GitHub because they can embed deployment-specific values.
Install the locally built DLL and matching metadata only while Jellyfin is
stopped and viewing is idle. Preserve its MIT license.

Installing an upstream release from the generic plugin repository replaces
these modifications. Test Jellyfin/plugin upgrades against the full stack
before deploying them. Do not use the upstream quick-start guide as the
recovery sequence for this configured server.

## Upstream reference

[UPSTREAM_README.md](UPSTREAM_README.md) preserves the original author's generic
setup/features guide. Optional TVDB/TMDB, Embedarr and upstream OpenSubtitles
settings described there are not a declaration that this server runs them.
The original [MIT license](LICENSE) is retained unchanged.

Hebrew subtitle choices are labelled עברית 1 / עברית 2. Bridge variant IDs must
remain in the native subtitle relay query: both tracks can share a language but
must retain distinct URLs/files/indexes. HomeSubtitleTracks labels both direct
and audio-converted playback without changing language IDs or explicit None.

Online Delete reports a title for fresh verification rather than removing its
virtual record or saved media. Reports are durable, prioritized and visible in
OMV worker counts; successful fresh checks return titles to ordinary browsing.
Local media deletion retains its native behavior. TV media-management settings
and existing user permissions determine whether Delete is visible.

Verification v5 uses signed exact-source playback URLs from the bridge. The filter validates their origin, title path and binding fields before native direct/HLS playback. Install the bridge and plugin together; see the catalogue operating guide for progress-aware source verification and resumable audits.
