# Home media catalogue

Online show cards reuse Jellyfin's native per-user episode counts and watched
data, so their unwatched badge matches Recently Added for indexed series.
Previews without indexed episode data do not invent an episode count.

Show posters place the green HE / red EN / gray unknown subtitle marker in
the bottom-left corner, leaving the top free for native episode-count badges.
Movie subtitle markers remain at the top-left. Poster style versions refresh
the client image identity when the placement changes.

## Native collections maintenance

The collections job reads native Jellyfin titles and existing collection
members in pages of 100. It requests total counts only on the first page,
with a 60-second maintenance request timeout. Availability filtering can
return an empty page before the end, so paging advances by the requested
size and uses a conservative native-slot bound. It does not stop at an empty
filtered page or at 2,000 titles. Existing collection members are preserved;
only missing memberships are added. Bucket matching uses a dictionary rather
than comparing every title against every metadata row.

Jellyfin 10.11.11 is pinned in /opt/media-stack/compose.yaml. The local Dynamic
Library extension is version 1.4.0.12; WizdomSubs Downloader is version 0.2.0.0.
Do not automatically update these plugins/Jellyfin without integration testing.

## Watching and browsing

Open סרטים צפייה ישירה or סדרות צפייה ישירה. The catalogue no longer has a 1,000/200
application limit. Browsing uses an on-disk IMDb index, roughly 775,000 movie and
346,000 series metadata entries at setup. Native TV loads 100 items per batch as
you scroll; requests can page through up to 1,000 entries at a time. The total
reported by the API respects year/release checks and known source failures.
Only opened titles are persisted into Jellyfin; browsing a million series does
not create every episode of every show. The original indexed libraries and
user favourites/watch state remain available if remote browsing fails.

Search for 2025 or 1994-2000 to select one release year or an inclusive range.
Years are filters, not dedicated folders. A series year means its first release.
Native Android TV 0.19.10 has no year-picker control, so use its existing search;
its usual search result limits still apply. The Items API supports fully paged
year/range queries; clients that expose Years can send a comma-separated list.
Production year descending stays primary. The selected name, IMDb community
score, runtime or popularity sort works within each year, before pagination.
Critic scores and age certificates are not provided by the IMDb dataset, so
sorting by those fields cannot distinguish titles without that metadata.

Hebrew queries use Wikidata's Hebrew labels and IMDb identities. Related English
franchise keywords expand the local search, including Hebrew Spider-Man variants. Known aliases
are stored locally and displayed as Hebrew / English; unidentified titles retain
their original names. Full-text local English/title search also works when the
Cinemeta search provider is unavailable. Tests include שובר שורות (Breaking Bad)
and מטריקס (The Matrix). Hebrew coverage and provider availability vary; the
system does not invent translations or promise every title has a Hebrew name.

Where metadata supplies a YouTube trailer, the native Trailer/Play trailers
button opens the TV's YouTube app. The button can be under More. Existing
metadata was backfilled for 1,185 titles; newly opened titles get trailers from
the provider where available. YouTube playback embedded inside the same native
TV page would require changes to that TV app.

The installed TV player already supports Fit, Auto crop, and Stretch through
its aspect-ratio control during playback. Fit shows the complete frame, Auto
crop fills the display by trimming edges, Stretch fills it by distorting the
aspect ratio. There is no server-side fine-grained zoom-out control. If Fit
still cuts off picture edges, check the TV's overscan/Screen Fit settings.

## Source availability

Metadata is not a guarantee of a working torrent. Titles with a confirmed empty
compatible-source listing or a failed playback preflight are hidden from normal
movie browsing/search while that status is fresh. Unknown titles remain
browsable so discovery can extend beyond prechecked releases. Series availability
is episode-specific; the system does not hide an entire series because one
specific episode failed. The background checker examines 50 listings per run,
prioritizing recently browsed titles, without downloading video. It runs every
15 minutes and defers on provider rate limiting. Empty listings expire after
six hours; failed preflights retry after a shorter interval.

Sources prefer H.264 1080p/720p and healthier reported swarms. Explicit zero-seed
releases, 4K, HEVC/AV1, camera releases and archive/ISO payloads are excluded.
An episode must match its season/episode or an explicit unambiguous file index;
movie packs require an explicit index. Actual codecs are probed. Preparation
has a 25-second budget with bounded metadata/source retries and subtitle lookup;
a stalled/dead source fails rather than waiting indefinitely. Free P2P sources
can still stall or fail. Only one distinct video stream is supported at once.
The server does not transcode video; clients must decode the selected release.

