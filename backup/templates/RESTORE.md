# Restore the project

This is a reviewed recovery guide, not an unattended disk installer. Do not
format a disk or overwrite `/etc/fstab` to restore this project. Do not enable
cleanup timers until runtime state and managed-download manifests are restored.

## Configure private deployment values first

The public files contain placeholders instead of private addresses, account
identifiers and storage/library UUIDs. Copy `deployment-values.example.json`
to a private file outside the checkout, fill in values for the destination,
then run `python3 backup/render_deployment.py --values /private/values.json`.
It replaces provided values without displaying them and leaves unfilled
placeholders for later stages. Use `--strict` after all deployment fields are
configured. A rendered checkout contains private values: never push it publicly.

Set network/storage values before using Compose. Configure a stable LAN address
through OMV or reserve the host's DHCP lease in
the router. Compose port bindings and the optional Windows worker connection
use that address; changing it after a reboot leaves services unreachable until
their configuration is updated.
On a fresh installation,
complete the Jellyfin wizard and create the intended online/local libraries
using the UI or supported API, then obtain their actual view IDs to fill the
plugin's library-ID fields. Render those source files before building the DLL.
Unused legacy-library IDs may use distinct freshly generated UUIDs. Optional
diagnostic sample IDs are needed only when running the corresponding checks.
This public backup intentionally contains no compiled DLL or third-party ZIP;
build/install them after configuring the private values. Credentials remain in
runtime secret files and do not belong in the deployment-values file.

## 1. Identify existing storage

The source server uses EXT4 on `/dev/sda5`, UUID
`<MEDIA_FILESYSTEM_UUID>`, mounted at
`/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>`.
Confirm actual mounts with `lsblk -f` and `findmnt` before changing anything.
Device names may differ after reinstalling or moving disks.

Existing OMV shared folders are AppData (`appdata/`), Downloads (`downloads/`)
and Media (`media/`). `deploy/omv-sharedfolders.json` is a reference, not a file
to blindly import into a new OMV instance: recreate/associate these through OMV's
supported configuration tools against the verified filesystem, preserving data.

Retained movie/show directories are `media/movies` and `media/shows`.
Jellyfin sees these as `/media/movies` and `/media/shows` through its read-only
`/media` bind. Online catalogue metadata is separate under `catalogue/`.

## 2. Restore confidential state separately

The repository excludes:

- `/opt/media-stack/secrets/` (Jellyfin API key, Transmission password, signing
  secret and optional OpenSubtitles credentials).
- `appdata/jellyfin/config`, which contains accounts, watch history, libraries,
  authentication state and native databases.
- `appdata/catalogue`, including managed-download ownership/manifests, aliases,
  source/subtitle caches, IMDb index and streaming metadata.
- Transmission configuration and resume/torrent state; TorrServer configuration
  and HTTP authentication settings; media and downloads themselves.

Restore these from a separate encrypted backup if available. No automated
off-server runtime backup is currently configured on the source server. If the
disk and state are lost, recreate credentials/accounts, initialize clean
application state and rebuild the metadata/validation index instead. Existing
media without its ownership manifest must not be treated as cleanup-owned.
A new signing key invalidates
old signed playback links. A fresh Jellyfin server requires the wizard and a
new API key; do not reuse a key from a different Jellyfin installation.
Copying live SQLite files is not a validated application backup. Use a controlled
stopped-service application-data snapshot or proper SQLite backups with required
associated files. Preserve media/managed-file identities together with download
manifests; do not simulate watched flags to test cleanup on real files.

## 3. Restore deployment files and plugins

1. Install Docker/Compose and an appropriate supported OMV/Debian combination
   if rebuilding the OS. Keep existing installations when recovering in place.
2. Review `deploy/media-stack/compose.yaml`, service units and Python defaults:
   they contain the original mount UUID, LAN address `<SERVER_LAN_IP>`, user/group
   mapping `1000:100` and source paths under `/opt/media-stack`. Adapt them
   consistently to the destination before deployment.
