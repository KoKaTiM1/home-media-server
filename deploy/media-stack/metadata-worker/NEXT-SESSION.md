# Continue the LAN metadata helper

The owner confirmed Windows install and offline demo passed. Server coordinator,
shared leases/provider budget, durable receipts, 30-second importer and OMV
telemetry are installed. The Windows private connection JSON is in the existing
server account home for authenticated SCP/SFTP transfer; no token is public.
The physical PC is now enrolled. Its first 67 metadata records were received
and applied at about 16 results/minute. Keep issue #2 open for the corrected launcher copy and
physical restart check. The updated worker.ps1 is also in the server account
home for SCP transfer; it fixes first-start native Docker inspect errors. Source verification still runs on the server, and metadata does not mark media playable.

Check all open GitHub issues before starting work. Issue #1 still depends on
completion of the initial verification audit. Issue #3 records a confirmed
TorrServer memory-limit restart that happened before coordinator activation;
respect viewing holds for any restart-requiring memory adjustment.

See server/README.md and PROTOCOL.md for the deployment boundaries. Preserve
watch/favorites/files, native metadata identity, source history and title holds.
Do not restart playback services merely because native Sessions is empty.

Current connection is active; preserve the worker identity in its data folder.
