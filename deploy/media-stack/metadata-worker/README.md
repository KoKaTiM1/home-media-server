# Windows metadata helper 0.1.0

Copy this package from the USB to a local Windows directory, open Docker Desktop
in Linux container mode, then run `01-INSTALL.cmd` and `02-OFFLINE-DEMO.cmd`.
The included image is Linux/amd64, matching the i7-6700 PC. Installation loads a
local image archive and does not need to fetch an image or Python dependencies.

The offline demonstration simulates a failed acknowledgement and a restart.
It checks that the fetched metadata survives and is submitted once with the
same result identity. Docker runs it with networking disabled.

The production LAN coordinator and importer are installed. The owner confirmed
that installation and the offline demo passed on Windows. Copy the private
connection file from the server user's home into `config/config.local.json`,
then run `04-START.cmd`. The USB delivered the client; assignments and results
travel over the LAN. See [server/README.md](server/README.md) for enrollment,
server operation, safety boundaries and recovery. The first physical PC batch was confirmed: 67 records received and applied,
about 16 results/minute. A corrected first-start container check is available
in the current worker.ps1; update that local launcher for fresh installations.

## Commands

| File | Purpose |
| --- | --- |
| `01-INSTALL.cmd` | Check and load the included image archive |
| `02-OFFLINE-DEMO.cmd` | Run the isolated, network-disabled demonstration |
| `03-CONFIGURE.cmd` | Save coordinator address, certificate pin and scoped worker token locally |
| `04-START.cmd` | Start the metadata-only worker in Docker |
| `05-STATUS.cmd` | Read local pending/completed counters and container state |
| `06-LOGS.cmd` | Show the last 60 worker messages |
| `07-STOP.cmd` | Stop the worker without removing pending metadata |

Scripts invoke PowerShell for this process only; they do not change the system
execution policy, firewall, SSH configuration or startup settings. Docker state
uses the fixed name `home-media-metadata-worker`. The worker uses two CPU units,
192 MiB memory and one provider request at a time, at most one every three
seconds (slower if the coordinator or provider asks). Stop before editing its
connection or moving the folder. Keep the PC awake while a batch runs.

If image import fails, first check Docker Desktop is running in Linux mode and
that all package files were copied together. If Docker is unavailable to your
Windows user, finish its local setup before using these scripts.

## Scope and durability

The helper fetches full title and episode metadata from the existing Cinemeta
provider for assigned IMDb identities. It does not build or replace the server's
basic IMDb index, download artwork/media, validate torrent sources, perform
audio conversion, write Jellyfin data or receive arbitrary executable commands.
Unavailable metadata does not mean unavailable playback.

Only the server coordinator can lease tasks and import results. Every request
requires a dedicated token. HTTPS validates the server before sending that
token; a trusted self-signed certificate can use its exact SHA256 fingerprint.
HTTP is refused in production. Coordinator redirects are refused. Provider
redirects are limited to the two current Cinemeta HTTPS hosts and the exact
assigned title path; other hosts, ports, paths, queries and credentials fail.

Local SQLite stores assignments and a durable outbox. Results are removed from
the outbox after a matching accepted/duplicate acknowledgement. Lost replies
reuse the same result ID, including after restart. Expired/rejected results are
retained separately for review. A local lock rejects a second worker sharing
the same state folder. Logs are bounded, raw provider responses and credentials
are not logged, and the worker stops at a 384 MiB local-state threshold. Review
and free quarantine items only after reconciling them with the server.

The image runs as user 10001 with no Linux capabilities, a read-only root
filesystem and no published ports. Its only writable bind mount is `data`.
`config` is mounted read-only. Keep the whole working directory on a local
drive; do not share its SQLite state across machines.

Token revocation on the future coordinator stops future access. To retire this
PC, stop its container, reconcile pending outputs, then remove its local token.
Keep unacknowledged data until import is confirmed. No Windows inbound SSH
server is required for this outbound connection.

## Source and validation

`worker/worker.py`, `worker/demo.py`, `worker.ps1` and `Dockerfile` are included.
The image is built from the available Python 3.13 slim base, with no additional
packages. `SHA256SUMS.json` records package file hashes for transfer checking.
The manifest is an integrity check, not a software publisher signature.
See `VALIDATION.txt` for the checks performed on the server and the remaining
physical Windows checks. The server receiver and 30-second importer are installed separately as systemd services.
