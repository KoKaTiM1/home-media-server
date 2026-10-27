# Optional Windows media verification worker

The always-on server owns the validation queue, chooses exact sources and commits
version-5 playback proofs. The optional Windows Linux-container worker performs
real FFprobe/FFmpeg probes, beginning/later sample decoding and fresh AAC-LC sample
conversion. It handles movies and episode keys through the same scheduler as the
server. The older metadata helper remains a separate component.

The server and Windows dispatcher each reserve a key with a renewable durable
claim before preparation. Both schedulers exclude live claims; expired claims
return to the server. Existing per-title/per-torrent locks remain in use. A
Windows dispatcher orchestrates the normal server pipeline while its media
recipes run on the PC. Only the trusted server can mark a title verified, choose
a fallback, update retries or import a playable item.

## Windows operation

Copy the prepared archive to a local directory outside a Git checkout, extract
it, start Docker Desktop's Linux engine and run `01-INSTALL.cmd`. Deliver the
private connection JSON separately through authenticated SCP into
`config/verification.local.json`, then run `04-START.cmd`.

The start window follows live logs. Ctrl+C closes that view while Docker keeps
working; `05-STATUS.cmd` shows saved counters, `06-LOGS.cmd` follows logs and
`07-STOP.cmd` stops the helper. Keep `data/` across restarts to preserve its
identity and durable unacknowledged result. There is no Windows auto-start;
start Docker Desktop and the launcher when assistance is wanted. The server
continues when Windows is absent.

For the 0.2.0 progress update, extract the new package over the existing local
folder without deleting `config/` or `data/`. Run `01-INSTALL.cmd`, then
`07-STOP.cmd` and `04-START.cmd`. The new image retains the same worker identity
and pending-result database. If a title is active, stop it after the viewing
session; the server verification worker remains independent.

Version 0.2.1 fixes a recovery loop after a long server outage. The gateway
can acknowledge an expired result as stale; the worker archives it locally and
requests new work. Older versions stop progressing when the local stale-result
archive reaches 64 files. Update the image over the existing folder and retain
`config/` and `data/` so the worker identity and pending result survive.

Additional Windows lanes use the same installed image and private
connection file, each with a separate `data-N/` identity and container. The
server supports up to four Windows jobs, but the live resource gate is raised in
stages: review three active lanes before enabling a fourth. Connected containers are
consumers of a shared queue; four connected containers do not mean four active
decode slots. The guard independently caps simultaneous leases.
Never copy a `data/` folder between lanes; each identity owns its checkpoint and
pending result. The launchers are `08-START-SECOND.cmd`, `11-START-THIRD.cmd`,
and `14-START-FOURTH.cmd`, with matching stop/status commands. The first lane
keeps the original `data/` folder and commands.
Newly created containers use Docker's `unless-stopped` policy: after Docker
Desktop starts following an outage, a previously running worker resumes using
its saved result. A deliberate `07-STOP.cmd` or `09-STOP-SECOND.cmd` keeps that
lane stopped. Existing containers retain their old restart policy until they
are recreated or changed with `docker update --restart unless-stopped`.

A probe or sample is one operation; it is not one fully verified title. OMV has
separate counts for Windows titles passed/unavailable/checkpointed, active
operations and heartbeat. Metadata counts and the initial audit are separate
from the large validation backlog. The owner decides when to turn Windows off;
there is no automatic stop at a remaining-title threshold.

OMV's Media workers **Progress overview** uses a cached catalogue snapshot for
final v5 passes, remaining source keys, retries and initial-audit progress.
Live queue state shows the title, batch position, probe/beginning/later sample,
elapsed time and useful source bytes. Version 0.2.0 also reports fixed decoder
phases on lease renewals every 10 seconds and a small fixed reason category for
checkpoints or decoder rejections. The earlier PC image remains compatible but
only the server-inferred operation stage appears until it is updated. No raw
FFmpeg output, signed source URL or unrestricted error text reaches OMV.

## Queue and progress update (0.2.2)

The server can stage up to two operations per active lane. The private guard
independently caps active leases (three, then four during staged pilots). Extra
producers resolve independent
titles ahead of time; a queued operation retains its server controller, exact
source binding, deadline and exclusive title claim. No whole-title batch is
handed to a PC. Each container runs one operation at a time. Expired leases are
fenced and the server resumes the title later; a late result cannot overwrite a
new lease. The existing progress/foreground/resource guards remain in force.

Claims can wait up to 15 seconds on the gateway, checking for new ready work
within that period. Queue waiting does not consume the source download stall
clock. The 0.2.2 client immediately asks again after an enabled empty claim.
Repeated identical idle messages are logged at most once per minute; local
status continues updating. Messages distinguish source preparation, a busy
decode slot, no ready operation and a paused resource gate. Acknowledged
operations are explicitly separate from the server's final title totals.

To update, extract the private 0.2.2 package over the existing folder, run
`01-INSTALL.cmd`, then `17-UPDATE-RUNNING.cmd`. The latter restarts only verifier
containers that were running, retaining all `data/`, `data-N/` and `config/`
folders and leaving stopped lanes stopped. Older clients remain compatible;
new console messages require the new image. Physical Windows installation must
be checked separately from server-side deployment.

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
