# Home media catalogue

Current deployed behavior, reviewed 2026-10-25 against Dynamic Library
**1.4.0.15**, Jellyfin **10.11.11**, the bridge and systemd units. This is the
current operating guide, rather than a chronological list of old features.
The installed WizdomSubs Downloader version is **0.2.0.0**.

## Browse, search and favorites

Open **סרטים צפייה ישירה** for online movies or **סדרות צפייה ישירה** for online
shows. Local **Movies** and **Shows** hold downloaded/retained media. The owner
has those four libraries in that order, followed by Collections and Downloads.
Library order belongs to each Jellyfin user.

The local IMDb index contains roughly a million discovery metadata entries.
It is not a million-title playable library. Normal online browsing/search shows
only saved copies or titles with successful video-source validation. A series
becomes eligible after a verified episode; its episode lists also hide unchecked
episodes. Unknown or failed titles remain in the durable validation/retry queue.
Searching an unknown title can queue it for a later check without displaying it
as immediately playable. Availability can change after a successful sample.

Favorites keep unavailable saved titles visible. Their hearts and watched state
survive source failure. Opening an unavailable favorite remains possible; Play
must prepare a working compatible source or return an explicit failure. This
exception applies to Favorites, not the normal online watch catalogue.

Movie/show browsing has no fixed total-title cap. The native TV app loads
100 entries per scrolling batch; the bridge accepts up to 1,000 entries in one
page request and supports further pages. Those request sizes do not determine
the total library size. Visible counts depend on successful validation and
saved copies, and can fall when a source fails. Only persisted/opened titles
create native Jellyfin items; browsing does not create every episode in the
metadata index.

| Search or sort | Behavior |
| --- | --- |
| `2025` | One release year |
| `1994-2000` | Inclusive year range |
| `2026-07` | One month, where a real release date is known |
| `2026-07..2026-09` | Inclusive month range, where dates are known |
| Default / Newest / Date added | Production year descending, then actual release date descending for online catalogue titles |
| Score, name or runtime | Selected secondary sort within each production year |
| Hebrew title or franchise keyword | Cached Hebrew aliases and related English matches; coverage varies |

A show's year is its first release year. The native Android TV app used for
verification has no added year/month picker: use its existing search controls,
which retain client result limits. Unknown dates are not invented and do not
match month searches. Local libraries retain Jellyfin's native Date added
behavior. Bulk IMDb metadata does not supply critic scores or age certificates.

Hebrew names use Wikidata labels and IMDb identity, with franchise expansion
such as Hebrew Spider-Man variants. Known names display as Hebrew / English;
others keep their original name. Local English full-text search can work when
the remote search provider is unavailable. No translation is invented.

## Posters, episode counts and trailers

Poster badges show **HE in green**, **EN in red**, or **? in gray** for confirmed
Hebrew, confirmed English, or unknown subtitle availability. Show markers sit
bottom-left; movie markers sit top-left. A show's subtitle badge does not promise
subtitles for every episode or exact timing for every release.

Indexed shows use native per-user unwatched episode counts, including watched
and favorite state. Previews without native indexing use locally saved distinct
released-episode metadata, then switch to native counts when indexed. Preview
counts include released specials and exclude future, duplicate and invalid
entries. Unknown counts stay unknown. Browsing does not fetch provider episode
metadata merely to populate the badge.

Browse, details, search and Back navigation preserve the same poster identity.
Missing artwork gets a generated title/badge image; it is not proof of a missing
torrent. Concurrent artwork writes use atomic replacement. Poster style/badge
changes invalidate the client image identity.

Where metadata supplies a YouTube trailer, the native Trailer / Play trailers
control can open YouTube on TV; the control may be under More. Trailers are
links, not videos hosted by this server. Embedded YouTube playback within the
native TV detail page is not implemented. The TV player's existing aspect-ratio
controls provide Fit, Auto crop and Stretch; those are client controls.

## Playback and audio

