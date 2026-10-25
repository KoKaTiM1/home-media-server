# Build plugin artifacts locally

Compiled plugins and archives are excluded from the public backup. A compiled
assembly can retain server-specific values even after the source is templated.

After setting the private deployment values, build the Dynamic Library DLL
from `plugin-source/dynamic-library` using the .NET SDK command in `RESTORE.md`.
Its matching metadata is retained in `artifacts/DynamicLibrary/meta.json`.
Install the resulting DLL only while Jellyfin is stopped.

Install WizdomSubs Downloader through Jellyfin's plugin catalogue or its
publisher's release package. The source server used Wizdom 0.2.0.0; preserve
the provider's license and verify compatibility with the Jellyfin version.