3. Copy `deploy/media-stack/` into `/opt/media-stack/`, retaining its layout.
   Place restored secrets under `/opt/media-stack/secrets` with restricted
   permissions; never add them to Git. `opensubtitles.json` may be `{}` when
   using the free Wizdom provider only.
4. Rebuild the local bridge image:

   ```sh
   docker build -t home-media-catalogue:1 /opt/media-stack/catalogue
   ```

5. Build the modified Dynamic Library version 1.4.0.19 from the configured
   source, then install its DLL and the public `meta.json` into
   `appdata/jellyfin/config/plugins/DynamicLibrary/` while Jellyfin is stopped.
   It was tested with Jellyfin 10.11.11. Install Wizdom from its publisher or
   Jellyfin's catalogue, preserving its license and readable ownership.
6. From this repository root, build the DLL locally:

   ```sh
   docker run --rm --memory 512m --cpus 1 \
     -v "$PWD/plugin-source/dynamic-library:/src" -w /src \
     mcr.microsoft.com/dotnet/sdk:9.0 \
     dotnet build Jellyfin.Plugin.DynamicLibrary/Jellyfin.Plugin.DynamicLibrary.csproj \
       -c Release -o /src/build
   ```

   Install the resulting DLL only with Jellyfin stopped. The upstream source
   license is `plugin-source/dynamic-library/LICENSE`.
7. Start the stack after verifying storage and secrets. Avoid starting cleanup
   timers before their manifests/state have been recovered and checked.
8. For a fresh Jellyfin configuration, the authenticated `catalogue/bootstrap.py`
   tool configures integration only after the wizard, installed plugins and key
   exist. Its legacy initial library names can be renamed through Jellyfin's
   supported VirtualFolders rename API/UI to סרטים צפייה ישירה and
   סדרות צפייה ישירה; keep `/catalogue/movies` and `/catalogue/shows` paths.
   Do not overwrite users' unrelated settings with copied private configuration.
   For the owner's library order, put סרטים צפייה ישירה first and
   סדרות צפייה ישירה second, local Movies third, and local Shows fourth,
   using Jellyfin's Home/library-order settings.
   This is stored in the user's `Configuration.OrderedViews`, using current
   library/view IDs; preserve all other user configuration when changing it.

The bridge uses one common `/storage` bind for media and completed downloads.
Keep that shared bind: moving a file between separate Docker bind mounts can
raise EXDEV even when the host directories reside on the same filesystem.

## 4. Restore mount-aware services and schedules

Review and copy `deploy/systemd/media-*` into `/etc/systemd/system/` and the
Docker storage drop-in into `/etc/systemd/system/docker.service.d/`. After
`systemctl daemon-reload`, confirm the actual `RequiresMountsFor` targets.
Do not restart Docker during active viewing merely to apply this dependency.

The exported unit files are the authoritative schedule definitions. Current
background services are:

| Unit | Purpose | Schedule |
| --- | --- | --- |
| `media-stack.service` | Start the four Docker containers after storage is mounted | At boot |
| `media-catalogue-sync` | Discover new catalogue titles and update metadata | 00:00/06:00/12:00/18:00 Asia/Jerusalem, with jitter |
| `media-source-availability` | Probe sources, verify audio/video, retry failures | Five minutes after boot, then one minute after each batch finishes |
| `media-saved-downloads` | Track requested downloads, move completed owned files, apply managed retention | Every minute |
| `media-catalogue-badges` | Refresh Hebrew/English subtitle badges | Hourly |
| `media-catalogue-collections` | Refresh native Jellyfin collection memberships using small pages | Every 30 minutes |
| `media-cleanup` | Apply legacy completed-download retention | Hourly |
| `media-worker-status` | Prepare the OMV dashboard's cached queue/health counters | Two minutes after boot, then one minute after completion |

