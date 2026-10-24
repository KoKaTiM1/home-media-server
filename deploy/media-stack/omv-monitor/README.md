# OMV media worker monitor

OMV administrator page: Diagnostics > Media workers, with two tabs:

- Workers and schedules: all six jobs, health/running/waiting/disabled state,
  schedule, next run/countdown, last start/result and current validation key.
- Catalogue queue and progress: pending movies/shows/episodes, failed/due retry
  counts, passed source checks, distinct movies/shows, indexed visible counts,
  metadata outside the queue, last-hour activity and last completed check.

Worker rows refresh every10 seconds; expensive queue counts cache60 seconds.
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
