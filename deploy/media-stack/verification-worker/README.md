# Optional Windows verification worker

Windows decodes fixed media samples; the server owns source selection and final
v5 proofs. Package 0.2.3 simplifies the launchers; its decoder image remains
`home-media-verification-worker:0.2.2` with the queue/progress improvements.

## Four launchers

| File | Action |
| --- | --- |
| `01-UPDATE-AND-START.cmd` | Verify/load the image, restart/start all four workers, remove old launchers after success. |
| `02-START-ALL.cmd` | Start all four without reinstalling; already-running workers are retained. |
| `03-LOGS.cmd` | Combined live logs, prefixed by worker name; Ctrl+C closes only the view. |
| `04-STOP-ALL.cmd` | Stop all four, retaining every saved result and identity. |

Extract over the existing worker folder. Preserve `config/`, `data/`, `data-2/`,
`data-3/` and `data-4/`; each container has a separate durable identity. Run
`01-UPDATE-AND-START.cmd` once after extracting. No per-worker start/stop actions
are needed. Fresh installations need their private connection JSON under
`config/verification.local.json` before Update or Start.

The updater checks the image checksum and occupied container names before
stopping workers. Cleanup targets only the exact historical launcher names,
never data/config or unrelated scripts. Failed validation leaves running workers
and old launchers intact. Other applications' containers are never adopted.
An interrupted update can be retried; retained pending results use existing
lease/generation fencing. Physical Windows installation remains a separate check
from server-side or mocked launcher tests.

## Queue and progress

Each container runs one operation at a time. Four connected containers do not
necessarily mean four active jobs: the private server resource guard sets that
limit independently, and can stage up to two operations per active lane.
Additional server producers prepare independent titles ahead of time under
exclusive title claims; whole-title batches are not handed to the PC.

Claim long polling delivers ready work promptly. Waiting for assignment does
not count as a torrent stall. Messages distinguish server preparation, busy
decode slots, no ready operation and paused gates. Identical waiting lines repeat
at most once per minute; local status keeps refreshing. Phase lines identify the
title and local acknowledged operation count. Operations are not final verified
titles; OMV shows server-validated title outcomes separately.

Workers use Docker's `unless-stopped` restart policy. After Docker Desktop
starts, previously running containers can resume without manually starting each
worker. A deliberate Stop keeps them stopped until Start/Update. Windows can be
shut down without stopping the primary server verifier.

## Restricted transport and evidence

The dedicated TLS gateway requires a separate verification token, a pinned server
certificate and an enrolled-PC address. It accepts fixed claim/renew/result
requests under `/v1/verification-worker`. It does not accept shell commands,
provider URLs or arbitrary file paths from the PC. The stream route authorizes
one live lease and proxies only its server-selected exact source. Windows does
not receive the bridge signing key, TorrServer password, Jellyfin admin key,
server filesystem access or a writable shared database.

FFmpeg reads through a loopback-only proxy inside the worker container. The
proxy validates the pinned server certificate before sending its token. Source
bytes flow over the scoped HTTPS gateway; private signed upstream URLs stay on
the server. Reader connections close on cancellation/disconnect/lease expiry.
The gateway can write only its separate operation queue, never the catalogue.

Both hosts use `catalogue/verification_media.py` for the same fixed v5 recipes.
The server checks decoded frame/audio evidence, required AAC-LC stereo/video-copy
results, exact-source byte delivery and source binding before its ordinary proof
commit. A disconnect, expired lease, transport problem or resource limit is a
checkpoint, not a failed source. Lost replies replay the same result identity.

## Server installation and recovery

1. Build the normal catalogue image first. From the stack directory build the
   helper with `docker build -f verification-worker/Dockerfile -t
   home-media-verification-worker:0.2.2 .`. Export this image privately; compiled
   image archives are not part of the public repository.
