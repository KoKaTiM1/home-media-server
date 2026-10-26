# Home media server

Project backup for a Debian 13 / OpenMediaVault home media server running
Jellyfin, a custom catalogue bridge, Transmission and TorrServer through Docker.

This public repository contains project code, deployment configuration, custom
plugin source, public artifact metadata and recovery instructions. It does
**not** contain passwords, API keys, private SSH keys, Jellyfin accounts/watch
history, runtime databases, media files or downloaded subtitles. It is not a
full disk image or a complete application-data backup. Private server addresses,
storage/library IDs and account-specific values are replaced by deployment
placeholders. Compiled assemblies and archives are excluded as well.

Before deploying, copy `deployment-values.example.json` to a private location
outside this checkout, fill in your own values, and render the required files:

```sh
python3 backup/render_deployment.py --values /private/deployment-values.json
```

See `RESTORE.md` for the staged setup order. Keep credentials in private runtime
secret files. Never commit rendered, machine-specific deployment files.
For later sanitized exports, keep the private deployment-value mapping at
`/opt/media-stack/secrets/export-redactions.json` with mode `0600`. The exporter
uses it only to replace values in the public copy; it never changes live files.
Public upstream software/API URLs and generic localhost bindings remain so the
source can be rebuilt. Authentication keys are supplied through runtime files.

## Follow-up after verification

