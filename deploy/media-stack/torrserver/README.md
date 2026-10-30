# TorrServer saved-torrent index fix

This build applies a small patch to the exact MatriX.147.1 source release
(commit d84f41968780744d4b8643f7a73b22a84d2c3779), retaining the official runtime
image, configuration, credentials, media paths and database format.

The original hash lookup decoded every saved torrent under a global mutex.
Saving one record also rewrote all records. With 950 saved records, stalled
requests accumulated: a private runtime dump showed 5,344 requests waiting
on settings.ListTorrent via GetTorrentDB. The patch reads/writes the requested
hash key only. Full list operations remain available for the normal UI.

Build with `docker compose build torrserver` from the deployment directory.
The pinned Dockerfile downloads the upstream source, applies the patch, runs
focused tests, and builds MatriX.147.1-home-dbindex1. Binary artifacts and private
database copies are not committed. Upstream code is GPLv3; see LICENSE.

Validation: record lookup/save and 32 concurrent reader/writer tests; isolated
API benchmark against a private copy of the 950-entry database with no external
network access. Original: 60 lookups/12 concurrent, 22.187 seconds total,
median 4.0895 seconds. Patched: 2,000 lookups/32 concurrent, 1.004 seconds total,
median 0.0124 seconds, all expected 404 responses for unknown hashes. Restoring
an existing saved torrent retained the exact hash, file index, path and length.
These API measurements do not certify sustained TV playback.

Rollback: select the pinned official runtime image from Dockerfile, recreate
only TorrServer while no player is active, and retain its existing volumes.
No database conversion or deletion is involved.
