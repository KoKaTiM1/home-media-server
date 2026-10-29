# Home media catalogue

Current deployed behavior, reviewed 2026-10-28 against Dynamic Library
**1.4.0.21**, Jellyfin **10.11.11**, the bridge and systemd units. This is the
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

Movies and episodes retain native per-user watched status. For online series,
known released-episode totals also include episodes awaiting verification or
indexing: watching all currently indexed episodes does not complete a larger
show. The response's remaining count includes those missing episodes; a whole
show gets a watched check only when all known released episodes are watched.
A new released episode removes completion. Released specials count; future,
duplicate and invalid entries do not. Saved metadata can be incomplete or stale;
unknown totals fall back to native state. No inferred progress is written to
watch history and no provider lookup runs merely to draw the indicator.

In the Android TV app, enable **Settings > Customization > Watched indicator >
Always** to retain both watched checks and unwatched counts. Some app versions
place it under Display. This is a TV-local preference; server configuration
cannot turn it on remotely. **Hide unwatched count** retains checks only; avoid
Episodes only / Never if you want checks on movies and whole shows.

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

Opening a playable movie/episode's details asks the bridge to prepare its source
in advance. One speculative job runs at a time, without a queue, for at most
12 seconds, with a 60-second per-title cooldown. Active or paused playback and
unavailable session status suppress the job; playback beginning cancels it.
A successful response, including subtitle lookup, is cached for five minutes.
After that cache expires or the bridge restarts, Play can reuse durable v5
evidence and codec metadata for the identical torrent/file and selected audio.
The live torrent metadata is resolved again and video requests still require
initial bytes before returning success. Changed sources or audio, exclusions,
newer failures and requested rechecks prevent this reuse. Scheduled background
audits always probe and decode fresh samples. Subtitle choices are refreshed
separately; reuse never advances the stored verification timestamp.
It may fetch torrent metadata and small video/audio probe samples; it does not
request a managed full-film download. Failed speculative attempts do not revoke
existing verification. Torrents opened by background verification or embedded
subtitle extraction are recorded in a private bounded ledger. Opening a playable
title pauses producers and asynchronously drops only ledger-owned torrents with
no active TorrServer readers; the requested/active title's persisted physical
torrent hash is protected even when a season pack was recorded under a different
episode key. Any active stream, managed downloads and watch state are excluded.
Immediate Play waits at most 1.5 seconds
for this cleanup and then continues even if cleanup is incomplete. Immediate Play has a 60-second
preparation budget during the current playback trial and may need to finish
work. Slow peers and client
buffering can still delay startup. Sanitized `details_warm`, `prepared_cache`,
`subtitle_lookup`, `upstream_open` and `stream_ready` events separate preparation,
cache reuse and the initial video-data wait from long-running transfer logs.
`durable_evidence` identifies reuse of a persisted exact-source proof.

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
Temporary HLS files use the configured Jellyfin cache.

| Server request stage | Bound / failure behavior |
| --- | --- |
| Foreground preparation | Shared 60-second budget; Jellyfin caller allows 70 seconds and the navigation lease lasts 75 seconds. Foreground metadata attempts start a fresh clock instead of inheriting a background attempt's age. |
| Plugin request for preparation | 30-second HTTP timeout; failure returns `NoCompatibleStream`, never a successful empty source list |
| Initial stream data | Separate 15-second first-byte budget after preparation; successful video headers require actual data |
| Background validation | Eight-minute resumable batch slice; a torrent may use up to 90 seconds total to expose its file list, while a background producer yields after 45 seconds, temporarily skips that candidate for five minutes, cools that title for 60 seconds, and tries other titles/candidates. The unresolved source remains pending rather than being marked unavailable. Selected-file delivery still uses a separate 45-second useful-progress clock. |

These are per-request limits, not a promise about total TV app time if it retries.
Alternate compatible sources are tried within the preparation budget; the server
does not change sources after delivery starts. Busy preparation and disconnected
clients are not negative source proof. A real source failure clears its cached
preparation and hides it from normal browse while retaining it for retry.