[Issue #1](../../issues/1) tracks the
embedded Hebrew subtitle extraction defect and the playback/blank-series
checks to repeat after the initial verification audit. Findings and closure
criteria are kept in the issue.

## Hardware currently running this stack

Measured on the source server on 2026-10-25; capacities are rounded. These are
reference specs for the existing machine, not minimum requirements.

| Component | Installed hardware / allocation |
| --- | --- |
| CPU | Intel Core i3-2120 @ 3.30 GHz, 2 cores / 4 threads, x86-64 |
| RAM | 2 GB installed; Linux reports 1,952 MiB usable |
| Swap | 4.0 GB decimal / approximately 3.73 GiB on disk |
| Disk | Western Digital WD5000AZRX-00A8LB0, 500 GB SATA rotating hard drive |
| OS partition | EXT4, approximately 50 GB decimal / 46.6 GiB |
| Media partition | Existing `/dev/sda5`, EXT4, approximately 445.6 GB decimal / 415 GiB raw; `df` reports 408 GiB filesystem capacity |
| Operating system | Debian 13; OpenMediaVault 8 |

The media partition holds AppData, Downloads, Media, catalogue databases and
streaming caches. Free space changes during normal use; the download queue
reserves at least 20 GiB. The stack supports one distinct video stream at a
time, favors H.264 720p/1080p, and uses video copy with AAC-LC audio conversion
where needed. Video transcoding is not the catalogue playback policy.
Container and worker resource limits keep background work bounded on this PC;
initial verification of the large metadata pool takes time.

No disk serial numbers, network addresses or machine identifiers are included
in this hardware overview.

## Current features — deployed 2026-10-26 (Dynamic Library 1.4.0.19)

- Separate online movies/shows and local downloaded Movies/Shows libraries.
  The owner's order puts online movies/shows first, then local Movies/Shows.
- Paged movie/show browsing with no fixed total-title cap. The visible library
  grows as background validation succeeds; saved results persist across syncs.
  Pagination controls how many entries load per request.
- Hebrew title lookup, related English franchise search and single/range year search.
- Background recovery of existing IMDb-named blank series from cached metadata,
  with targeted directory discovery and preserved watch/favorite state. Current
  IMDb movie names also match sources when remote metadata keeps a working title.
- Newest production year first, then real release date by default; selected score/name/runtime sorting within years.
- Month searches such as `2026-07` and `2026-07..2026-09` where real dates are known.
- Online libraries named סרטים צפייה ישירה and סדרות צפייה ישירה.
- YouTube trailer links where metadata supplies them. TV playback uses the
  native trailer control; embedded trailers within the detail page are not added.
- Up to two distinct Hebrew subtitle choices, עברית 1 / עברית 2, with the best
  available release match selected first. Wizdom provides free Hebrew subtitles
  with English fallback where available. Green HE means
  confirmed Hebrew, red EN means confirmed English, gray ? means unknown.
- Direct AAC-LC playback or native audio-only HLS conversion with unchanged
  video, for supported torrent streams and saved files. Verified conversion
  sources are eligible; missing torrents remain hidden for retry.
- Stronger v5 verification decodes six beginning seconds and a rotating two-second
  later sample, including the selected audio. Supported other audio is tested
  through AAC-LC stereo conversion with video copy. Zero-frame successes fail.
- Saved working torrents stay first. Relevant new sources rank by reported
  seeders/peers; actual file progress and decoded samples determine success.
  Exact torrent/file bindings carry through native playback and HLS.
- Slow useful downloads continue; 45 seconds without useful progress triggers
  fallback. Request/work limits save checkpoints for background continuation.
  Initial audits preserve visibility until fresh failures; daily successful-source
  audits follow. OMV shows progress, audit outcomes and direct/converted counts.
- Torrent streaming and an explicit download-to-server option. Completed owned
  downloads move into the existing movies/shows storage and are preferred on
  the original title page; managed retention removes them after viewing or age.
- Native watched checks for movies/episodes; online whole-show completion also
  counts known released episodes awaiting indexing. Enable the TV app's Watched
  indicator preference locally. No inferred progress overwrites watch history.
- OMV phone tables use labelled cards with borders and an optional table view;
  desktop layout remains native.
- Delete on online titles reports them for fresh verification without removing
  metadata, saved files or watch history. Reported sources are hidden and
  prioritized; fresh passes return them to the catalogue. Native local-file
  deletion keeps its existing behavior.
- Timed per-title viewing holds let other verification work continue while a
  selected title is excluded from audits/retries and managed saved-file cleanup.
  Holds preserve existing readiness and native progress/favorites, persist across
  reboot and expire automatically without restarting playback services.
- Unavailable favorites stay visible without losing hearts or watched state.
  Normal online browsing continues to hide unavailable sources.
- Shared 27-second foreground preparation budget, a 30-second plugin HTTP
  timeout and a separate 15-second initial stream-data budget. Failed playback
  returns an explicit error; unfinished preparation resumes in background and the viewer can retry Play.
- Details-page source preparation: one job, up to 12 seconds, suppressed during
  viewing, with successful results reused for five minutes. Failed speculative
  preparation keeps existing verification. Slow sources can still delay Play.
- Resource-bounded verification: one FFmpeg/FFprobe job across worker and HTTP
  processes, single-thread decoding/filtering and small decoded checksum frames
  keep HD checks within the bridge memory limit. Original playback resolution
  stays unchanged; resource interruptions checkpoint instead of rejecting media.
- Local playback diagnostics identify phase, elapsed time and exception type;
  private links, magnets and credentials are excluded.
- Faster shared initial loading: a compact persistent cache of verified-title
  metadata, direct indexed lookups and reusable local posters with image cache
  validation. Watched/favorite data remains specific to the current user.
- Heavy validation and counter scans yield during navigation, using a 30-second
  lease renewed by metadata requests. Images and maintenance calls do not renew it.
- Four catalogue syncs per day, mount-aware startup and enabled background timers.
- OMV Diagnostics > Media workers: live schedules, health, current check and queue counters.
- Persistent video validation and retained failed-title retry queue. Ordinary
  online browsing/search shows validated sources or saved copies; Favorites
  preserves saved titles even when unavailable. Failures retry with backoff
  from six hours up to 30 days; checks yield during probes when playback or navigation starts.

Torrent listings, metadata and subtitle matches are not guarantees of successful
playback or exact subtitle timing. Native TV uses its existing search controls;
its result limits and lack of a year picker remain. Bulk metadata lacks critic
scores/age certificates. The owner confirmed Chromecast playback including
AAC Main converted to AAC-LC. A full system reboot verified automatic startup;
replacement hardware and future software updates still require their own checks.

## Layout

| Path | Contents |
| --- | --- |
| `deploy/media-stack/` | Docker Compose, bridge, cleanup/configuration tools and tests |
| `deploy/systemd/` | Startup unit, storage dependency, services and timers |
| `deploy/omv-sharedfolders.json` | Reference export of supported OMV shared-folder configuration |
| `plugin-source/dynamic-library/` | Modified MIT-licensed Dynamic Library source |
| `artifacts/` | Public plugin metadata and local build/install instructions |
| `backup/export_project.py` | Allowlisted export and secret checks for future snapshots |
| `MANIFEST.json` | SHA-256 checksums and snapshot creation time |
| `RESTORE.md` | Recovery sequence and information that must be supplied separately |

The authoritative current catalogue behavior is described in
[the catalogue README](deploy/media-stack/catalogue/README.md).
[The stack README](deploy/media-stack/README.md) describes current storage,
service access and administration; [RESTORE.md](RESTORE.md) covers rebuilding.
The [worker-monitor guide](deploy/media-stack/omv-monitor/README.md) and
[phone-layout guide](deploy/media-stack/omv-mobile/README.md) describe the installed
OMV extensions. `RESTORE.md` includes all seven worker schedules and startup steps.

Jellyfin is pinned to 10.11.11; the modified Dynamic Library is 1.4.0.19.
The modified plugin must be built from this repository's configured source;
installing an upstream release does not include these server-specific features.
Its [plugin README](plugin-source/dynamic-library/README.md) describes this fork;
the original upstream guide is separately labelled reference material. The MIT
license is retained. Wizdom is installed separately from its publisher or plugin
catalogue; its licensing remains with its authors. No provider ZIP is included.

## Snapshot updates

On the configured source server, create a new snapshot directory:

```sh
python3 /opt/media-stack/backup/export_project.py /root/home-media-snapshot-NEW
python3 /opt/media-stack/backup/export_project.py --verify-only /root/home-media-snapshot-NEW
```

Commit the reviewed snapshot using the repository's dedicated SSH deploy key.
The private key stays outside this repository. `<READ_ONLY_COLLABORATOR>` can remain read-only;
repository owner `<GITHUB_OWNER>` authorizes the server deploy key. Pushes must preserve
remote history and never force-replace unrelated changes. For the configured checkout, update and verify the remote backup with:

```sh
python3 /opt/media-stack/backup/push_project.py
```

This stops if the checkout is dirty, the remote differs, or the remote history
cannot be fast-forwarded. No unattended push schedule is enabled.

GitHub currently provides software/configuration recovery, not recovery of
excluded application state. No automated encrypted off-server runtime backup
is configured. Without one, a lost disk means recreating accounts, credentials
and library configuration, rebuilding metadata/verification, and downloading
media again. For state recovery, maintain a separate encrypted off-server
backup; never commit that unencrypted data here.

## Optional Windows metadata helper — LAN connection

A [Windows Docker helper](deploy/media-stack/metadata-worker/README.md) has been
prepared for offline installation from USB; the owner confirmed its demo passed
on Windows. A restricted HTTPS receiver, durable shared leases and 30-second
server importer are installed. The PC uses a private scoped connection file;
metadata receipts and results survive interruptions. Windows handles series
prefetch while server background fetches share ownership/provider budgets and
retain an offline fallback. OMV lists the receiver/importer and metadata counts.
Follow [issue #2](../../issues/2) for the first physical Windows upload batch and
throughput measurement. [Receiver operation/recovery](deploy/media-stack/metadata-worker/server/README.md)
describes code installation and private credential regeneration.
Metadata enrichment does not mark torrent sources playable or complete the
initial verification audit. The source/protocol/launchers are backed up here;
the USB's compiled image archive and all live credentials/state are excluded.