Each timer above has a matching `.service` script command in `deploy/systemd`.
After copying the reviewed units and restoring private state, enable startup:

```sh
systemctl daemon-reload
systemctl enable --now media-stack.service
systemctl enable --now media-catalogue-sync.timer media-source-availability.timer \
  media-saved-downloads.timer media-catalogue-badges.timer \
  media-catalogue-collections.timer media-cleanup.timer media-worker-status.timer
systemctl list-timers 'media-*'
systemctl --failed
```

Enable retention timers only after restoring and checking their private state;
these timers intentionally remove eligible owned downloads. The source checker
pauses between probes during playback. Counters run with limited CPU/memory and
low disk priority. No login is required for enabled system services to start.

A fresh bulk IMDb index can be created with `docker compose run --rm --no-deps
catalogue-bridge imdb-sync` from `/opt/media-stack`, followed by discovery sync.
This downloads metadata, not movie files; allow time/storage for the initial build.

## 5. Verify recovery

- Verify `/dev/sda5`/actual filesystem identity, OMV shared folders and Docker
  mounts. Confirm no pending OMV changes remain unexpectedly.
- Confirm all four containers are running and Jellyfin is healthy.
- Run Python unit tests; live catalogue checks require the configured server
  address, authentication and online libraries. Do not run playback-oriented
  live checks while someone is watching.
- Check native browsing, Hebrew Spider-Man search, `2025` and `1994-2000`,
  within-year score order and green HE/red EN posters. Native TV search retains
  its app-imposed result limit and does not gain a year-picker UI.
- Test a known owned local video and one lawful torrent source independently.
  Confirm subtitle delivery and manual language selection in the real client.
- Confirm systemd units/timers and mount dependencies, then test a controlled
  reboot separately if appropriate; a healthy service alone is not reboot proof.

The source-server deployment was checked on 2026-10-25 with Dynamic Library
1.4.0.19: a zero-warning plugin build, Python feature and playback-deadline/stream
regression suites, .NET favorites/audio/indexing checks, native
Favorites queries retaining unavailable titles, explicit failed-source responses,
local HTTP206 playback and unchanged existing watched/favorite records. The
owner confirmed Chromecast AAC Main-to-LC playback. These checks describe the
source server, not a guarantee that any remote title remains available.

A full reboot on 2026-10-25 verified storage, core services, all four containers,
Tailscale and worker startup without login. Post-reboot quota and maintenance
issues were repaired and checked. Repeat verification after restoring to new
hardware; inspect each live-check script's fixtures before running it. Prior
saved diagnostic downloads may have been removed by normal retention.

## Android TV failure contracts and ordinary Play

The deployed plugin handles four reproduced server-side problems: empty
successful PlaybackInfo caused Android TV 0.19.10 to dereference a null source;
ordinary Play could bypass preparation and expose a local .strm placeholder;
Intros returned an array where QueryResult was required; renamed collections
left older clients using invalid parent IDs. Failures now carry NoCompatibleStream,
ordinary catalogue playback always resolves real bridge sources, Intros is a
QueryResult object, and known old collection IDs translate before model binding.

Live check_catalogue_filters.py and check_tv_recovery.py exercise these contracts.
The recovery test briefly injects missing/preparation-failed cache responses and
restores the original rows in finally; run only while nobody is watching. It
also verifies an actual saved open-film HTTP206 range and fallback artwork.
The owner confirmed subsequent Chromecast playback, including converted AAC
Main audio. This does not establish that every remote title works; recheck the
actual client after restoring.
Covering sort indexes, count caching and six-hour artwork negative caching
reduce catalogue delays and repeated missing-image requests.

Release dates/months: the online catalogue defaults to actual newest premiere date within each
year; search2026-07 or2026-07..2026-09 for known-date month filtering. Dates
persist in the runtime release_dates table independently of the daily IMDb
rebuild. Recent-year date enrichment follows the four daily syncs; unknown dates
are never invented and are absent from month searches. Restore this metadata
cache if a separate encrypted application backup exists, or let provider
enrichment refill it.