## Subtitles

Wizdom is the default free Hebrew provider and needs no account/key. Movie IMDb
IDs or series IMDb plus exact season/episode identify matches; release names
help rank candidates. Hebrew is preferred, with English where available.
The player offers up to two Hebrew tracks named **עברית 1** and **עברית 2**.
Completed cached extraction of the exact online source is preferred when
available; local media retain native embedded tracks. Missing slots use up to
two Wizdom releases, best filename match first. Identical normalized cue content is
deduplicated, including copies that differ only in cue numbering/newlines.
A second version appears only when available. This offers an alternative for a
poorly timed track; it does not automatically prove or correct synchronization.

The first Hebrew choice is the default. Tracks have distinct original indexes
and content-specific files/URLs; choosing the second does not reuse the first
file. Provider failures on one release can fall through to another within the
existing preparation budget. Signed variant IDs survive the native VTT relay.
Legacy single-file subtitle links remain readable. Native language identifiers
stay `heb`, while titles/display names begin in Hebrew. English and explicit
None retain their existing behavior. Prepared metadata uses subtitle-policy5,
so preparation rebuilds older cached track lists.
OpenSubtitles is optional additional coverage, configured privately through
`configure-subtitles.py`. Missing subtitles cannot be fabricated, and matching
a release name does not guarantee synchronization.

The bridge reads ZIP contents without extracting archive paths and converts
Windows-1255/UTF-16 content to UTF-8. The signed Jellyfin-relative subtitle relay
serves Hebrew/English text as WebVTT. Delivered external tracks and local
embedded text retain explicit None across PlaybackInfo refreshes. A player
already open during an update must be stopped/reopened to use changed metadata.

### Embedded Hebrew cache for online titles

Native extraction cannot read the virtual dynamic catalogue path. Instead,
foreground preparation queues an on-demand exact-source extraction job; Play
never waits for it. The idle worker reads thirty-second windows with preroll,
converts a Hebrew text stream to SRT and checks copied video packet timestamps
for window coverage. It saves cues and a durable cursor, not a full video file.
This can still require reading much of the source over the torrent transport.

Only a completely covered, nonempty subtitle is published as an external signed
variant. Partial cue sets stay hidden. A source/file change uses a different
cache identity; a failed/manual-pending video check cannot authorize extraction.
Existing external subtitles continue working while extraction is pending or
unavailable. Only one embedded Hebrew text track is extracted per source;
bitmap subtitles are unsupported. Exact-source extraction improves release
matching but cannot correct timing errors already inside the original track.

Each window covers30seconds, limiting work lost to a playback/resource yield.
Ordinary cancellation retries after15seconds; source errors retain their longer
backoff. The same playback and memory guards are checked before reading again.
`media-embedded-subtitles.timer` resumes up to sixteen windows within a110-second
slice and checks again15seconds after a slice ends. Source stalls retry later;
ordinary slice exhaustion retains the last completed window and resumes soon.
Active/paused viewing, navigation, title holds and resource pressure defer or
cancel work. The worker shares exclusive title/torrent claims with verification
and never changes video validation proofs or failure counters. Completed output
invalidates only prepared subtitle-response caches; a currently opened player
retains its original metadata. Each job's cached cue data is bounded to2MiB.
OMV Media workers lists the extraction service separately from v5 verification.

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

Validation runs serially, at most 20 entries or an eight-minute work slice.
Verification v5 requires actual decoded video frames and timestamps, plus the
selected audio stream, over six seconds at the beginning and two seconds later.
The later sample rotates between approximately 25%, 50% and 75% on successive
successful audits. Non-LC audio uses fresh video-copy/AAC-LC stereo samples that
are decoded again; old probe/conversion caches cannot count as fresh evidence.
Exit zero with zero frames, incomplete samples and decoding errors fail.