## Save on the server

On a movie or episode page, use the native source/version selector:
- Stream now: streams without requesting a full download.
- Download to server + play: explicitly queues the selected movie/episode in
  Transmission and starts playback. Stop the player if you only want to save it
  for later; the download continues. This is a native source option, not a new
  standalone custom TV button. A series is saved one selected episode at a time.
- Downloaded on server: a completed file takes priority automatically on the
  SAME original item page. After its eventual removal, Play uses torrents again.

The download watcher checks every minute. It selects only the requested file
from a season pack, reserves at least 20 GiB free space, and caps queued requests
at three. Existing torrents outside this feature are rejected rather than
modified. A durable manifest and process lock protect engine operations. A
persisted move intent recovers if a process/reboot interrupts the final move.

Storage is entirely on /dev/sda5, UUID
<MEDIA_FILESYSTEM_UUID>. Host root:
/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>
Completed movies: media/movies/Saved/IMDB/movie.EXT
Completed episodes: media/shows/Saved/IMDB/Season 01/S01E02.EXT
Transmission staging: downloads/complete/Saved/TORRENT_HASH/...

The bridge has one common bind mount of this filesystem at /storage so completed
files can be atomically renamed across its download/media directories. Jellyfin
still sees retained media read-only at /media and completed Downloads at /downloads.
Bridge state/secrets remain separate; new owned media files are tracked explicitly.

Managed Saved files are removed on a watcher pass AFTER a new watched/completed
signal, while never removing an actively playing item. Old historical Played
flags do not immediately delete a new download. Unwatched saved files expire
30 days after completion. Size/inode/modification checks preserve changed files.
Only manifest-owned files under Saved are eligible; existing manually retained
media is excluded. The older Downloads cleanup policy remains 24 hours after
watched / 30 days unwatched and is separate from the new Saved workflow.

## Subtitles

WizdomSubs is the default free Hebrew provider; no key/account is required.
The bridge matches movie IMDb IDs or series IMDb plus exact season/episode,
ranks releases against the selected filename, reads ZIPs without extracting
paths, and converts Windows-1255/UTF-16 subtitles to UTF-8. Hebrew is preferred,
then embedded English. OpenSubtitles is optional for additional Hebrew/English
coverage; configure privately using /opt/media-stack/configure-subtitles.py.
If English is neither embedded nor available from a configured provider, it
cannot be fabricated. Release matching does not guarantee perfect timing.

Native subtitle URLs are Jellyfin-relative /HomeCatalogue/Subtitles/... paths.
The signed relay validates through the bridge and converts Hebrew SRT to WebVTT.
Embedded subtitle tracks explicitly advertise Embed; external tracks advertise
External. Explicit selections including None survive PlaybackInfo refreshes.
A player opened before a deployment must be stopped/reopened to get new URLs.

Poster HE/EN/? badges represent confirmed subtitle availability, not torrent
availability. A show badge does not establish that every episode has subtitles.
Hourly checks update 25 badges under a 200-search/day application budget without
bulk subtitle downloads. Actual preferred subtitles are downloaded on demand.

## Scheduling and restart

media-catalogue-sync.timer checks at 00:00, 06:00, 12:00 and 18:00 Asia/Jerusalem,
with up to two minutes jitter and Persistent=yes. Its discovery-sync command
refreshes recent metadata and episode lists of indexed/opened shows. IMDb bulk
source data is refreshed about daily; it is not downloaded four times daily.
A separate last-full-sync marker prevents small incremental changes from
accidentally postponing the next daily rebuild. Index replacement is atomic.

media-source-availability.timer: every 15 minutes.
media-saved-downloads.timer: every minute.
media-catalogue-badges.timer: hourly.
media-catalogue-collections.timer: every 30 minutes.
media-cleanup.timer: hourly legacy completed-download retention.

Docker's media-storage.conf drop-in and media-stack.service require the actual
media mount before startup. Docker/container restart policies and these timers
are enabled. Dependencies and controlled service/container restarts are checked;
no whole-machine reboot was performed during this change.

## Validation and recovery

python3 /opt/media-stack/catalogue/test_app.py
python3 /opt/media-stack/catalogue/test_features.py
python3 /opt/media-stack/catalogue/check_subtitle_delivery.py
python3 /opt/media-stack/catalogue/check_saved_download.py

