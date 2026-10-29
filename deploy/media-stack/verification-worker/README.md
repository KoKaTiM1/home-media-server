# Windows V6 verification workers

The optional Windows PC verifies torrent sources using the same V6 source, speed,
video sample and audio requirements as Debian. Debian chooses candidates and
validates every proof before changing library readiness.

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

Temporary local source failures use durable title cooldowns: 5 minutes, 15 minutes,
1 hour, then 6 hours on repeated failures. Audit and discovery selectors respect
the same cooldowns. A pass or explicit new manual recheck clears the retry history.
A checkpoint never becomes a failed media proof merely because its retry is delayed.
Playback interruptions and connection outages remain scheduling interruptions.

Windows-local failures may request Debian fallback; its separate resource gate
can defer that request. Host TorrServer OOM does not disable otherwise healthy
Windows-local torrent work. Host-memory and catalogue-bridge protections still apply.

OMV Diagnostics > Media workers reports requested topology, worker concurrency,
distinct healthy torrent backends, attempts, distinct titles, V6 passes and
sanitized failure categories. Operations, retries and complete title passes are
separate measurements. No raw error text or source URLs appear in these metrics.

## Build and restore

Build the worker Dockerfile with an allowlisted context containing catalogue/*.py
and the verifier sources; exclude secrets and runtime data. package.py produces
verification-worker-v0.4.0.zip from the worker 0.4.0 image and torrent backend 0.3.0.
The archive contains exactly four CMD launchers and no enrolled data or credentials.
On restore, install the coordinator and resource gate, provision private TLS/token
configuration, then copy the package to Windows and run Update. The topology is
applied by Start; no per-lane server editing is required. Initial server rollout
retains existing capacity until the updated launcher requests a new topology.

Issue #4 tracks throughput; issue #3 tracks the host TorrServer OOM review. The
100+ distinct completed title checks/hour target requires measurement. Preserve
all V6 quality checks when evaluating more workers or torrent backends.
