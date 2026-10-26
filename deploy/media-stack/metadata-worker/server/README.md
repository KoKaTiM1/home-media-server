# LAN coordinator and server importer

The Windows client uses the same `home-media-metadata/1` protocol as this HTTPS
receiver. Client image 0.1.0 does not need rebuilding to connect. The configured
server allows only the enrolled Windows PC and its own LAN address, requires a
dedicated token, and uses a pinned TLS certificate. No router port forwarding
or Windows inbound SSH service is needed.

## Connect the prepared PC

Copy the private `home-media-worker-config.json` file from the server user's
home directory into `config/config.local.json` in the local Windows package.
Use the existing authenticated server SSH/SFTP connection; never commit that
file or paste its token into an issue. Start Docker Desktop in Linux mode and
run `04-START.cmd`. Use `05-STATUS.cmd`, `06-LOGS.cmd`, and `07-STOP.cmd` to
inspect or stop it. Its existing data directory preserves its worker identity.

The token binds to the first Windows worker identity at its first claim. Moving
the folder with its data keeps that identity. A new data directory on another
PC requires explicit enrollment review; it cannot silently take over a lease.
Rotate the dedicated token and restart only the metadata receiver to revoke
access. The server's TLS certificate fingerprint is part of the client config.

## Runtime separation

`media-metadata-coordinator.service` runs as the existing unprivileged server
user. Its only writable server directory is the metadata queue. Public code
is installed in `/usr/local/lib/home-media-metadata`; the private stack directory
does not need to be made readable to that service user. Systemd LoadCredential
delivers the token, TLS key/certificate and private receiver configuration.

The queue directory is owned by the service user, group `users`, mode 2770.
The SQLite queue **and its WAL/SHM files must be group writable (0660)**, because
the receiver and existing container background workers use different UIDs.
MetadataJobs initializes the database with that mode before SQLite creates its
companion files. Changing only the directory permissions is insufficient.

`media-metadata-import.timer` runs every 30 seconds. Its root service invokes
`docker exec catalogue-bridge python /app/app.py metadata-import`. That existing
container can read the production catalogue and seed a bounded window of up to
100 missing/near-expiry series records, prioritizing the current audit. The
network receiver cannot write that production database. Jobs, leases, result
receipts and pending metadata reside in the separate helper database.

The importer applies at most 20 handoffs per run to the ordinary six-hour
metadata cache. Newer local cache data wins. Receipt plus immutable handoff is
one transaction; a cache commit interrupted before its applied marker is safe
to replay because expiry comparison makes it idempotent. Metadata imports do
not touch source proofs, native userdata, favorites, saved media or library
visibility. Existing verification/import rules decide when Jellyfin shows a
title and which episodes are available.

Windows fetches series metadata. Server background metadata calls share leases
and rate budgets; movies stay local. If Windows is not seen for two minutes,
the server can fetch missing series metadata itself. If a specific series is
waiting on the PC, its short metadata wait lets verification continue with
other titles. Background catalogue-page requests share the provider budget;
429/503 cooldowns apply to both machines. Urgent interactive metadata requests
remain an explicit exception so they cannot be blocked by a sleeping PC. The
already-running HTTP daemon loads its existing foreground path until a normal
permitted restart; no playback restart was needed for this integration.

## Operation and recovery

OMV Diagnostics / Media workers lists the receiver and import timer. Counters
show metadata awaiting fetch, currently leased, awaiting import, imported,
kept-newer results and the Windows worker's last-seen time. These counters are
separate from source verification and playable-title counts.

Public backup includes source, tests and systemd units. Credentials, local
connection JSON, SQLite state and compiled image archives are excluded. To
rebuild after losing the server:

1. Restore the normal stack and use the updated catalogue Dockerfile, which
   includes `metadata_jobs.py`. Prepare the storage mount first.
2. Install `coordinator.py`, `catalogue/metadata_jobs.py`, and `worker/worker.py`
   as readable files in `/usr/local/lib/home-media-metadata`. Keep the receiver
   service's user and permitted addresses aligned with the new installation.
3. Create its queue directory with the ownership/modes above. Generate a new
   random metadata-only token and TLS key/certificate. Store those privately as
   `metadata_worker_token`, `metadata_tls_key.pem`, `metadata_tls_cert.pem` under
   the stack secrets directory, readable by root only. Fill the example JSON
   privately as `metadata_coordinator.json`; its generation remains stable
   across ordinary restarts.
4. Install the exported receiver/import units. Start the receiver initially
   disabled for claims. Deploy shared background metadata logic and let old
   background writers finish before enabling assignments. The existing HTTP
   daemon may keep its documented foreground exception while viewing continues.
5. Initialize MetadataJobs with the configured queue generation and enable it.
   Write the private `metadata-helper.enabled` marker in catalogue AppData with
   `queue_path` as seen inside the bridge container and `generation`. On this
   deployment that queue is reached through the existing storage bind mount;
   an absolute host path is not automatically a valid container path.
6. Enable the receiver and import timer at boot, then run one importer cycle.
   Deliver a regenerated private Windows connection file with server URL,
   TLS fingerprint, dedicated token, kind `series`, and interval 3 seconds.
   Reenroll the Windows identity if the queue database was lost.
7. Check pinned HTTPS authentication, group access from both processes,
   receipt/import counters, a real client batch and preserved viewing state.

Stopping the helper does not require stopping Jellyfin or torrents. Retain
unacknowledged results on Windows until the server has acknowledged them.
See issue #2 for the first physical PC connection and throughput follow-up.