Initial audits cover existing successful sources and failed sources retaining
older positive proof. Older proofs and reported problem titles run first;
user Delete/recheck reports have highest priority. Existing catalogue entries
remain visible until a fresh failed check. Afterwards, one in five ordinary
slots audits successful sources older than 24 hours. New discovery, additional
episodes and due failed retries continue through the durable queue. A passed
episode count is not a count of distinct shows.

Every sample and playback URL binds the exact torrent hash, file ID, path and
length. The last working source is preferred; verified backups precede new
sources. New releases must match title/year or the requested episode, exclude
samples/trailers and fit the existing video policy. Within eligible sources,
reported seeder counts (or peers where seeders are absent) rank first, with
quality/audio preferences breaking ties. Counts are availability hints and
never verification proof. Ambiguous multi-movie packs require a matching file
name; provider file indices cannot safely identify TorrServer's sorted files.

Progress is checked about every two seconds. Unique delivered sample ranges,
decoded frames and the current required reader pieces count as useful progress.
Piece attribution requires an exclusive sample lease and a sole newly-created
reader. Existing/unrelated readers disable that attribution. Global torrent
bytes, peer counts, activity in another episode and repeated ranges do not reset
the 45-second stall timer. Slow sources keep their chance while useful data
arrives. The eight-minute slice and foreground request limit checkpoint work
rather than creating negative proof; partial beginning samples are reusable
for at most one hour for the identical source/track/audit. Retry Play later
when preparation has to continue in the background.

Workers yield during active or paused playback and navigation, including during
a probe. Missing session information defers work. Cancelling a sample closes
its own blocked upstream reader; it does not drop a shared torrent or stop the
viewer's connection. Per-title and per-torrent process locks bind work consistently; a global
verification lock permits only one FFmpeg/FFprobe job across CLI/HTTP processes.
Decoder, encoder and filter threads are limited to one for this small server.
Decoded verification frames are reduced to 160 pixels wide before checksum
evidence, with bounded output interleaving; this prevents HD preroll/audio
offset queues from exhausting memory. Playback and conversion video stay copied
at their original resolution.
A killed process or memory/thread allocation failure checkpoints the job instead
of declaring the source unplayable. Work, winners, alternatives and audit results persist
across reboot; this does not pre-download full movies or enable every torrent
on startup. A two-window sample is not a guarantee of complete-film playback.

Failures retry after 6 hours, 12 hours, 1 day, 2 days and increasing delays capped
at 30 days. Prior successful proof remains saved for diagnosis; newer failed
evidence determines current readiness. Provider HTTP429 triggers 15 minutes
cooldown; provider server errors trigger five minutes. Initial v5 audits replace the one-time audio-policy audit without resetting
metadata, attempts, failed-entry retry dates, watch history or saved files.

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

Details-page preparation status checks use an isolated bounded HTTP budget
without their own cancellation callback. This prevents recursive self-cancellation
while retaining active/paused and unreachable-session suppression. The regression
check uses a real local HTTP session response, including paused playback.

## Report a broken online title with Delete

For online Movies, Shows, seasons and episodes, the native **Delete** action
now means hide and request fresh source verification. It does not remove the
STRM record, artwork, downloaded file, favorite or watched history. A movie or
episode is queued directly; a season/show queues its known episode keys (or a
first-episode seed if none are known). The normal catalogue hides reported keys
until a fresh background video/audio check passes. Existing Favorites remain
accessible under the existing unavailable-favorite rule.

Reports bypass the title's current retry delay and receive the next eligible
worker slot ahead of discovery. Provider cooldowns, active/paused playback and
navigation priority still apply. Prepared-response caches are cleared before
that worker attempt; the saved torrent may be retried first but must pass fresh
v5 decoding. A cached resolution, header or old conversion is not a new pass. Old proof is retained for diagnosis; failures stay hidden
and return to normal retry backoff. An in-flight check begun before the report
cannot undo it. Requests and results survive process restarts.

