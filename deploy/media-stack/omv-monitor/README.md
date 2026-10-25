# OMV media worker monitor

OMV administrator page: Diagnostics > Media workers, with two tabs:

- Workers and schedules: all scheduled jobs, health/running/waiting/disabled state,
  schedule, next run/countdown, last start/result and current validation key.
- Catalogue queue and progress: pending movies/shows/episodes, failed/due retry
  counts, passed source checks, distinct movies/shows, indexed visible counts,
  metadata outside the queue, last-hour activity and last completed check.

Worker rows refresh every10 seconds. A separate worker prepares counters in the background, one minute after each completed snapshot; the page reads the saved snapshot immediately. Metadata pool/outside-queue counters additionally cache5 minutes. Each value shows its snapshot time.
Waiting between successful oneshot runs is healthy. A disabled timer or failed
run is reported separately. Data is read-only; no start/stop/delete actions or
shell input are accepted. RPC methods require the OMV administrator role.
No source URLs, signed playback links, passwords or API tokens are returned.
The helper reads systemd properties and read-only SQLite connections.

Deploy/restore at /opt/media-stack/omv-monitor and run `python3 install.py`.
This uses supported workbench manifest and engined RPC extension directories;
OMV core files are not modified. Only the OMV RPC engine restarts. On another
server, adjust DATA in monitor.py to the restored media partition first.

The initial validation scheduler allocates most probes to new titles, with
slots for existing-series episodes and due failures. Many passed episodes can
belong to one visible show; source-check counts are not library title counts.
AAC Main test status is separate from proof that a physical Chromecast plays it.

The owner reported distorted AAC Main audio and better AAC-LC audio in the
paired60-second samples. Both decoded without errors on the server. During the
Main test, the TV session reported video transcoding with IsAudioDirect=true;
the AAC audio was passed unchanged. Keep AAC Main-only filtering enabled.
The exact encoder-versus-TV-decoder cause is unproven. The private persisted
aac-main-tv-result.json supplies the reported result to the progress page.

The audio-policy resync counters show how many prior failures were scheduled
once and how many existing successful sources still await an audio audit.
Verified entries remain visible pending an actual failed recheck; restarting
the worker never resets the resync marker or the increasing retry waits.

The counter snapshot job is included in the worker table. It is enabled at boot,
uses low CPU/IO priority and a192MiB memory limit, and can take longer than a
minute on a cold database. Counter reads never launch million-row queries
inside an OMV web request.

Current mode is audio_policy2: AAC Main inputs are eligible after a verified
AAC-LC conversion sample; direct Main delivery is not used. The current resync
counter reads audio-policy-resync-v2, which supersedes the earlier filter-only
policy. Native HLS handles delivery for torrent and saved-file sources.
