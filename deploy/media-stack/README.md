# Home media server: current deployment

Debian 13 / OpenMediaVault 8 runs Jellyfin, the custom catalogue bridge,
Transmission and TorrServer through `/opt/media-stack/compose.yaml`.
Jellyfin is pinned to 10.11.11, the modified Dynamic Library plugin is
1.4.0.15, and the installed Wizdom subtitle provider is 0.2.0.0.
The initial Jellyfin wizard and catalogue integration are complete on the
source server. A fresh recovery still needs its own accounts, keys and setup.

See [catalogue/README.md](catalogue/README.md) for browsing, validation,
Hebrew subtitles, audio conversion, saved downloads and current limitations.
The public repository's root README includes measured hardware specifications;
its RESTORE.md describes rebuilding with private deployment values.

## Libraries and playback

The owner's library order is:

1. סרטים צפייה ישירה — online movies.
2. סדרות צפייה ישירה — online shows.
3. Movies — local/downloaded movie files.
4. Shows — local/downloaded episodes.
5. Collections, followed by Downloads.

Order is per-user, stored through Jellyfin's supported user configuration.
The online catalogue shows verified sources and saved copies. The large local
metadata index is a discovery pool; its size is not the visible playable count.
Unverified or failed sources stay queued for retries. Four daily discovery
syncs and the continuous background validator gradually expand the library.

Use the native source/version selector on a title to stream, request
Download to server + play, or play an existing downloaded copy. Saved files
are linked to the original title page. If an owned saved copy is removed by
retention, the same page can resolve a verified torrent again.
Online show cards use native per-user unwatched counts when available and
saved released-episode metadata for previews pending native indexing.
Show subtitle markers sit bottom-left, clear of the episode counter.

## Storage and Docker mounts

The existing media filesystem is EXT4 on `/dev/sda5`; never format it as part
of recovery. Its private deployment mount is
`/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>`.
Device names can change on another machine: verify the filesystem identity.

| Host directory relative to the media mount | Container path / use |
| --- | --- |
| `appdata/jellyfin/config` | Jellyfin `/config` |
| `appdata/jellyfin/cache` | Jellyfin `/cache`, including temporary HLS |
| `media` | Jellyfin `/media`, read-only |
| `downloads/complete` | Jellyfin `/downloads`, read-only |
| `catalogue` | Jellyfin and bridge `/catalogue` |
| Entire media filesystem | Bridge `/storage`, shared bind for atomic moves |
| `appdata/catalogue` | Bridge `/data`: persistent queue, caches and manifests |
| `appdata/transmission/config` | Transmission configuration |
| `appdata/torrserver` and `streaming-cache` | TorrServer state and working cache |

OMV shares are AppData (`appdata/`), Downloads (`downloads/`) and Media
(`media/`). Movie and show folders are `media/movies` and `media/shows`,
visible in Jellyfin as `/media/movies` and `/media/shows`.
Managed saves use `media/movies/Saved/` and `media/shows/Saved/`; staging uses
`downloads/complete/Saved/`. Preserve the bridge's common filesystem bind so
final moves do not cross separate Docker mounts.

OMV-managed mount options are `defaults,nofail,user_xattr,acl`, with no
configured user/group disk quotas. Apply mount changes through OMV's supported
configuration database and deployment tools, preserving its existing mount
and shared-folder references. Do not overwrite generated `/etc/fstab` entries.

## Retention

The saved-download watcher checks every minute. It reserves 20 GiB free,
limits queued requests to three, and selects only the requested movie/episode.
Only manifest-owned Saved files are eligible for automatic deletion: after a
new watched/completed signal, or 30 days after completion if unwatched.
Active playback, changed files and unrelated retained media are protected.
A durable move intent recovers interrupted final moves after restart.

The separate hourly legacy Downloads cleaner observes indexed completed
Transmission video under `/downloads`: watched files have a 24-hour grace
period; unwatched files expire 30 days after observed completion. It skips
active playback and removes matching owned subtitle sidecars. Its state is
under `appdata/cleanup/state.json`. Existing manually retained media is excluded.
External-player viewing does not supply Jellyfin watched status.
TorrServer's temporary streaming cache has its own drop behavior.