2. Install `coordinator.py`, `resource_guard.py` and the shared
   `catalogue/verification_jobs.py` / `verification_media.py` under
   `/usr/local/lib/home-media-verification` as root-owned readable source.
3. Prepare an AppData `verification-worker` directory owned by the existing
   service account/group with mode 2770. Its SQLite/WAL/SHM use mode 0660.
   Generate a new dedicated verification token and TLS credentials (or reuse the
   enrolled server certificate, never a public client token). Keep live gateway
   and resource-guard JSON under the stack's private secrets directory.
4. Restore the exported coordinator/guard/remote-availability systemd units and
   fill their private storage paths, account and address allowlist. The gateway
   resolves only its configured trusted catalogue origin. When `IPAddressDeny=any`
   is used, also allow the Docker bridge subnet carrying the catalogue connection;
   Docker may route the LAN-published bridge port through that subnet. Test an
   authenticated media probe after restoring or changing the Docker network.
5. Add `verification-helper.json` to catalogue AppData containing `queue_path`
   as visible inside the container. Enable the gateway and guard timers; the
   resource gate starts unapproved. New background commands use the installed
   source; apply updated HTTP reader cleanup in a confirmed safe viewing window.
6. Investigate and approve the memory gate before allowing parallel source reads.
   Configure one server and staged Windows dispatchers, beginning with a
   bounded pilot and baseline restart/OOM counters. Enable Windows lanes one at
   a time only after checking TorrServer working memory and host headroom. The guard pauses the
   optional worker if status is unavailable, a restart/OOM occurs, host available
   RAM falls below 256 MiB, or TorrServer's cgroup working memory approaches
   its cap (80% pause / 70% resume hysteresis). The guard subtracts reclaimable
   inactive file cache from raw cgroup usage and still pauses on a new OOM or
   restart. Review the completed pilot before enabling continuous
   Windows assistance. The normal server worker remains available throughout.
7. Deliver a regenerated private PC JSON, enroll its persisted identity, verify
   real PC results and fallback on stopping the PC. Preserve protected titles,
   watch progress, favourites and downloads. Do not restart a playing service.

For bounded lookahead, set private guard `prefetch_per_lane` to `2` (default
`1`). The active lease limit remains `max_windows_jobs`; the total operation
queue limit is their product, at most eight. At three active slots, restore the
`media-remote-lookahead@4`, `@5` and `@6` timer instances alongside the ordinary
three producers. At four active slots, also restore `@7` and `@8`, for eight
producer slots in total. Keep all dispatchers gated off until the staged memory gate
and pilot are approved. Producer slots are not extra decoder slots. Add the
catalogue `verification_jobs.py` and `verification_remote.py` read-only source
mounts from Compose when restoring; compare deployed module hashes with source.

Windows producer timers retry after three seconds (one-second timer accuracy).
The controller checks leased operation results every 250 ms and queued work
every 500 ms; it does not insert a separate two-second pause between remote
titles. These remove scheduling delays, not torrent startup or download time.
The four active decoder/eight total queued-work limits, exclusive source/title
leases, provider backoff and playback/resource guards still apply. Existing
producer processes finish their current batch before loading updated code.

TorrServer's limit was increased live from 256 to 320 MiB after checking host
headroom; no TorrServer restart was needed. More physical RAM provides headroom
but does not itself change Docker limits or establish that reader/heap problems
are fixed. Root-cause investigation remains tracked in issue #3; the Windows
verification rollout is issue #4. Production PC validation remains required
before advertising a speedup.

The physical PC completed a probe of a local media canary after the gateway's
Docker-network allow rule was corrected. It then committed v5 proofs from real
selected torrents, with beginning and later samples and fresh AAC-LC conversion
when required. The first 20-title pilot completed with 19 verified and one
unavailable outcome, without a new TorrServer OOM or restart. Continuous PC
assistance was enabled after that review; the existing memory and playback
guards still pause it as needed. A physical Windows stop/reconnect and longer
throughput review remain open in issue #4.