The Android TV app calls this action Delete and keeps its existing confirmation
dialog. If it is hidden, enable **Settings > Customization > Enable media
management** on that TV once. It requires existing deletion permission; this
feature does not grant it. Use the online libraries for this reporting action.
Local Movies/Shows/Downloads keep native real-file deletion behavior. Mixed
online/local bulk deletion requests are rejected before anything is changed.

Authenticated native DELETE is intercepted before filesystem removal; a failed
report-queue write returns an error instead of falling back to deletion. The
plugin writes an atomic private event under `.recheck-requests/pending` on the
existing shared catalogue volume. The bridge commits it once and archives the
event under `processed`. No public reporting mutation endpoint or new credential
is introduced. OMV Media workers > Counts includes reports waiting, failed and
passed. A past sample or a successful recheck still cannot guarantee future
torrent availability or compatibility with every TV.


## Stronger-verifier dashboard

OMV **Diagnostics → Media workers** displays the current verification phase,
useful received/downloaded bytes, speed, available required-piece buffer, time
since useful progress and direct/converting audio. Queue counters include initial
audit awaiting/passed/failed, passed v5 direct/conversion sources, daily audits
due, resumable preparation checkpoints, failed titles and the existing complete
discovery queue. A paused worker may retain its last saved phase; the worker's
health/message explains whether it is currently running or waiting.


## Temporary title holds

A private timed hold in `verification_holds` excludes an exact movie/episode key
from initial/daily audits, manual-report work, checkpoint resumption, discovery
verification and managed saved-file cleanup. Other titles continue normally.
The hold preserves existing readiness, source proof, files and native watch/
favorite records; it does not mark an audit complete or fabricate a pass.
Holds persist across reboot and expire automatically. Foreground Play remains
available, and a hold does not pin torrent pieces forever or guarantee peers
will still be available later. Watch progress stays in Jellyfin's native state.

The worker and managed-download jobs load the updated host-bound Python code
on their next invocation, so adding a hold does not require restarting Jellyfin
or the catalogue bridge. OMV counters show the number of protected titles and
the earliest expiry. Native active/paused-session and navigation safeguards
continue to defer heavy work during detected viewing.


## Background metadata recovery and current subtitle findings

Existing series folders created by poster caching can appear in native Jellyfin
as a bare IMDb number with no episodes. The availability worker hydrates at
most two such already-present, ready series per batch using cached metadata.
It writes released episode metadata and reports that exact directory to
Jellyfin's media-update endpoint so newly created children are discovered.
Bare parent titles also use a supported metadata update with the existing full
DTO and UserData omitted. The local native index is read-only; all metadata
writes go through Jellyfin APIs.
Unchecked episodes are still filtered; metadata recovery is not a playback pass.
Work is deferred during navigation. No full-library scan or service restart is
required; native display refresh may complete asynchronously.

Movie source relevance also accepts current/original English names from the
local IMDb projection and saved English aliases for the same IMDb identity.
This handles renamed films whose remote metadata retains a working title,
while requiring the release year and excluding unrelated titles.

Downloaded Hebrew tracks and embedded Hebrew tracks use different delivery
paths. Native embedded-track extraction can fail before producing the cached
SRT; this is distinct from zero provider matches. A working downloaded second
option is not evidence that the embedded first option extracted successfully.
That native extraction issue remains under investigation; no subtitle-source
expansion or playback-service restart was applied as part of this repair.

