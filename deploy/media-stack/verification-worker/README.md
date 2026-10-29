# Optional Windows verification worker

Windows can run complete V6 source checks with its own private TorrServer; the
Debian server still owns candidate ranking, evidence acceptance and final proof
commits. Package 0.3.1 contains worker image `home-media-verification-worker:0.3.1`
and a private, container-network-only `home-media-verification-torrserver:0.3.0`.

## Four launchers

| File | Action |
| --- | --- |
| `01-UPDATE-AND-START.cmd` | Verify/load the image, restart/start all four workers, remove old launchers after success. |
| `02-START-ALL.cmd` | Start all four without reinstalling; already-running workers are retained. |
| `03-LOGS.cmd` | Combined live logs, prefixed by worker name; Ctrl+C closes only the view. |
| `04-STOP-ALL.cmd` | Stop all four, retaining every saved result and identity. |

Extract over the existing worker folder. Preserve `config/`, `data/`, `data-2/`,
`data-3/`, `data-4/` and `torrent-state/`; each container has a separate durable identity. Run
`01-UPDATE-AND-START.cmd` once after extracting. No per-worker start/stop actions
are needed. Fresh installations need their private connection JSON under
`config/verification.local.json` before Update or Start. The optional
`fallback_server_urls` array enables a secondary HTTPS path; each address must
present the same pinned certificate. The worker tries the LAN URL first and
uses the Tailscale URL when it cannot establish a pinned TLS connection. The
durable worker identity remains tied to the primary URL and is preserved.

The updater checks the image checksum and occupied container names before
stopping workers. Cleanup targets only the exact historical launcher names,
never data/config or unrelated scripts. Failed validation leaves running workers
and old launchers intact. Other applications' containers are never adopted.
An interrupted update can be retried; retained pending results use existing
lease/generation fencing. Physical Windows installation remains a separate check
from server-side or mocked launcher tests.

## Queue and progress

Each container runs one operation or one complete title check at a time. Four connected containers do not
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

## Local torrent verification and fallback

For a complete-title job, Debian sends up to 20 ranked public info hashes and
the title identity. Windows resolves the file list locally, applies the same
file selector, probes the chosen file, measures two uncached ranges, decodes the
beginning and rotating later sample, and tests AAC-LC conversion when required.
The local torrent API is available only inside the dedicated Docker network;
it is not published on the PC or LAN. Its memory is capped at 2 GiB and its
in-memory cache starts at 16 MiB so cached bytes cannot masquerade as network
speed in the two range measurements.

Debian validates the exact hash, file index/name/length, raw sample checksums,
codec policy, range offsets, byte counts, timing and proof freshness before it
commits a result. A local service failure, disconnect, expired lease or invalid
result causes one normal Debian verification fallback. It does not create a
false unavailable title. The ordinary Debian worker continues independently
when Windows is off. A Windows speed pass describes delivery on the Windows PC;
it is intentionally not repeated on Debian and therefore does not guarantee the
same speed from the server's network path.

The initial capped rollout is complete. Old 0.2.x workers remain compatible and
receive their earlier fixed probe/sample operations. Only a 0.3.x worker that
reports a healthy local torrent backend can claim complete-title work.

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

Both hosts use `catalogue/verification_media.py` for the same fixed V6 recipes.
The server checks decoded frame/audio evidence, required AAC-LC stereo/video-copy
results, exact-source byte delivery and source binding before its ordinary proof
commit. A disconnect, expired lease, transport problem or resource limit is a
checkpoint, not a failed source. Lost replies replay the same result identity.

## Server installation and recovery

1. Build the normal catalogue image first. From the stack directory build the
   helper with `docker build -f verification-worker/Dockerfile -t
   home-media-verification-worker:0.3.1 .`. Tag the pinned TorrServer image as
   `home-media-verification-torrserver:0.3.0` and export both images privately; compiled
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

For bounded lookahead, set private guard `prefetch_per_lane` to `2` through `8`
(`1` by default). The active Windows lease limit remains `max_windows_jobs`
(currently four); total queued plus leased decode operations are capped at the
product, up to32. Queue entries are operations, not titles: a title can need a
probe and multiple sample operations. With four Windows leases, producer slots
1–16 prepare work while Windows consumes ready operations from the shared
queue. Slots17–32 are available for additional lookahead, but are currently
paused after the 32-producer experiment saturated the bridge's two-CPU quota
without keeping the ready queue filled. These producer slots do not grant any
extra Windows decoder leases.

The 1.5 GiB catalogue-bridge memory allowance is now part of the host guard:
Windows work pauses at80% of bridge working memory and resumes below70%, using
the same reclaimable-cache adjustment as TorrServer. TorrServer retains its
1.5 GiB allowance and 80/70% guard; host-available RAM and new OOM/restart
checks still apply. The bridge CPU quota is two logical CPUs on the four-thread
source host. Expand preparation concurrency in stages and measure ready work,
resource peaks and useful outcomes before claiming a speedup. Add the catalogue
`verification_jobs.py` and `verification_remote.py` read-only source mounts from
Compose when restoring; compare deployed module hashes with source.

Windows producer timers retry after three seconds (one-second timer accuracy).
The controller checks leased operation results every 250 ms and queued work
every 500 ms; it does not insert a separate two-second pause between remote
titles. These remove scheduling delays, not torrent startup or download time.
The four active decoder/thirty-two maximum total queued-work limits, exclusive source/title
leases, provider backoff and playback/resource guards still apply. Existing
producer processes finish their current batch before loading updated code.

The former 256/320 MiB TorrServer limits predate the host's 16 GiB RAM upgrade.
Issue #3's OOM mitigation is closed. The current 1.5 GiB allowance belongs to
the expanded source-preparation observation in issue #4 and was applied live
after measuring memory pressure; no playback container restart was needed.
Functional Windows validation/recovery has passed. The current throughput
optimization still needs observed useful-work and resource results before
advertising a speedup.

The physical PC completed a probe of a local media canary after the gateway's
Docker-network allow rule was corrected. It then committed v5 proofs from real
selected torrents, with beginning and later samples and fresh AAC-LC conversion
when required. The first 20-title pilot completed with 19 verified and one
unavailable outcome, without a new TorrServer OOM or restart. Continuous PC
assistance was enabled after that review; the existing memory and playback
guards still pause it as needed. A physical Windows stop/reconnect and longer
throughput review remain open in issue #4.

The current bounded supply pilot retains four active decoders and a32-operation
queue, with producers1–16 active (ordinary producers1–3 plus lookahead4–16).
Lookahead17–32 remains paused until source metadata resolution can reliably fill
the ready queue without overloading the bridge. Existing playback and resource
guards apply; measure useful outcomes and queue starvation before increasing.
