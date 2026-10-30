# Regression test gate

Tests remain beside their components (`catalogue/test_*.py`, monitor,
cleanup and metadata worker); this folder provides the single automatic entry point.
The runner discovers all `test_*.py` modules in each registered suite. Interactive
`check_*.py` scripts are not run: some contact live playback services.

Build the reusable test image once:

```sh
docker build -t home-media-tests:1 -f tests/Dockerfile .
python3 tests/regression_gate.py --repository "$PWD" --commit HEAD
```

The container has no external network, Docker socket, credentials or production
state mounts. It receives a read-only source snapshot, temporary writable state,
and resource limits. FFmpeg/HTTP tests use fixtures and container loopback.
The runner saves per-suite logs plus `result.json` and `latest.json` under
`/var/lib/media-regression/` on the testing host. Reports are private and excluded
from the public backup. Any failed suite blocks publication.

The server publisher runs this gate on its exported snapshot before committing.
An installed Git `pre-push` hook invokes `tests/pre_push.py`, checking the exact
committed trees being pushed (including manual pushes from that checkout).
The server's scheduled remote watcher checks new remote branch commits every
five minutes, including changes pushed elsewhere. It records failures in the
systemd journal and the local report; it never deploys or merges a commit.
Hooks can be bypassed and do not enforce policy on other clones. The watcher
reports a remote push after it occurs; it cannot block it. Mandatory PR checks
across all clones would require repository branch protection and hosted CI.
No GitHub API key is needed for this local setup; fetching uses the existing
repository SSH access. Passing tests is not automatic PR approval.

For every bug fix, add or update a regression that fails on the broken code and
passes with the correction. Exercise the real interface between components:
unconditional mocks must not replace the contract the regression is checking.
Tests cannot establish physical TV playback or every torrent's availability;
keep those acceptance checks in the relevant GitHub issue.

Current automated scope: Python catalogue, verification protocol/media helpers,
OMV monitor, cleanup, metadata helper and export privacy. C# plugin builds,
Go/TorrServer patches, PowerShell launcher checks and physical-client acceptance
remain separate checks; this gate does not claim to cover them.