Sources favor H.264 1080p/720p and healthier reported swarms. Explicit zero-seed
listings, 4K, HEVC/AV1, camera releases and archive/ISO payloads are excluded.
Actual video/audio are probed. Episodes must match season/episode or an explicit
unambiguous file index; movie packs require an explicit index. Selected video
must be compatible with the client: this catalogue does not encode or scale it.
One distinct video stream is supported at a time on the current PC.

AAC-LC audio can play directly. Selected AAC Main, AC3/EAC3, MP3 or other non-LC
tracks can use Jellyfin native HLS with **video copy and AAC-LC stereo audio at
192 kbps / 48 kHz**. The bridge verifies a small converted sample before making
a conversion-dependent source eligible. Unknown VOD duration or failed metadata,
data, conversion or decode checks leave the source unavailable for retry.
AAC Main is not categorically excluded and is not passed directly to the TV by
this catalogue conversion path. Conversion works for torrent streams, saved
copies on the original title page and native local files. Full-film download
is not required before torrent playback starts; original files stay unchanged.

Native Jellyfin HLS owns seeking, segments, authentication and cleanup. Text
subtitles remain separately selectable; video encoding/subtitle burning is not
requested. After restore, `configure-audio.py` applies native throttling at
60 seconds ahead and segment retention of 90 seconds through Jellyfin's API.
Temporary HLS files use the existing media-partition Jellyfin cache.

| Server request stage | Bound / failure behavior |
| --- | --- |
| Foreground preparation | Shared 27-second budget for lock waits, provider calls, resolution, probing, conversion checks and subtitle wait |
| Plugin request for preparation | 30-second HTTP timeout; failure returns `NoCompatibleStream`, never a successful empty source list |
| Initial stream resolution and first video byte | Separate shared 15-second budget; do not send successful video headers before receiving data |
| Background validation preparation | 60-second preparation budget per probe |

These are per-request limits, not a promise about total TV app time if it retries.
Alternate compatible sources are tried within the preparation budget; the server
does not change sources after delivery starts. Busy preparation and disconnected
clients are not negative source proof. A real source failure clears its cached
preparation and hides it from normal browse while retaining it for retry.

## Subtitles

Wizdom is the default free Hebrew provider and needs no account/key. Movie IMDb
IDs or series IMDb plus exact season/episode identify matches; release names
help rank candidates. Hebrew is preferred, with English where available.
OpenSubtitles is optional additional coverage, configured privately through
`configure-subtitles.py`. Missing subtitles cannot be fabricated, and matching
a release name does not guarantee synchronization.

The bridge reads ZIP contents without extracting archive paths and converts
Windows-1255/UTF-16 content to UTF-8. The signed Jellyfin-relative subtitle relay
serves Hebrew/English text as WebVTT. External and embedded text tracks remain
selectable, including explicit None across PlaybackInfo refreshes. A player
already open during an update must be stopped/reopened to use changed metadata.

Hourly badge refreshes are bounded to 25 titles and a 200-search/day application
budget, prioritizing recently browsed titles. Badges use cached availability;
preferred subtitle files are downloaded on demand, not for the entire index.

## Save files and retention

Use the native source/version selector on a movie or episode page:

- **Stream now**: stream without requesting a full download.
- **Download to server + play**: queue the selected movie/episode in Transmission
  and begin playback. Stopping the player leaves the requested download running.
- **Downloaded on server**: a completed owned file takes priority on the same
  original title page. After retention removes it, Play can use torrents again.

This is a source option, not a custom TV download button. Shows download one
selected episode at a time. The watcher selects only the requested file from a
pack, caps queued requests at three and reserves at least 20 GiB free. Existing
torrents outside this feature are rejected rather than modified.

All persistent state, media, staging and streaming caches use the existing EXT4
media partition, `/dev/sda5` on the source machine. Device names may differ after
recovery; verify the filesystem. Paths below are relative to its OMV mount:

