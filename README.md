# Home media server

Project backup for a Debian 13 / OpenMediaVault home media server running
Jellyfin, a custom catalogue bridge, Transmission and TorrServer through Docker.

This public repository contains project code, deployment configuration, custom
plugin source, installed plugin artifacts and recovery instructions. It does
**not** contain passwords, API keys, private SSH keys, Jellyfin accounts/watch
history, runtime databases, media files or downloaded subtitles. It is not a
full disk image or a complete application-data backup.

## Current features

- Movies and series catalogue with paging beyond the original 1,000/200 limits.
- Hebrew title lookup, related English franchise search and single/range year search.
- Newest production year first, then real release date by default; selected score/name/runtime sorting within years.
- Month searches such as `2026-07` and `2026-07..2026-09` where real dates are known.
- Online libraries named סרטים צפייה ישירה and סדרות צפייה ישירה.
- Wizdom Hebrew subtitles with English fallback where available. Green HE means
  confirmed Hebrew, red EN means confirmed English, gray ? means unknown.
- Direct AAC-LC playback or native audio-only HLS conversion with unchanged
  video, for supported torrent streams and saved files. Verified conversion
  sources are eligible; missing torrents remain hidden for retry.
- Torrent streaming and an explicit download-to-server option. Completed owned
  downloads move into the existing movies/shows storage and are preferred on
  the original title page; managed retention removes them after viewing or age.
- Four catalogue syncs per day, mount-aware startup and enabled background timers.
- OMV Diagnostics > Media workers: live schedules, health, current check and queue counters.
- Persistent video validation and failed-title retry queue; only validated sources
  or saved copies appear in the online watch catalogue. Backoff reduces repeated
  requests; checks pause between probes during playback and resume after reboot.

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
| `artifacts/` | Exact installed Dynamic Library DLL/meta and Wizdom 0.2.0.0 package |
| `backup/export_project.py` | Allowlisted export and secret checks for future snapshots |
| `MANIFEST.json` | SHA-256 checksums and snapshot creation time |
| `RESTORE.md` | Recovery sequence and information that must be supplied separately |

The authoritative current catalogue behavior is described in
[the catalogue README](deploy/media-stack/catalogue/README.md). The original
stack README is retained as a historical setup document; some early manual-only
instructions and library names there predate the integrated catalogue.

Jellyfin is pinned to 10.11.11; the modified Dynamic Library is 1.4.0.11.
Upstream Dynamic Library source retains its MIT license. Wizdom is included as
the original provider package; its licensing remains with its authors.

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

For a complete recovery, maintain a separate encrypted off-server backup of
credentials and application data. Never commit that unencrypted data here.