Newest/Date added is a release-date sort alias for the virtual online catalogue;
it does not use the shared bulk-import timestamp. Local library Date added
semantics are unchanged. Live month checks cover this alias as well as Premiere
date and default year order.

Poster navigation: the plugin keeps canonical poster tags/URLs on catalogue previews,
native details/refreshes and search. Image-cache updates preserve media/series
DTOs. Native query DTOs must be materialized before image mutations. Concurrent
artwork writes use unique temporary files, atomic replacement and readable
permissions. check_poster_navigation.py verifies navigation and show episodes.

Restore the optional OMV worker page by copying deploy/media-stack/omv-monitor
to /opt/media-stack/omv-monitor, adjusting DATA in monitor.py for the restored
partition, and running `python3 /opt/media-stack/omv-monitor/install.py` as root.
This restarts only the OMV RPC engine and regenerates its navigation/routes.
The local AAC Main movie sample and runtime queue are private media/data and
are excluded from the public project export.

After Jellyfin and its API key are restored, run
`python3 /opt/media-stack/configure-audio.py` while playback is idle. This
configures bounded native HLS buffering and segment cleanup through the API.
Audio conversion uses Jellyfin's own ffmpeg with video copy; saved originals
are retained until the existing managed watched/age cleanup removes them.
## Optional OMV phone layout

After restoring `deploy/media-stack/omv-mobile` to
`/opt/media-stack/omv-mobile`, run:

```sh
python3 /opt/media-stack/omv-mobile/install.py
```

This installs local assets and a separate nginx include for readable phone
tables. It validates and reloads nginx without modifying OMV's packaged web
files. Desktop styling remains native. Refresh Safari after installation.
Use the same command with `--remove` to disable the extension.

## Remote access and surviving a hardware failure

The stack README's Tailscale section includes sign-in, SSH and persistent
Jellyfin TCP forwarding instructions. A replacement installation must join
the owner's tailnet and receive the intended tags/access rules; do not copy
Tailscale identity into the public repository. Preserve the original LAN IP
or update the address references consistently. A different media disk also
requires updating the filesystem UUID references before startup.

If the PC fails while its media disk remains healthy, that disk can retain
the existing `appdata`, catalogue state and media. Attach and identify it
without formatting, then adapt this deployment to the replacement machine.
If the disk fails too, GitHub can rebuild the software but cannot recover
excluded private application data or media. A separate encrypted backup on
another device/storage provider is needed for that recovery. Keeping a
backup on the same disk does not protect against losing that disk.


## Favorites and timeout verification

Dynamic Library 1.4.0.19 preserves unavailable titles in explicit Favorites
queries without exposing them in normal online browse/search. Verify an existing
unavailable favorite remains visible and that Play still returns a compatible
source or explicit failure. Preserve user hearts/watch history during recovery.
The bridge shares a 27-second preparation budget; the plugin waits at most 30
seconds for that request. First stream data has a separate 15-second budget.
Use sanitized Playback phase events in bridge logs to distinguish preparation,
probe/conversion and initial-data failures. TV app retries may add client time.

## Restore startup-performance behavior

The plugin/bridge source includes compact verified-title metadata/readiness
caches, conditional poster responses, a bounded image cache and sanitized
`[HomeTiming]` request measurements. Private runtime cache tables are created
and rebuilt automatically; restore original queue/proof/download state if it
exists, rather than trying to manufacture ready flags. Do not drop native
Jellyfin tables or manually copy unrelated SQLite indexes to reproduce this.

Keep Jellyfin's `/catalogue` bind writable by its configured user/group: real
client navigation writes `.foreground-read-until` there. The bridge and host
counter helper read this bounded lease so disk-heavy work yields during startup.
Timers retain their normal schedules. Install the matching monitor helper too;
its previous counter snapshot remains available when a scan yields. Client
image caches are optional: metadata lists and local posters still work without
internet artwork access when the images are already stored.

