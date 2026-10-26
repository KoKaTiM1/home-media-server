# LAN metadata protocol: home-media-metadata/1

Client and server implementations are included. The receiver uses its own
restricted HTTPS endpoint, separate from Jellyfin and the catalogue bridge.

## Transport

Use a dedicated HTTPS origin over LAN or Tailscale. Every endpoint is under
`/v1/metadata-worker`. Require `Authorization: Bearer <scoped-worker-token>`.
Credentials must have only metadata-claim/result rights, not native Jellyfin,
shell, torrent or administration rights. Restrict access to the intended PC.
Configure a trusted public certificate or independently verify the server's
SHA256 DER certificate fingerprint before saving it on Windows. Pin comparison
is performed after TLS establishment and before sending the token.

All replies use HTTP 200 with a JSON object containing `protocol` equal to
`home-media-metadata/1`. Non-200 codes are transport/access errors. Do not
redirect coordinator calls. Requests/results need bounded body sizes. Never log bearer tokens,
native identifiers, signed media links or raw response bodies.

## Client endpoints

`GET /capabilities` must return `enabled: true`, `role: "metadata-only"`,
`exclusive_assignments: true`, `server_only_import: true`, `kinds: ["series"]`
(or the permitted kinds), and `min_request_interval_seconds` (default 3).
These booleans represent actual implemented behavior, not deployment wishes.

All POST bodies contain `protocol` and a persistent `worker_id`. A token must
be bound to that worker/kind during registration. Do not trust a worker-supplied
identity to expand token access.

`POST /claim` receives `kind` and `limit: 1`. Return `tasks: []` and an optional
`retry_after_seconds` if there is no work. Otherwise return a task containing:
`job_id`, `lease_id`, `generation` (bounded opaque identifiers), `kind` and
`imdb`. No commands, arbitrary URLs, server paths or credentials are accepted
as assignments. A lease must be committed durably before responding.

`POST /renew` receives the task identity. Return the same `job_id`, `lease_id`
and `active: true` only if that token/worker still owns this exact assignment.
Renew for at least 120 seconds to cover a bounded provider request and upload.
An expired/reassigned lease returns `active: false`; it does not reclaim it.

`POST /result` receives the task identity plus `result` containing:
`result_id`, `source: "cinemeta"`, `fetched_at`, `status` and either `metadata`
for `ok`, or a bounded `reason` for `retry`/`not_found`. Expected reasons are
`provider_not_found`, `provider_http_error`, `provider_network_error` and
`provider_metadata_invalid`. A movie result is title metadata; a series result
may contain episode metadata. No verification/readiness proof is submitted.
Provider HTTP failures also include `provider_status` and a bounded numeric
`retry_after_seconds`; use these to coordinate cooldowns across both machines.

Respond with the exact `result_id` and `ack` in `accepted`, `duplicate`, `stale`
or `rejected`. Unknown/wrong IDs must not acknowledge unrelated submissions.

## Server invariants

- Use a durable queue/lease registry. Background metadata work on both machines
  must claim through the same registry, including any existing server sync paths
  that overlap. A filename split alone does not provide this guarantee.
- Initially assign series to Windows and movie enrichment to the server. Restrict
  claims to missing/stale metadata useful to near-term verification/import;
  avoid fetching a million full records that will expire before being used.
- Atomically fence leases by token, worker, job, lease and dataset generation.
  A generation change or lease reassignment cannot accept an old write.
- Commit an immutable result receipt and metadata handoff in one server-local
  transaction. When acceptance has succeeded but its reply is lost, the same
  result ID/payload must return `duplicate` even after the lease is completed.
  Reject reuse of a result ID with a different payload or unauthorized token.
- Apply new submissions only while their assignment is valid; expired first
  submissions return `stale`. Never advance a source verification record or
  mark playback available on a metadata result, including `not_found`.
- Validate types, IMDb identity, field sizes, episode identity/release dates,
  freshness, source, body size and allowed artwork hosts. Avoid arbitrary URL
  fetches/private-network artwork URLs, HTML execution and partial imports.
- Import through the existing server metadata/cache/index logic and supported
  Jellyfin APIs. Retain newer local data; preserve watch/favorites/holds and
  existing library/source policy. No direct writes to Jellyfin's database.
- Coordinate provider cooldown/rate budgets across both workers. A 429 response
  applies backoff; multiple PCs must not bypass a shared provider limit.
- Interactive viewing remains available. Define a documented exception for any
  urgent interactive metadata fetch; it must not overwrite a newer imported
  record or be confused with duplicate background assignment.
- Handle shutdown, token revocation, worker sleep, lost upload replies, retries,
  lease expiry, failed metadata import and restart with retained receipts.
  Operational counters must distinguish fetched, accepted, applied and failed.

The production coordinator/importer is installed. Physical Windows connection and first batch are confirmed: 67 records applied
at about 16 results/minute. Launcher/restart follow-up is tracked in issue #2. The receiver commits a
receipt and import handoff together; the separate trusted importer applies the
normal cache idempotently. Background full-title fetches use shared leases and
provider budget, and background catalogue pages share that rate budget.
