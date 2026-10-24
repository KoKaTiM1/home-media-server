# Home media server

Online catalogue integration and its current limitations are documented in
`/opt/media-stack/catalogue/README.md`. Online Movies/Shows are separate from
the retained media and completed-download libraries described below.

All persistent media, downloads, application configuration, and torrent streaming
cache use `/dev/sda5`, filesystem UUID `<MEDIA_FILESYSTEM_UUID>`,
mounted at `/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>`.
Linux reports approximately 415 GiB; decimal drive sizes are larger.

## Stream a torrent

Open <http://<SERVER_LAN_IP>:8090/> (TorrServer), username `server`. It uses the same
locally stored password as Transmission:
`<SERVICE_HOME>/.config/media-stack/transmission_password`.
Add a torrent file or magnet link for media you are entitled to access, then open
the stream using a supported player, such as VLC. Browser support depends on the
video and audio codecs. TorrServer supplies an HTTP stream; it is a separate
interface from Jellyfin and is not an integrated Jellyfin torrent-search plugin.

TorrServer uses disk cache at `/streaming-cache` inside its container, backed by
the media partition. The configured buffer is 128 MiB and disconnect timeout is
60 seconds; `RemoveCacheOnDrop` is enabled. No torrent source catalogue has been
configured. Playback with an actual torrent still needs a client-side test.

## Download and watch later

Open <http://<SERVER_LAN_IP>:9091/transmission/web/>, username `server`, using the local
password file above. Completed downloads are under `downloads/complete` on the
media filesystem. Jellyfin can read that directory as `/downloads`.

Finish the Jellyfin wizard at <http://<SERVER_LAN_IP>:8096/>. Add a library for completed
downloads using `/downloads` (Mixed Movies and Shows content type if using a
single flat download area), and optional retained media from `/media`.
Jellyfin has read-only access to both paths. Automatic retention applies only to
completed Transmission video downloads indexed under `/downloads`; manually
retained `/media` content is excluded.

## Enable watched-status cleanup

1. Finish Jellyfin setup, create the owner account, and add the download library.
2. Create an API key in Jellyfin Dashboard → API Keys.
3. Save the key to `/opt/media-stack/secrets/jellyfin_api_key` on this server,
   owned by root with mode `0600`. Do not paste the key into chat or commit it.
4. Run `python3 /opt/media-stack/cleanup.py` to preview eligibility.
5. Run `systemctl start media-cleanup.service`, then inspect
   `journalctl -u media-cleanup.service --no-pager -n 30`.

The enabled `media-cleanup.timer` checks hourly. It waits without deleting until
the API key, completed Jellyfin setup, both APIs, and the media mount are
available. It observes movies and episodes independently. A watched flag from
any Jellyfin user starts a 24-hour grace period at first observation. Clearing
the watched flag resets this grace period. Unwatched completed downloads expire
30 days after the cleaner first observes completion; torrent creation date and
file modification dates are deliberately not used. Active playback is skipped.

Before deleting a video the cleaner stops its Transmission torrent. For a
multi-episode torrent, other episodes stay in place and the torrent remains
stopped; do not restart it unless you intend to redownload missing files. The
torrent entry is removed only when all listed files are gone. The cleaner also
removes matching same-stem subtitle sidecars. It never recursively deletes
directories. Other leftovers may require manual review.

This policy deletes downloaded video data, not merely `.torrent` metadata.
Cleanup cannot observe watched status in external players; watched-based
deletion of downloaded files requires playback through Jellyfin or marking the
item watched there. TorrServer's temporary cache uses its own drop behavior.

## Administration

Compose project: `/opt/media-stack/compose.yaml`.
Startup unit: `media-stack.service`. Cleanup state is under
`appdata/cleanup/state.json` on the media filesystem.

Commands: `docker compose -f /opt/media-stack/compose.yaml ps`,
`systemctl list-timers media-cleanup.timer`, and
`python3 /opt/media-stack/test_cleanup.py`.

Current services bind to LAN address `<SERVER_LAN_IP>`. A router DHCP reservation is
recommended to keep that address stable; no router setting was changed here.
OMV is at <http://<SERVER_LAN_IP>/>; change its default administrator password during
initial setup.
