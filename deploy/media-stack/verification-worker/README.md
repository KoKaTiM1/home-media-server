# Windows V6 verification workers

Launcher package **0.4.14** uses verifier image **0.4.13** and TorrServer image **0.3.0**. This package adds an opt-in, sanitized request lifecycle trace for proxy start/stop/handler close. The window is capped at one hour; the per-worker log is capped at 1 MiB and expires after 24 hours. It does not change the decoder protocol, V6 policy, torrent selection, cleanup behavior, or proof acceptance. The trace omits titles, hashes, URLs, IPs and media payloads. HEVC Main 8-bit SDR up to 1080p remains supported after the owner TV picture/audio test. Replay/seek acceptance remains tracked separately; HDR, Main10, 4K and AV1 are excluded. Verifiers
run with Docker `--init` to reap orphaned decoder children after cancellations.
Start/Update automatically drains and recreates containers with a mismatched
image or missing init while preserving identities, config, data and backend state.
`UpdateLane` upgrades exactly one selected worker and retains its prior container
for rollback; `RollbackLane` restores it without deleting data.

The optional Windows PC verifies torrent sources using the same V6 source, speed,
video sample and audio requirements as Debian. Debian chooses candidates and
validates every proof before changing library readiness.

### Opt-in proxy lifecycle observation

For a bounded investigation, create `data/proxy-lifecycle-observe-until` in one
worker's data folder and write a Unix epoch expiry no more than 3,600 seconds in
the future. The worker records only random trace/request IDs, fixed lifecycle
event names, GET/HEAD method, elapsed milliseconds, bytes sent and coarse close
reasons in `data/proxy-lifecycle.jsonl`. It records no title, source hash, URL,
IP address or media payload. The file is capped at 1 MiB; expired records are
pruned and the file is removed 24 hours after its last event. The worker removes
the observation marker after it expires. Delete the marker to stop immediately.

## Configure capacity

`config/workers.json` is created on first Start or Update:

```json
{"workers": 8, "torrservers": 2}
```

Edit these counts and run `02-START-ALL.cmd`. Supported counts are 1–16 workers
and 1–8 TorrServers, with TorrServers no greater than workers. Workers are spread
in consecutive balanced groups: 8/2 assigns 1–4 to backend 1 and 5–8 to backend 2;
10/3 assigns groups of 4, 3 and 3. A coordinator acknowledgement is required before
running containers are changed. The server keeps a sixteen-worker ceiling.

Each TorrServer is configured with a 2 GiB memory limit; each verifier has a
512 MiB limit. Docker must have the total configured limits plus 2 GiB overhead
available in its VM allocation; default 8/2 requires at least 10 GiB allocated.
Windows must have at least 4 GiB currently available before applying capacity.
These checks do not reserve that memory or guarantee a particular check rate.

## Four launcher commands

- `01-UPDATE-AND-START.cmd`: verify checksum, load packaged images, reconcile topology.
- `02-START-ALL.cmd`: start or apply the counts in workers.json.
- `03-LOGS.cmd`: combined prefixed verifier logs. Ctrl+C closes the view.
- `04-STOP-ALL.cmd`: drain workers, then stop all managed workers and torrent backends.

Keep `worker.ps1`, the image archive, checksums and documentation with these files.
Do not delete config/, data/, data-N/, torrent-state/ or torrent-state-N/.
Existing worker identities remain in their original data directories. Increasing
capacity adds identities; reducing capacity preserves surplus directories.
Affected current-version workers finish their current job before replacement.
Legacy clients checkpoint safely on their first upgrade. A drain timeout leaves
the affected container running; inspect it before retrying the launcher.

The original backend retains the name home-media-verification-torrserver and
state in torrent-state/. Additional backends use numbered names/directories.
Workers share locks only with workers assigned to the same backend. Torrent APIs
are Docker-internal and publish no host ports.

## Connection and configuration

Private coordinator credentials remain in config/verification.local.json.
The TLS certificate pin and worker bearer token are required. server_url is the
preferred LAN endpoint; optional fallback_server_urls are tried after connection
failures using the same token and TLS pin. Preserve data directories when changing
network settings or updating images. Never publish private configuration.

The capacity helper uses the same authenticated connection and only accepts
bounded worker/backend counts. It cannot run arbitrary host commands. Heartbeats
cannot change requested capacity. Existing older clients remain supported.

## Scheduling, results and recovery

The coordinator grants exclusive title jobs with renewable leases, generation
fencing and idempotent result receipts. Queued work cannot exceed two jobs per
configured worker, subject to a thirty-two-job total bound. One title cannot have
two active authoritative owners. Debian continues independently when Windows is off.

The initial V6 sweep takes a durable snapshot of the eligible source queue and
gives each untouched key one full attempt before retry work resumes. A first
attempt has up to four minutes of preparation/execution; ready-queue waiting
has its own bounded 240-second window. The Windows decoder stops with a
30-second receipt margin. Checkpoints are deferred, not counted as failures.
After the sweep, existing retry/cooldown rules apply. Retry jobs keep the full
470-second decoder budget, with a 500-second server receipt deadline. Candidate
selection and alternate-source checks are unchanged.

