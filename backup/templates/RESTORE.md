# Restore the project

This is a reviewed recovery guide, not an unattended disk installer. Do not
format a disk or overwrite `/etc/fstab` to restore this project. Do not enable
cleanup timers until runtime state and managed-download manifests are restored.

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

Restore these from a separate encrypted backup. A new signing key invalidates
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

5. With Jellyfin stopped, restore `artifacts/DynamicLibrary/` into
   `appdata/jellyfin/config/plugins/DynamicLibrary/`. The supplied DLL is
   modified version 1.4.0.7, tested with Jellyfin 10.11.11. Restore/extract the
   Wizdom package into its own plugin directory. Preserve readable ownership.
6. If rebuilding the DLL from source instead, from this repository root:

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

The bridge uses one common `/storage` bind for media and completed downloads.
Keep that shared bind: moving a file between separate Docker bind mounts can
raise EXDEV even when the host directories reside on the same filesystem.

## 4. Restore mount-aware services and schedules

Review and copy `deploy/systemd/media-*` into `/etc/systemd/system/` and the
Docker storage drop-in into `/etc/systemd/system/docker.service.d/`. After
`systemctl daemon-reload`, confirm the actual `RequiresMountsFor` targets.
Do not restart Docker during active viewing merely to apply this dependency.

Schedules: catalogue sync 00:00/06:00/12:00/18:00 Asia/Jerusalem with jitter;
source listings every 15 minutes; saved-download management every minute;
subtitle availability badges hourly; collections every 30 minutes; legacy
download cleanup hourly. Enable reviewed timers after state checks.

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

Last source validation: 26 catalogue unit tests, zero-warning .NET build,
year/range/paging/search/native HE-poster live checks passed. Existing 36 HE and
2 EN posters were rendered and pixel-verified as green/red. Cached Hebrew VTT
relay and invalid-signature checks passed. A later Breaking Bad pilot full
playback/subtitle-selection retest hit torrent timeouts; do not claim that
particular source is currently working.

## Android TV failure contracts and ordinary Play

Dynamic Library 1.4.0.7 addresses four reproduced server-side problems: empty
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
A physical Chromecast retry is still needed to confirm the app behavior.
Covering sort indexes, count caching and six-hour artwork negative caching
reduce catalogue delays and repeated missing-image requests.

Release dates/months:1.4.0.7 defaults to actual newest premiere date within each
year; search2026-07 or2026-07..2026-09 for known-date month filtering. Dates
persist in the runtime release_dates table independently of the daily IMDb
rebuild. Recent-year date enrichment follows the four daily syncs; unknown dates
are never invented and are absent from month searches. Restore this metadata
cache from the encrypted application backup or let provider enrichment refill it.

Newest/Date added is a release-date sort alias for the virtual online catalogue;
it does not use the shared bulk-import timestamp. Local library Date added
semantics are unchanged. Live month checks cover this alias as well as Premiere
date and default year order.