### Playback preparation and watched indicators

Details-page source preparation runs only in the bridge: one 12-second job,
60-second repeat cooldown, no queue, fail-closed session checks, five-minute
prepared cache. It never requests a managed full-film download. Preserve the
existing one-stream limit and do not restart playback services during viewing.
Test active/paused suppression, source failure retaining verification, cache
reuse and cancellation before enabling on a restored server.

Whole-show response counts include known released episodes not indexed yet.
They do not modify native UserData. Enable Settings > Customization (or Display)
> Watched indicator > Always on each Android TV client. This TV-local setting
is not a server preference and is not restored by this software backup.

### Two Hebrew subtitle choices

Keep the bridge and Dynamic Library subtitle relay versions synchronized.
Up to two tracks share `Language=heb` but have distinct stream indexes and
content-specific `variant` query IDs. Preserve that query in the native relay.
Test that both actual Jellyfin VTT responses contain the selected track's
content, not only different menu labels. Titles begin עברית 1 / עברית 2;
None and English keep existing behavior. Older `he.srt` links remain readable,
and old prepared-cache entries rebuild on the next preparation. No provider can
guarantee a second available version or synchronization with every release.

Details-page preparation status checks use an isolated bounded HTTP budget
without their own cancellation callback. This prevents recursive self-cancellation
while retaining active/paused and unreachable-session suppression. The regression
check uses a real local HTTP session response, including paused playback.

### Online Delete as a recheck report

Keep Dynamic Library18 and the bridge report consumer together. The existing
`/catalogue` mapping must be writable by the Jellyfin UID and bridge. Pending
reports are atomic JSON events in `/catalogue/.recheck-requests/pending`; the
bridge archives committed events under `processed`. Runtime SQLite tables
`manual_rechecks` and `recheck_requests` preserve the latest result and request
idempotence. These are runtime state and remain excluded from the public backup.
No extra API key or public mutation endpoint is needed.

Enable media management in each TV's Customization settings if Delete is not
shown. Existing Jellyfin deletion permissions still apply. Online Delete is
intercepted as hide/recheck; local deletion remains native. Before exercising
DELETE after restore, verify the authenticated read-only
`/HomeCatalogue/Rechecks/Capabilities?itemId=<ONLINE_ITEM_ID>` reports
`Registered=true`, mode `online-delete-recheck`, the expected catalogue scope
and current plugin version. Never test deletion against a local movie. Confirm
virtual metadata/native records and watch/favorite state remain unchanged, then
verify the reported key becomes pending/priority1000 with no retry delay.


## Verification v5 recovery

The bridge image must include `catalogue/reliability.py` alongside `app.py` and
`features.py`; build with the supplied Dockerfile. Dynamic Library 1.4.0.19
requires the bridge's signed exact-source `PlaybackUrl` in prepared responses.
Restore these together. No new external credentials or inbound ports are needed.

The software backup recreates the audit/progress/source-choice schema. When
runtime state is lost, the discovery queue is rebuilt and sources are verified
again. When the existing media-partition runtime database is retained, completed
v5 results, failed retries, source winners/backups and work checkpoints resume.
The initial migration preserves existing visible entries until fresh failures;
it never formats storage or deletes downloaded media or watch/favorite data.

Review the Media workers page for initial-audit pending/passed/failed counts,
v5 direct/converted counts, daily audits and live useful-data progress. The
worker runs serially in eight-minute slices and defers during active/paused
viewing, navigation or unavailable session status. Do not equate an eight-minute
checkpoint or a reported peer count with a failed or successful playable source.


For optional Windows verification, restore the restricted gateway, memory guard and remote-availability units using [the helper recovery instructions](deploy/media-stack/verification-worker/README.md). Generate fresh private tokens/PC configuration and complete the bounded live pilot before allowing continuous parallel checks. Worker state and compiled images are private runtime data and are not stored here.
