# Continue after USB phase 1

The owner authorized a Windows metadata helper carried to the PC on USB, with
actual assignments/results transferred over LAN or Tailscale. Prepare the USB
package first; server worker behavior is not to change during this stage.

Completed package contents: metadata-only Docker client, offline demonstration,
Windows install/configure/start/status/logs/stop launchers, resumable local
outbox, bounded logs/state, HTTPS certificate validation/pinning and scoped-token
protocol. No live token, host address or database is included. The coordinator
is not active and no claims have been made against the live server.

Track the remaining work in GitHub issue #2. Issue #1 retains its separate
post-verification playback/subtitle prerequisite. Use the main owner account
for connector changes; it was verified again during USB preparation.

Next work:

1. Have the owner copy the folder to a local Windows drive, load the included
   image and run the offline demo. Record the actual Windows/Docker outcome.
2. Check open GitHub issues first and the verification prerequisite for issue 1.
   Preserve all known viewing holds and owner playback safety instructions.
3. Inspect the existing metadata and verification pipeline to choose a useful
   bounded prefetch queue. The basic bulk IMDb index already exists; source
   verification is the bottleneck for library visibility. Metadata fetching
   alone does not prove playback or finish that verification audit.
4. Implement the actual server coordinator/lease registry/importer according to
   PROTOCOL.md. Stage and test without altering runtime workers first. No
   production endpoint may claim exclusive assignments before the existing
   background metadata paths cooperate with its lease registry.
5. Set up authenticated HTTPS, a scoped revocable worker token and restricted
   network access. Deliver certificate identity/token privately; keep credentials
   off public GitHub, logs and future USB source archives.
6. Activate only after the package and integration are ready under the owner's
   server-safety constraints. If a playback-service restart would be needed,
   protect known active/paused viewing even when native Sessions appears empty.
7. Run a small real series-only batch. Verify receipts, source identity/freshness,
   exact-once import behavior, disconnect/restart/replay handling and preserved
   native watch/favorites. Measure actual speed before expanding the queue.
8. Export sanitized source/docs to GitHub using the existing privacy workflow;
   never publish image archives, live config, runtime state, keys or endpoints.

The worker runs manually after Windows reboot. Automatic startup is a later
choice. Token changes on the same server are allowed; a data folder cannot be
reused silently for a different coordinator/certificate identity.