Temporary local source failures use durable title cooldowns: 5 minutes, 15 minutes,
1 hour, then 6 hours on repeated failures. Audit and discovery selectors respect
the same cooldowns. A pass or explicit new manual recheck clears the retry history.
A checkpoint never becomes a failed media proof merely because its retry is delayed.
Playback interruptions and connection outages remain scheduling interruptions.

Windows-local failures may request Debian fallback; its separate resource gate
can defer that request. Host TorrServer OOM does not disable otherwise healthy
Windows-local torrent work. Host-memory and catalogue-bridge protections still apply.
The 15-second resource guard also keeps a bounded, identifier-free diagnostic
history at `/var/log/home-media/torrserver-resource.jsonl` (three 1 MiB rotated
files plus the active file). It records TorrServer/bridge cgroup memory breakdown,
host availability, OOM/restart counters, queue/worker counts, provider/phase lane
counts, gate decisions and the number of tracked background-torrent cleanup
records. It contains no title keys, infohashes, source URLs or client addresses.
Catalogue playback logs include a matching cleanup summary (dropped, already
missing, active-reader protected, concurrently protected, or API error counts).

OMV Diagnostics > Media workers reports requested topology, worker concurrency,
distinct healthy torrent backends, attempts, distinct titles, V6 passes and
sanitized failure categories. Operations, retries and complete title passes are
separate measurements. No raw error text or source URLs appear in these metrics.

## Build and restore