| Directory | Purpose |
| --- | --- |
| `media/movies/Saved/IMDB/movie.EXT` | Completed owned movies |
| `media/shows/Saved/IMDB/Season 01/S01E02.EXT` | Completed owned episodes |
| `downloads/complete/Saved/TORRENT_HASH/` | Transmission staging |
| `appdata/catalogue/catalogue.sqlite` | Queue, proof, aliases, metadata/cache and download manifests |
| `appdata/catalogue/imdb/catalogue.sqlite` | Rebuildable bulk metadata index |

The bridge sees the whole filesystem at `/storage` so final moves can use an
atomic rename. Jellyfin reads media at `/media` and completed Downloads at
`/downloads`. Preserve that common bridge bind when restoring.

Only manifest-owned Saved files qualify for automatic removal: after a new
watched/completed signal, or 30 days after completion if unwatched. Historical
Played flags do not immediately delete a new save. Active playback and changed
size/inode/modification identities are protected. A durable move intent recovers
interrupted final moves. Manually retained media is excluded.

The separate legacy Downloads cleaner uses a 24-hour watched grace period or
30 days after observed completion if unwatched. Watching in an external player
does not supply Jellyfin watched status. Retention deletes video data; restore
ownership/cleanup state before enabling it on a recovered server.

## Workers, retries and restart

| Timer | Schedule / purpose |
| --- | --- |
| `media-catalogue-sync` | 00:00 / 06:00 / 12:00 / 18:00 Asia/Jerusalem, up to two minutes jitter; discover and refresh metadata |
| `media-source-availability` | Five minutes after boot, then one minute after each batch finishes; source/audio/video validation |
| `media-saved-downloads` | Every minute; downloads, final moves and Saved retention |
| `media-catalogue-badges` | Hourly; subtitle availability markers |
| `media-catalogue-collections` | Every 30 minutes; native collection membership |
| `media-cleanup` | Hourly; legacy Downloads retention |
| `media-worker-status` | Two minutes after boot, then one minute after completion; cached dashboard counters |

Bulk IMDb refresh is approximately daily, not four full downloads per day;
index replacement is atomic. Four daily syncs update recent metadata, release
dates and indexed/opened shows without resetting existing validation results.

Validation runs serially, starting at most 20 probes per batch or running for
up to eight minutes before starting another probe. It reads bounded video samples
and decodes two seconds rather than saving full films. It pauses between probes
during viewing and defers when session information is unavailable. Queue slots
cover new movies/first show episodes, extra episodes, due failures and older
audio-proof audits. A passed episode count is not a count of distinct shows.

Failures retry after 6 hours, 12 hours, 1 day, 2 days and increasing delays capped
at 30 days. Prior successful proof remains saved for diagnosis; newer failed
evidence determines current readiness. Provider HTTP429 triggers 15 minutes
cooldown; provider server errors trigger five minutes. Audio-policy v2 resync
rechecks older proofs without resetting metadata, attempts or future backoff.

Collections maintenance reads native titles/members in pages of 100, requests
totals once and uses a 60-second maintenance timeout. It traverses empty filtered
pages and titles beyond 2,000 while preserving existing memberships.

OMV **Diagnostics > Media workers** shows all seven jobs, schedules, health,
current work, pending/failed/due counts, distinct passed titles, metadata outside
the queue and resync progress. Heavy counters are prepared outside web requests.

Mount-aware Docker/startup units, restart policies and timers start without an
interactive login. A full reboot on 2026-10-25 verified source-server startup;
repeat storage/network/client checks on replacement hardware. GitHub contains
software and recovery templates, not private runtime databases or media. No
automated encrypted off-server runtime backup is configured.

## Shared initial loading

Home and online-library pages share startup work. Browsing now uses primary-key
lookups of the verified set and a compact persistent `home_titles` metadata cache
on the existing media partition, instead of scanning the bulk discovery index.
The cache survives service restarts, refreshes when the bulk index changes, and
is warmed by discovery sync. Compact `home_ready` and `home_source_status`
projections are maintained atomically by SQLite triggers across worker processes.
New failures affect readiness immediately; these tables do not replace proof,
retry or watched history. User-specific DTOs are not cached across accounts.

