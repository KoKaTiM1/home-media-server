# Home media server — modified Dynamic Library plugin

This is the modified plugin used by **home-media-server**, currently
**1.4.0.15**, tested with **Jellyfin 10.11.11**. Its active configuration uses
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