Build the worker Dockerfile with an allowlisted context containing catalogue/*.py
and the verifier sources; exclude secrets and runtime data. package.py produces
verification-worker-v0.4.10.zip from the worker 0.4.10 image and torrent backend 0.3.0.
The archive contains exactly four CMD launchers and no enrolled data or credentials.
On restore, install the coordinator and resource gate, provision private TLS/token
configuration, then copy the package to Windows and run Update. The topology is
applied by Start; no per-lane server editing is required. Initial server rollout
retains existing capacity until the updated launcher requests a new topology.

Issue #4 tracks throughput; issue #6 tracks playback and backend stability. The
100+ distinct completed title checks/hour target requires measurement. Preserve
all V6 quality checks when evaluating more workers or torrent backends.

## Historical queue timing and throughput trial (0.4.2)

The first sweep gives a title up to four minutes of preparation/execution;
later retries allow up to 500 seconds. Metadata preparation consumes that
allowance, while queue waiting is separately capped at 240 seconds. After a
claim, the controller follows the worker lease's granted deadline instead of
subtracting queue wait a second time. The Windows decoder reserves 30
seconds for result receipt. Renewing a lease does not extend execution time.
Windows-local producers handle one title per invocation; existing sample/decode
jobs keep their original deadlines. Each remote producer service needs
`TimeoutStartSec=18min`, including the lookahead template.

Results retain safe checkpoint codes and cumulative backend-lock wait / active
processing seconds. Active time includes source transport and decoding; it does
not mean CPU utilization. OMV also reports queue times and cancellation causes.

The authorized capacity trial starts with a timing-fixed 8-worker/2-backend
30-minute baseline, then sets `config/workers.json` to
`{"workers":8,"torrservers":4}` and runs `02-START-ALL.cmd`. Four backends require
at least 14 GiB Docker VM memory and 4 GiB available Windows memory. Each backend
retains its exclusive speed-test lock; four backends permit four simultaneous
speed tests. Local probes and decoded samples can overlap. This does not guarantee a particular title rate.
The launcher default remains 8/2; an existing topology is preserved on update.
Compare final passes/failures, checkpoints, cancellations and lock waits, then
observe a promising configuration for an hour. Revert the JSON to 8/2 and Start
if memory/backend stability or playback regresses. No data folders are removed.

## Exact slow-source fallback (0.4.5)

When a Windows V6 speed test rejects a Torrentio candidate, the worker now
returns that candidate's hash as a diagnostic hint. Debian accepts the hint
only if it matches a Torrentio candidate in that exact leased job. On the next
background attempt, Debian skips only that rejected hash and tries newly
matched MediaFusion fallback candidates before older cached primaries. The
hint does not change playback selection, source exclusions, V6 evidence, or
provider pacing. If an older client omits the hint, server validation remains
unchanged and the next attempt cannot skip an exact source.

## Child execution budget (0.4.4)

Metadata preparation now receives the same child deadline as probe, speed and
sample work. Expiry produces a structured local_work_budget checkpoint with
its current phase before the parent deadline, preserving the existing receipt
margin. V6 proof requirements, candidate alternatives and retry policy are unchanged.

## Runtime permissions and durable diagnostics (0.4.10 candidate)

The image normalizes copied Python sources to mode 0644 before dropping to user 10001. The publication gate builds with deliberately restrictive source modes and tests imports as that user; package creation repeats the smoke check on the actual release image. Local import/permission faults become acknowledged infrastructure checkpoints, rather than misleading connection retries. Transport retries preserve their current task/result and log fixed request/endpoint labels without addresses or exception text.

The single diagnostics exporter runs every 30 seconds with bounded batches. Queue and first-sweep completion transactions retain private outbox receipts until the separate diagnostic ledger commits them. It preserves every receipt ID, cumulative reason counts and the latest outcome for the sweep lifetime, plus three detailed attempts per title per scope. First-sweep completions, prerequisite waits and Windows operations remain separate. Normal queue-history cleanup cannot delete unexported receipts. No raw source URL, credentials, exception text or decoder stderr enters the ledger or OMV. A later pass replaces the latest failure bucket without deleting historical counts.

Capacity remains eight Windows workers and four backends during this stabilization round. Preserve V6 criteria and provider pacing; specialized difficult-source recovery remains after the first sweep.

The diagnostic exporter is a local service with no capabilities or network access and write access limited to the two private runtime database directories. It runs as root to access the existing private catalogue directory without changing that directory's ownership or permissions. The queue remains owned by its coordinator account; initialization does not rewrite an already-correct shared mode.

Infrastructure startup faults, controller expiry, database faults and viewing interruptions retain an untouched first-sweep item in its queue with a short scheduling cooldown. They do not finish its first attempt or alter an accepted proof. Actual source rejections remain recorded/deferred under the existing sweep policy.

## Stay on the reachable coordinator path (0.4.10 candidate)

Each worker initially tries its configured LAN origin. If that connection fails, it tries the configured fallback. After a pinned connection succeeds, later requests keep using that path until a network/connection failure, then try the other path. There are no periodic LAN probes while Tailscale works. Turning off Tailscale after LAN is repaired triggers the reverse switch. A worker restart begins with LAN again. The original origin identity, worker data, tokens and TLS certificate pin remain unchanged; no new configuration or enrollment is required.

A request failure retains the task/result for the existing retry path; requests are not silently replayed. HTTP authentication/protocol errors do not trigger a network-path switch. If both origins fail, the existing 15-second retry wait prevents a busy loop. Local status.json records coordinator_endpoint and the console logs a bounded primary/fallback label when it changes, without an address.

## Request progress and initial-sweep execution (0.4.10 candidate)

Windows probe/decode reads use an ephemeral loopback proxy scoped to each exact file request. Only newly delivered byte ranges reset the 45-second no-progress watchdog; duplicate ranges and other readers do not. Cancelling an operation closes its own upstream read and subprocess. V6 speed measurements continue on the direct path. Initial-sweep jobs carry an optional bounded execution policy: 240 seconds after claim plus a separate 30-second receipt margin, independent of provider discovery and queue waiting. Jobs without that policy retain legacy behavior. No V6 sample, speed, audio or identity requirements are relaxed. Specific checkpoint codes survive preparation wrappers; configured execution limits and measured elapsed time remain distinct.

The 0.4.10 full-fleet rollout requires the image fixtures and repository gate first. Retain each lane's known 0.4.4 container for rollback; observe all eight candidate workers together rather than waiting an hour per lane. Ordinary source rejections remain sweep outcomes; infrastructure, acknowledgement, process or resource regressions require rollback and investigation.

## Diagnostic evidence in 0.4.13

Optional `diagnostic.root_cause` and `diagnostic.source_failures` retain up to 20
structured, deduplicated source failures. Assigned hashes are validated against
the server-held job. No exception messages, URLs or FFmpeg stderr are reported.
Existing operational diagnostic fields remain unchanged and still control retry
and fallback behavior. OMV shows separate root-cause counts; old clients remain
compatible but cannot supply this additional evidence. Package availability
does not establish physical installation: confirm the reported client version.


## Verified TorrServer settings bootstrap

The launcher lets the engine create its complete defaults, then uses its settings API to persist the owned overrides in the supported `BitTorr` record. It checks effective and persisted settings before resuming workers. The earlier flat settings layout was ignored by the engine.

Fresh backends initialize automatically. A legacy launcher file is migrated only when its flat fields match the known generated signature and its nested/effective owned fields still match the shipped defaults. Already-correct settings are idempotent; valid custom nested settings are preserved. Existing unrecognized flat or malformed files fail preflight before workers are drained. Only consumers of affected backends are drained/stopped; their containers, data and identities are retained. A backend image replacement also drains its consumers.

Before applying, the helper saves the original file and full effective settings as `settings.before-bootstrap-v1.*.json` in that backend's private config directory. Unrelated fields are preserved. Readback failure attempts to restore the complete prior effective settings and leaves a `settings.bootstrap-v1.pending.json` recovery marker; affected workers stay stopped and subsequent starts fail preflight until an administrator reviews the backups and removes the marker after confirming recovery. Do not delete the marker merely to bypass an unresolved settings failure. Backups may contain private configuration and must not enter public source control.

Keep `torrserver_settings.py` alongside `worker.ps1`; no worker or TorrServer image upgrade is needed for this package. Tests exercise fresh, legacy, custom, idempotent and rollback behavior, plus actual-engine API readback. This repair does not establish the cause of unavailable torrents or authorize Comet integration.