Unchanged discovery pages avoid writer transactions. Badge lookups read only
requested titles, and locally known episode-count calculations are reused while
the metadata and current day remain unchanged. Existing style/badge-matched
posters are served directly without database/provider work. Bridge and Jellyfin
poster responses use ETags and private 60-second caching; the plugin shares
concurrent fetches in a 16 MiB bounded artwork cache. Changed badge/style URLs
receive separate image identities.

Real client navigation writes a small shared 30-second lease, throttled to one
write per five seconds. Further qualifying metadata navigation can renew it;
images and maintenance API calls cannot. Source checks yield without recording
negative proof, and running preparation subprocesses can stop promptly. Heavy
counter queries yield and preserve the previous timestamped snapshot. Timers
keep their existing schedules and work resumes after the lease expires.

This gives foreground reads priority on the current hard drive; it does not
promise a fixed phone/TV rendering time or make every internet image/source
available. A fresh metadata-index generation can need cache rebuilding. Logs
separate native request time from bridge browse time and artwork delivery.

Measured on the source server on 2026-10-25 (not physical client screen time):

| Request / workload | Before | After |
| --- | ---: | ---: |
| First Movies page, 100 entries | 15.7 s | 3.5 s |
| First Shows page, 100 entries | 9.4 s | 3.5 s |
| Parallel home metadata requests | 44 s | 11 s |
| Repeated Movies page | 5.0 s | 0.17 s |
| Repeated Shows page | 2.2 s | 0.37 s |
| First batch of 16 poster images | 5.7 s | 1.0 s |

These observations include the machine's actual workload and caches; they are
not a hardware-independent benchmark or a guarantee for phone/TV rendering.
The after test includes the correct Continue Watching endpoint as well as the
home rows measured before. No Linux page-cache clearing was forced.

## Logs, verification and recovery

```sh
docker logs --since 15m catalogue-bridge
journalctl -u media-source-availability.service --no-pager -n 40
systemctl list-timers 'media-*'
systemctl --failed
```

`[HomeTiming]` entries in Jellyfin logs record controller/action, elapsed
milliseconds and response status for metadata requests and slower image requests.
Arguments, query strings, user IDs, credentials and URLs are not logged.

Structured `Playback` events record the title/episode key, phase, duration,
outcome and exception class for preparation, torrent resolution, video probing,
audio conversion and initial video data. Generic request failures are recorded
too. Exception text, signed URLs, magnets and credentials are excluded. These
logs narrow the failing stage; they cannot always prove a remote peer's cause.
Background preparation events also appear in the source worker's journal.

Run isolated unit tests from this folder:

```sh
python3 -m unittest test_app test_features test_playback_deadlines test_startup
```

`check_show_counts.py --all` is read-only and checks native/preview counts.
Playback/source/download checks such as `check_audio_conversion.py`,
`check_audio_subtitles.py`, `check_saved_download.py` and
`check_subtitle_delivery.py` are live integration tools: inspect their fixtures
and run only while viewing is idle. Earlier saved Big Buck Bunny downloads may
have been removed by normal retention; do not assume they still exist.

Deployment verification includes a zero-warning plugin build, Python feature
and playback-deadline/stream regression suites, .NET favorites/audio/indexing
checks, both native Favorites query variants, explicit failed-source responses,
local HTTP206 playback and unchanged existing watched/favorite records. Counts
are dated verification observations, not fixed library totals. The owner
confirmed Chromecast AAC Main-to-LC playback; each client/source still needs its
own test after a restore or update. No sample establishes every torrent's health.

Use the public root `RESTORE.md` for recovery. Keep media/ownership state together;
never format the existing filesystem or erase watched state to satisfy a check.
The modified plugin retains its upstream MIT license. Metadata uses IMDb's
non-commercial datasets, Cinemeta and Wikidata; posters/trailers come from
metadata providers, and trailer videos are not mirrored.
