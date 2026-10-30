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

## Tracked follow-up

The embedded Hebrew subtitle extraction and its playback checks are complete.
Issue [#6](../../issues/6) tracks remaining individual-title startup and TV
retry cases; issue [#9](../../issues/9) tracks Russian-audio alternatives, which
remain hidden until an eligible validated source is found.

## Deployment profile

The current deployment uses a Debian 13 / OpenMediaVault 8 host, with system
and service data on SSD storage and media on a separate EXT4 filesystem. Exact
machine models, capacities, partition sizes, filesystem identifiers and service
resource allocations are intentionally omitted from this public repository.
Actual throughput depends on the host, network, client and selected media.
Use the Windows worker guide for its configurable resource requirements.

## Current deployment — checked 2026-10-30

Jellyfin 10.11.11 runs the modified Dynamic Library plugin **1.4.0.28** and
WizdomSubs Downloader **0.2.0.0**. The features below describe current code and
deployed service behavior; client compatibility and torrent availability remain
source- and device-dependent.

- Four Jellyfin libraries are ordered with online Movies and Shows first,
  followed by downloaded Movies and Shows. Browsing is paged and has no fixed
  total-title cap; the visible set grows as source checks pass.
- Search supports Hebrew aliases/franchise matching, English keywords and
  single-year, year-range and month filters. Release-year ordering is the default.
- Online titles require an accepted source check before ordinary browsing.
  V6 verification checks exact torrent/file identity, decoded video/audio
  samples and measured source delivery speed. Existing matching decode evidence
  can be reused for the speed upgrade; old proof data is retained during the
  gradual migration.
- Playback binds to the accepted torrent file and can fall back among sources.
  Working downloads can be saved to the server's Movies/Shows folders and are
  managed by watch/age retention rules. A passed sample is not a guarantee of
  future peer availability or uninterrupted playback on every client.
- Hebrew subtitle choices and HE/EN poster badges are shown when confirmed.
  Complete embedded Hebrew tracks can be cached on demand. Optional Russian
  audio alternatives are kept hidden until a matching source/subtitle passes
  validation; no eligible alternative was available at the last check.
- Windows V6 verifier workers support configurable worker/backend counts. The
  Debian server remains authoritative for candidate selection and final proof;
  resource, playback and backend health guards remain enabled.
- OMV Diagnostics > Media workers reports V6 verification, worker schedules,
  queue health and current categorized failures. Phone and desktop layouts are
  supported.
- The Dynamic Library adds watched-state support for online titles, trailer
  links where metadata provides them, and safe fresh-check reporting for broken
  online sources. Native client settings and source availability still apply.

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

Jellyfin is pinned to 10.11.11; the modified Dynamic Library is 1.4.0.28.
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

The optional metadata helper enriches catalogue records independently from
source verification. Its restricted coordinator and importer use separate
leases and credentials; received metadata never marks a torrent source playable.
See [the helper guide](deploy/media-stack/metadata-worker/README.md) for setup
and recovery. Runtime credentials, queues and local databases remain private.

Optional Windows source verification is implemented separately from metadata
fetching. The current worker package is 0.4.3 (verifier image 0.4.2); the configurable launcher supports
1–16 workers and 1–8 private TorrServer backends. The Debian server validates all
submitted evidence. See [worker operation and recovery](deploy/media-stack/verification-worker/README.md)
for sizing, preserved-data updates and measured-throughput limits. Issue [#4](../../issues/4)
tracks ongoing capacity tuning; [#6](../../issues/6) tracks individual playback failures.

Windows verifier package 0.4.3 provides a durable initial V6 sweep and separates queue wait from execution time. It supports configurable worker/backend counts while retaining source-proof, memory, playback and data-preservation guards. See the verifier README for current requirements and the preserved-data update procedure.

## Automated regression gate

The Python publisher tests the exported snapshot before committing/pushing.
The backup checkout also has a pre-push check for the exact committed tree.
See [tests/README.md](tests/README.md) for the container runner, coverage,
private local reports, and remote-commit watcher. A failure blocks publication;
passing tests does not replace physical TV acceptance or approve a PR.
