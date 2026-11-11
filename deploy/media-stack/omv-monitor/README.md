# OMV media worker monitor

The installed OMV administrator page is **Diagnostics > Media workers**.
It provides three read-only tabs:

- **Progress overview**: current V6 verified sources, currently verified movies and shows,
  keys still lacking V6 proof, retries, V6 passes in the last hour, PC contribution,
  connected Windows lanes, current operation, the current Torrentio/MediaFusion
  source attribution (or None when no lookup is active or neither yields an
  accepted candidate) and the
  latest categorized PC issue.
  The one-time V5-to-V6 speed-audit card shows passed, failed and pending counts
  only while checks remain pending; it disappears automatically when that audit
  finishes. Its historical records and normal failed-source retries are retained. The V6 first-sweep row
  reports one-attempt coverage of the starting queue, with passes, conclusive
  failures and deferred checkpoints counted separately.
- **Workers and schedules**: server and optional Windows jobs, health, running/waiting/disabled
  state, schedule, next run/countdown, last start/result and current check.
- **Catalogue queue and progress**: pending movies/shows/episodes, failed and
  due retries, passed source checks, distinct movies/shows, visible indexed
  counts, metadata outside the queue, recent activity and current proof audits.
  Current unavailable titles are grouped by their latest saved failure reason;
  counts update with the normal counter snapshot and drop when a title passes.
  Worker checkpoints are not included in these failure counts.

The active Windows LAN source verifier and its gateway appear ahead of routine
jobs. Retired metadata-only helper rows are omitted from this page. The source
verifier reports PC heartbeat, active operation, title passes, unavailable
outcomes and checkpoints. A successful probe or sample operation alone does not
count as a fully verified title. Both tables display all current rows without
silently cutting off the verification status at the old page limit.
Completed migration-audit cards and detailed rows are omitted; current queue,
V6 first-sweep coverage and final pass counts remain visible.

The optional **Media verification** widget uses OMV's supported dashboard grid
to show the same overview as compact cards on the OMV home page. Enable it in
the dashboard's widget selection if it does not appear automatically. The
overview reads the existing background counter snapshot and a small live job
record; opening OMV does not recount the large catalogue. Current Windows
workers report fixed decoder phases and categorized reasons rather than raw
logs. Worker/backend counts are configurable; assignments remain governed by
source ownership, viewing and resource safeguards.

Worker rows refresh every 10 seconds. A separate worker prepares counters two
minutes after boot and then one minute after each completed snapshot. The page
reads the saved snapshot without launching heavy database queries. Metadata
pool/outside-queue counters cache up to five minutes; values include snapshot
timestamps. A cold database can take longer than a minute to count. During real Jellyfin
navigation, the snapshot job yields to the shared 30-second navigation lease;
running queries can abort safely, leaving the previous snapshot and timestamp
intact. Images and maintenance API calls do not extend that lease.

Waiting between successful oneshot runs is healthy. Failed jobs and disabled
timers are reported separately. Counter generation uses limited CPU/IO and a
192 MiB memory limit. The page is read-only, requires the OMV administrator role,
and exposes no shell input, service controls, source URLs or credentials.

The catalogue table omits the one-time audio-resync scheduling marker and the
local AAC demonstration-file status; neither describes current verification
progress. It also avoids repeating the conversion count already shown in the
stronger-check breakdown.

The validation scheduler allocates probes across new titles/first show episodes,
extra episodes, due failures and older audio-proof audits. Many verified episodes
can belong to one show: source-check counts are not distinct library counts.
Metadata pool size is not the number of verified playable titles.

The deployed audio mode is **audio_policy2**: AAC-LC can play directly, while
AAC Main and other supported non-LC audio require a verified AAC-LC conversion
sample before eligibility. Native Jellyfin HLS supplies audio-only conversion
for torrent and saved-file sources. Direct Main pass-through is not used for
these catalogue sources. The owner confirmed converted playback on Chromecast.

The durable **audio-policy-resync-v2** marker schedules older failed entries
and proof audits once. Existing verified titles stay visible pending an actual
failed recheck; restarting the worker does not reset attempts or retry backoff.

The metadata preparation view also reports sanitized metadata-fetch retries for
the last 24 hours, grouped by processing stage, category, allowlisted exception
type and upstream HTTP status. The stage distinguishes provider fetch, response
envelope parsing, coordinator receipt and retry receipt handling. It excludes
titles, IMDb keys, URLs and exception text. Retry counts are stored in hourly
aggregates with a seven-day retention window; they describe metadata fetches,
not failed media verification.
The private AAC TV-test result supplies reported test status; dashboard counters
alone cannot establish that a physical client plays every source correctly.

Restore this folder to `/opt/media-stack/omv-monitor`, configure the destination
media-partition path in `monitor.py`, and run `python3 install.py` as root.
The installer uses supported workbench manifest/engined RPC extension locations;
OMV core files remain unchanged. It restarts only the OMV RPC engine.

User report counters show keys awaiting a fresh check, failed on the latest
reported check and passed after a report. These are latest per-key states, not
a count of every click. Reports are prioritized by the ordinary source worker
and obey its playback/navigation/provider pauses.

Playback events shows the latest 200 structured foreground failure/checkpoint events for 30 days: title, stage, category and current fresh-check status. Request/network/unknown errors preserve existing proof; confirmed source/decode failures request authoritative re-verification. This administrator-only view never reads raw exception messages or stream URLs.

The progress overview also reports catalogue-metadata prerequisite waits. Those
remain in the initial-sweep remaining count until source verification runs;
metadata arrival alone does not increase V6 passes or completed checks.

Partial provider coverage retries have a separate counter and due count. These are completed but incomplete-coverage searches scheduled on the 6h-to-30d source retry route, not confirmed missing media, successful proofs or admission-only waits.