Cleanup intentionally deletes eligible video data. During recovery, restore
and inspect ownership manifests and cleanup state before enabling retention.
Preview legacy eligibility with `python3 /opt/media-stack/cleanup.py`.

## Background services and OMV

`media-stack.service` and Docker's storage dependency require the actual media
mount before startup. Containers use restart policies. A full reboot on
2026-10-25 verified automatic service startup without a user login; subsequent
quota and maintenance issues were repaired and checked.

| Worker timer | Schedule / purpose |
| --- | --- |
| `media-catalogue-sync` | 00:00, 06:00, 12:00, 18:00 Asia/Jerusalem, with jitter; discovery and metadata |
| `media-source-availability` | Five minutes after boot, then one minute after each batch; bounded serial source checks |
| `media-saved-downloads` | Every minute; track downloads, move owned files and apply managed retention |
| `media-catalogue-badges` | Hourly; subtitle availability markers |
| `media-catalogue-collections` | Every 30 minutes; native collection membership |
| `media-cleanup` | Hourly; legacy Downloads retention |
| `media-worker-status` | Two minutes after boot, then one minute after completion; cached OMV counters |

Source verification pauses during playback and yields to real client navigation.
A 30-second metadata-navigation lease gives initial foreground reads priority;
counter scans also yield, keeping their previous snapshot. Images/maintenance
requests do not renew the lease. The seven timer schedules remain unchanged. Its batch starts
at most 20 probes or runs up to eight minutes before starting another probe.
Failed titles are retained and retry with backoff, not rediscovered from scratch.

OMV Diagnostics > Media workers displays schedules, health, current work and
queue/pass/failure counters. See [omv-monitor/README.md](omv-monitor/README.md).
The optional phone layout uses labelled bordered cards and a table toggle;
desktop styling remains native. See [omv-mobile/README.md](omv-mobile/README.md).
Both extensions are in the project backup and have restore installers.

## Access and administration

Use the configured private LAN address for OMV on port 80, Jellyfin on 8096,
Transmission on 9091, TorrServer on 8090 and the bridge on 8092. Do not publish
these runtime addresses. Credentials remain in restricted private runtime
files under `/opt/media-stack/secrets/`; the repository excludes them.
The integrated Jellyfin workflow normally needs no manual TorrServer entry.

For remote access, connect the client to the owner's tailnet:

- OMV: `http://<TAILSCALE_IP>/`
- Jellyfin: `http://<TAILSCALE_IP>:8096/`

Find the private address using `tailscale ip -4` or the owner's admin console.
Tailnet policy must permit the intended ports; tags alone do not grant access.
Tailscale SSH additionally needs the owner's SSH policy and device tag.
Keep the normal Jellyfin login. No Tailscale Funnel is enabled.

Jellyfin retains its LAN binding. The persistent tailnet-only TCP forwarder is:

```sh
tailscale serve --bg --tcp=8096 tcp://<SERVER_LAN_IP>:8096
tailscale serve status
```

On replacement hardware, sign in to Tailscale, apply intended tags and access
rules, verify the actual LAN address, then configure forwarding. Identity/state
files are private and excluded from GitHub. To remove only this forwarding:
`tailscale serve --tcp=8096 off`.

Routine read-only checks:

```sh
docker compose -f /opt/media-stack/compose.yaml ps
systemctl list-timers 'media-*'
systemctl --failed
journalctl -u media-source-availability.service --no-pager -n 30
```

The public backup contains source, deployment templates, units, tests and
recovery instructions. It excludes runtime databases, users/watch history,
credentials, media and compiled plugins. Build the configured plugin from
source; install Wizdom separately. No automated off-server runtime backup is
configured. If the disk is lost, software can be rebuilt, but excluded state
must be recreated and media downloaded again unless separately backed up.
