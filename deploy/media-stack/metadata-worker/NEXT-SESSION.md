# Continue the LAN metadata helper

The owner confirmed Windows install and offline demo passed. Server coordinator,
shared leases/provider budget, durable receipts, 30-second importer and OMV
telemetry are installed. The Windows private connection JSON is in the existing
server account home for authenticated SCP/SFTP transfer; no token is public.
Copy it into config/config.local.json and run 04-START.cmd, then check the
receiver for that PC's first claim and metadata uploads. Keep issue #2 open
until the physical PC batch/throughput is confirmed. Source verification still
runs on the server, and metadata does not mark media playable.

Check all open GitHub issues before starting work. Issue #1 still depends on
completion of the initial verification audit. Issue #3 records a confirmed
TorrServer memory-limit restart that happened before coordinator activation;
respect viewing holds for any restart-requiring memory adjustment.

See server/README.md and PROTOCOL.md for the deployment boundaries. Preserve
watch/favorites/files, native metadata identity, source history and title holds.
Do not restart playback services merely because native Sessions is empty.
