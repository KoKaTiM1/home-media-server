# OMV media worker monitor

The installed OMV administrator page is **Diagnostics > Media workers**.
It provides three read-only tabs:

- **Progress overview**: final v5 verified titles, keys still lacking v5 proof,
  retries, initial-audit progress, final passes in the last hour, PC contribution,
  current Windows operation and the most recent categorized PC issue.
- **Workers and schedules**: server and optional Windows jobs, health, running/waiting/disabled
  state, schedule, next run/countdown, last start/result and current check.
- **Catalogue queue and progress**: pending movies/shows/episodes, failed and
  due retries, passed source checks, distinct movies/shows, visible indexed
  counts, metadata outside the queue, recent activity and audio resync progress.

The active Windows LAN source verifier and its gateway appear ahead of routine
jobs. Retired metadata-only helper rows are omitted from this page. The source
verifier reports PC heartbeat, active operation, title passes, unavailable
outcomes and checkpoints. A successful probe or sample operation alone does not
count as a fully verified title. Both tables display all current rows without
silently cutting off the verification status at the old page limit.

The optional **Media verification** widget uses OMV's supported dashboard grid
to show the same overview as compact cards on the OMV home page. Enable it in
the dashboard's widget selection if it does not appear automatically. The
overview reads the existing background counter snapshot and a small live job
record; opening OMV does not recount the large catalogue. The Windows 0.2.0
worker reports fixed decoder phases every 10 seconds. Until the PC image is
updated, the overview still shows its claimed probe/sample stage and progress
observed at the server. Error reasons are fixed categories, not raw logs.

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