Post-verification investigation and acceptance criteria are tracked in [GitHub issue #1](../../../../../issues/1).
## Search and playback diagnostics

Search includes local ready identities independently of provider result limits,
preserves distinct IMDb releases, and uses the indexed movie year when regional
provider metadata labels the same film differently. Actual release dates/months
remain unchanged. Native and catalogue hits are merged by identity, ranked by
title/year relevance, and paginated once on both Items and TV Search/Hints.
Broad native searches above the merge prefix retain ordinary native paging.
Unavailable titles remain hidden; search never manufactures source readiness.

Foreground request timeouts, HTTP errors, disconnects, checkpoints, local errors
and unknown faults retain existing video proof. Typed source/binding/stall or
decode failures request high-priority background verification, hide the title
while pending, and allow only a fresh authoritative result to restore it. No
exact-source bytes from a remote decoder process is a transport checkpoint.

OMV Diagnostics > Media workers > Playback events shows the latest200 sanitized
failures/checkpoints for30days, including title, stage, category and current fresh
check status. Raw errors, URLs, credentials and device/user identities are not
stored in that table. Existing successful phase timings remain structured logs.

## Hebrew titles and descriptions

Cinemeta remains the English metadata fallback. Hebrew enrichment uses a
Wikidata record only after its IMDb property exactly matches the local title;
when a Hebrew Wikipedia article exists, its lead extract supplies a fuller
Hebrew description. Title and description are selected independently, so a
missing Hebrew field leaves its English value intact. Original and English
names remain stored as search aliases. Provider failures retain cached values.

A dedicated worker runs every five minutes while idle, handling up to sixteen
ready movie/show identities with a 90-second processing budget per invocation.
Newly verified identities receive priority, with older entries interleaved to
keep the backfill moving. Work is resumable in the catalogue database. Wikimedia
requests are serialized at one per second, with provider Retry-After/backoff
respected; there is no daily quota imposed by this integration. The six-hour
discovery sync can also advance the same queue under its shared process lock. New titles use the same cached
localization when they are opened/imported. Existing NFO files are updated
atomically with only title, original title and plot; other tags and media proofs
are preserved. Jellyfin refreshes only the affected parent items, and the batch
is deferred while playback or foreground catalogue work is active. Metadata
refresh uses Jellyfin's API and does not write user watch/favorite state.

Native Items lists, individual details and TV search hints apply the saved Hebrew
fields after cached catalogue DTOs are assembled. The plugin reads local labels
in batches of at most 100; browsing performs no translation-provider requests.
English fields remain when their Hebrew equivalents are missing or the cache is
temporarily unreachable. This changes displayed text and preserves poster artwork,
item identities, release years and per-user data.


### v6 delivery-speed evidence

v6 adds Debian-measured exact-file delivery at 1.5 times file-size/runtime bitrate.
Two bounded range reads (4–32 MiB each, 60 seconds total) include connection time;
startup must return bytes within ten seconds. The initial torrent cache's entire
filled-byte count is pessimistically subtracted from each window's delivered
bytes. Each window must prove at least 4 MiB of uncached delivery, and cumulative
throughput must also pass, so read-ahead cannot manufacture a network-speed pass.
Large/ambiguous caches checkpoint until the bounded uncached test is possible;
no cache eviction or torrent removal is used to force a test. Other readers,
viewing/navigation, exhausted work budgets and resource holds checkpoint safely.

For an unchanged accepted v5 file/audio policy, v6 preserves the decoded samples
and original decode timestamp and only checks speed. Changed sources/audio and
manual fresh rechecks decode again. Gradual migration preserves legacy visibility
until an actual source failure; old v5 playback evidence remains usable during
migration. Speed failures exclude that exact source for six hours, trying other
matching sources; titles with no accepted alternative remain retryable. Daily
background audits refresh speed evidence without redecoding unchanged torrents.
Local files retain their decode checks and bypass torrent-speed requirements.
Speed evidence describes the machine and network path that measured it, not
future peer availability or the client Wi-Fi path. Optional Windows package
0.3.0 can perform the complete V6 check with a private local TorrServer before
Debian retrieves torrent metadata. Debian still ranks candidates, validates raw
evidence and commits the final proof. Exact accepted V5 samples can be reused;
Windows supplies the new speed evidence. Windows-local failures use one normal
Debian fallback when its resource gate is healthy, otherwise the title remains
pending. Old Windows workers keep their fixed probe/sample workflow.