The last two are live checks for root while nobody is watching. The download
check uses the publisher's Big Buck Bunny source and leaves the completed file
for owner testing. No watch state is modified. The local movie successfully
served two HTTP 206 ranges through its original Jellyfin item, at offsets 0 and
1,048,576. Unit checks cover episode selection, no external-torrent mutation,
managed-file retention, active playback protection, historical watched flags,
changed files, year/paging beyond 1,000, Hebrew alias lookup, and restart recovery
between rename and ready-state persistence. Server checks cannot establish
physical TV rendering, every torrent's viability, or subtitle synchronization.

Operational logs: docker logs catalogue-bridge, docker logs jellyfin, and the
corresponding systemd journals. SQLite state and the catalogue index live under
appdata/catalogue. The full local plugin source/build outputs are under
/opt/media-stack/vendor/dynamic-library-src; retain the upstream MIT license.

Backups before these changes are in /root/server-setup-backups/before-trailers-*
and /root/server-setup-backups/before-catalogue-expansion-*. The latter includes
the prior DLL/meta, Compose/app/tests/docs, timers and server context. Preserve
current media and download manifests before rollback; restore the old DLL/config
and Compose only while services are stopped. Do not delete saved media as part
of rollback. Detailed milestones are in <SERVICE_HOME>/SERVER_CONTEXT.md.

Metadata sources: IMDb Non-Commercial Datasets, used locally for personal
non-commercial access (https://data.imdb.com/non-commercial-datasets/), Cinemeta,
and Wikidata (https://www.wikidata.org/wiki/Wikidata:Data_access). Posters and
trailer links come from metadata providers; no trailer videos are mirrored.

## Poster subtitle indications

All indexed-catalogue and search previews use the local poster renderer: HE
means confirmed Hebrew (green), EN confirmed English (red), and ? unchecked/unknown (gray). These
are cached title/release availability indications, not a guarantee of timing
for every torrent. Cached Wizdom movie matches count as confirmed Hebrew.
Poster rendering uses cached discovery metadata and bounded concurrency; it
does not fetch complete title metadata or query subtitle providers per image.
The hourly badge job prioritizes recently browsed titles within its 200 daily
lookup budget. Image tags incorporate poster URL and current badge, invalidating
previously unbadged previews and changing when cached availability changes.

## Android TV recovery fixes (1.4.0.6)

Failed/empty PlaybackInfo responses now carry NoCompatibleStream. The Android
TV 0.19.10 client checks this error before creating StreamInfo; without it the
client dereferences a null MediaSource and crashes. Intros returns QueryResult
instead of an array. The two library IDs retained by clients before the Hebrew
rename translate to the current collection IDs before model binding.

Covering name/score/runtime indexes avoid table seeks while filtering the current
year; catalogue counts cache for60 seconds with index/source-state invalidation.
Missing artwork returns a generated title/badge JPEG and caches failures for six
hours rather than retrying an absent image for every card.

Ordinary Play for persisted catalogue movies/episodes uses the bridge even when
a cached browse preview lacks a full premiere date. It cannot fall through to
the native .strm placeholder. Recovery checks cover both empty listings and
listed sources with failed preparation, verifying NoCompatibleStream; the saved
Big Buck Bunny movie returns real prepared metadata and seekable HTTP206 bytes.

Cold magnet metadata adds can take20 seconds; a socket timeout is followed by
status polling rather than immediate abandonment. Already-loaded metadata is
reused. Overall preparation stays bounded at27 seconds, with time reserved for
probing, so a failed source returns an explicit error rather than waiting beyond
the TV client's request timeout. Slow/dead swarms can still fail; cataloguing a
title does not establish its torrent health.

## Real release months (1.4.0.7)

Default year ordering now uses actual premiere date newest-first within each
year. Selecting score/name retains those secondary sorts. Choose Premiere date
/Release date to restore chronological month/day order. Native Android TV has
no server-supplied month picker; search2026-07 or2026-07..2026-09 for one month
or an inclusive range. Normal TV search result limits remain.

Provider-supplied full dates persist in release_dates independently of the daily
IMDb rebuild, and populate the bulk index released field. Existing metadata was
backfilled; recent two years are enriched during the existing four daily syncs.
Known future dates are hidden from normal browsing. Dates unknown in IMDb remain
unknown: do not invent January1. Month searches include only titles with known
dates, so they do not cover the entire million-title index. Provider timeouts
are deferred to subsequent syncs; opening a title can add its genuine date too.

check_release_months.py verifies the native Items dates/order, Movie/Series month
searches, inclusive ranges and Android TV Search/Hints.

For online catalogue browsing only, the client's Date added/Newest sort is an
alias for real premiere date too; the bulk index creation timestamp cannot
distinguish new releases. Local media libraries keep Jellyfin's native Date
added behavior. Score/name choices retain their within-year sorting.

## Poster consistency on Back (1.4.0.12)

Opening a title can finish a source-list check; a confirmed missing compatible
source or newly known future release may then hide it from subsequent browsing.
This does not remove a media file. Missing artwork does not prove a missing
torrent. Details retrieve listings/metadata/subtitle availability, while movie
data preparation/streaming happens through PlaybackInfo/Play.

Browse, metadata, native item refresh and search now share the same poster URL
and hash tag, preserving the poster when a native DTO replaces the grid card.
Native query DTOs are materialized before image metadata is applied, so deferred
serialization cannot silently discard the fix. Artwork URL updates do not
replace cached media/series DTOs. JPEG writes use unique temporary files and
atomic replacement with readable permissions, including concurrent rendering.

check_poster_navigation.py verifies image bytes/tags across browse, detail,
repeat detail, native refresh and search, plus series seasons/episodes.

## Persistent validation catalogue (1.4.0.9)

The IMDb metadata index is a discovery pool, not proof that a title can stream.
Only saved copies or titles with successful video validation appear in the
online watch catalogue/search. Series become visible after a verified episode;
individual episode lists also hide unchecked episodes. Searching an unknown
title saves its metadata and queues a high-priority check; it may appear later.

The media partition's appdata/catalogue/catalogue.sqlite stores the durable
validation queue, latest state, attempt count, retry time, and prior successful
source proof. The separate imdb/catalogue.sqlite retains bulk title metadata.
Both live on /dev/sda5. Bulk metadata refreshes add missing queue keys without
resetting existing results. A verified series queues its aired episodes too.
Previous negative source listings migrate into retained retry records.

Background validation resumes one minute after each batch (maximum 20 probes
or eight minutes before starting another probe). It pauses between probes when
Jellyfin reports playback; provider/session outages defer the job. Checks run
serially, read a bounded video sample and decode two seconds. They do not save
full films. The existing TorrServer working cache is reused. A source listing
alone is insufficient. At that stage AAC Main-only audio and non-8-bit H264 were rejected; see the
current LC conversion section below. Failed candidates can fall through to alternatives.

Failures retry after 6h, 12h, 1d, 2d, and so on, capped at 30d. Successful proofs
remain until newer negative evidence or a streaming error invalidates them;
old metadata/proofs are retained for future retries. Successful sources are
checked again at Play, rather than re-downloading every old title each sync.
This cannot guarantee an entire remote torrent or every physical client.
The catalogue initially shrinks and grows as background verification succeeds.

GET /validation exposes aggregate queue counts; GET /ready supplies ready keys
for the local Jellyfin result filter. These endpoints expose no provider URL,
torrent magnet, authentication token or signed playback URL. Existing local
libraries, watched-download cleanup and storage reserve stay in place.

Search safely handles null/unknown release dates. Provider search metadata is
cached for six hours (five minutes after a lookup error); unknown title keys
are queued in one database transaction. HTTP 429 pauses source validation for
15 minutes, and provider server errors pause it for five minutes. Native search
readiness filtering applies even when remote enrichment fails. The durable
queue, metadata databases and proofs belong in a separate encrypted runtime
backup; the public GitHub project snapshot intentionally excludes them.

check_durable_validation.py verifies the live queue, ready-only bridge/native
browse and search, and saved-file HTTP206 delivery. Earlier broad-catalogue
checks used unchecked metadata as fixtures and their fixed result counts no
longer describe the ready-only live library.

One in five probes is reserved for due failed entries, preventing retries from
being starved by a large new-title backlog. Remaining probes prioritize searched
and previously opened titles, then recent discovery pages and popular titles.

## OMV worker page and first-pass breadth (1.4.0.10)

OMV Diagnostics > Media workers has auto-refreshing schedules/health and queue
counter tabs. Most initial probes now check distinct movies and first show
episodes; one in five slots covers extra episodes and one in five reserves due
retries. Previously extra episodes could take priority over most new titles,
so a high passed-source count could still mean only a few visible shows.

An owner-requested 60-second AAC Main test is stored separately under local
Movies/Tests/AAC Main TV test. It uses Big Buck Bunny H264 video with AAC Main
stereo audio. Search `AAC Main TV test` in Jellyfin. It is not a full downloaded
film or a torrent-readiness claim. Check TV picture/sound and whether playback
is Direct Play before widening AAC Main acceptance. Canelones' torrent stalled
in preparation; it was not falsely marked ready. No per-title AAC Main bypass is enabled for online playback.

Dynamic Library 1.4.0.10 lets actual existing local video files use Jellyfin's
native PlaybackInfo handler. Upstream's persisted-item branch previously
returned an empty response even for ordinary local MP4/MKV files. Catalogue
.strm files retain their verified bridge source handling and explicit errors.

## Earlier audio-policy resync (superseded by LC conversion below)

AAC-LC sounded better than the AAC Main test on the owner's Chromecast. Online
preparation now excludes AAC Main tracks and offers AAC-LC first among compatible
choices, preserving original stream indexes and profiles. AAC Main-only sources
remain unavailable; actual local diagnostic samples use native Jellyfin playback.
Prepared results include audio_policy1 and old prepared caches are not reused.

A one-time audio-policy-resync-v1 marker moves prior failures to due for a fresh
source lookup and queues existing successful sources in a durable audio_recheck
table. Existing verified titles stay visible during the audit. A new failed
check removes only that source's readiness. One in ten probes audits a saved
success until those audits finish, alongside new-title checks, episodes and due
retries. The migration does not reset metadata, attempts or later retry backoff.
Source proofs retain the observed audio codecs/profiles and policy version.
The OMV progress tab shows failed entries scheduled and remaining audio audits.

That earlier filtering-only policy is superseded by the deployed LC conversion mode described below.


## Current audio delivery: direct LC or verified LC conversion (1.4.0.12)

AAC-LC sources retain direct playback. A selected non-LC track (including AAC
Main, AC3/EAC3 and MP3) can now play through Jellyfin native HLS with video
stream copy and AAC-LC stereo audio at192kbps/48kHz. Downloaded media files
remain unchanged: conversion happens during playback, whether input is the
buffered torrent, the original page's saved file, or a native local library
item. No completed full download is required before torrent playback starts.

The bridge validates a small converted sample and its decode before marking a
conversion-dependent source ready. Codec conversion alone is not proof of
torrent availability: failed metadata/data probes, failed conversion and
unknown VOD duration remain unavailable for retry. Video format restrictions
remain. Selected stream indexes, source profiles and audio_policy2 are stored.
Prepared results from the earlier policy cannot bypass the current checks.

The plugin registers original verified media sources with Jellyfin's native
media-source provider. Native HLS handles segment production, forward/backward
seek, authentication and encoding cleanup. Its URL forces VideoCodec=copy and
AllowAudioStreamCopy=false; no video scaling, video encoding or subtitle burning
is requested. External and embedded text subtitles remain selectable as VTT,
with explicit None preserved. Native response DTOs do not mutate stored
subtitle metadata. Newly browsed canonical movie/episode records are persisted
if the asynchronous library scan has not yet created them.

Run configure-audio.py after restoration to set native transcoder throttling
to60seconds ahead and remove old segments with90seconds retention. Encoding
options are updated through Jellyfin's supported API; private configuration
and API keys are not exported. Temporary HLS data uses the existing Jellyfin
cache on the media partition, not the managed saved-download directory.

The one-time audio-policy-resync-v2 marker makes prior failures due for fresh
source checks, and re-audits older successful proofs gradually. Already
verified audio_policy2 sources are skipped. Titles stay visible during audits
until a real failure invalidates them. Existing metadata, attempts and later
retry backoff survive sync/reboot. The OMV progress tab displays current v2
resync totals and remaining audits.

Validation tools: check_audio_conversion.py checks actual LC/H264 HLS segments,
seeking, direct LC and unchanged watched-state fields; check_audio_subtitles.py
checks Hebrew VTT/None and unchanged native subtitle codec. AudioChecks in
the plugin source checks authorization, media-source cloning and first-open
canonical movie/series/season/episode creation without overwriting items.
