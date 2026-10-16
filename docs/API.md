# HTTP API and configuration

The API returns UTF-8 JSON using schema version `1`. Storage numbers are integer bytes; timestamps are UTC ISO 8601; unavailable measurements are `null`, never invented zeroes. Additive fields may appear within the same API version. Clients should ignore fields they do not use.

## Authentication and routes

Use `Authorization: Bearer TOKEN`. HTTP Basic authentication with username `status` and password `TOKEN` is also accepted for browser access. The two methods expose the same read-only observations. Tokens never belong in URLs or public widget configuration.

| Route | Authentication | Meaning |
| --- | --- | --- |
| `GET /healthz` | None | Process can answer HTTP; always `200` with `{"status":"ok"}`. Does not inspect storage. |
| `GET /readyz` | Required | Current observation envelope; `200` after a successful, nonstale collection, otherwise `503`. A degraded RAID can still have a ready collector. |
| `GET /api/v1/status` | Required | All discovered sets plus configured expected sets that are absent. `200` even when storage health is degraded or collection failed; inspect the JSON. |
| `GET /api/v1/raids/UUID` | Required | One set in `raid`; UUID matching is case-insensitive. `404` if absent, with a missing-set observation. |
| `GET /` | Required | Minimal status page. |
| `GET /status.js` | Required | Status-page script. |

`HEAD` has GET semantics without a response body. `POST`, `PUT`, `PATCH`, `DELETE`, and `OPTIONS` return `405`. Query strings and absolute-form URLs are rejected. Unauthenticated protected requests return `401`; unknown routes return `404` after authentication. There is no CORS configuration or write endpoint.

Responses include `Cache-Control: no-store`. This prevents browser/proxy caching; the service itself still returns its shared observation cache.

## Observation envelope

| Field | Meaning |
| --- | --- |
| `schema_version` | API schema integer, currently `1`. |
| `version` | Helper release version. |
| `collected_at` | Time the most recent successful collection completed, or `null`. |
| `attempted_at` | Time the most recent completed attempt began, or `null`. |
| `collection_ok` | Whether the latest completed attempt succeeded. |
| `error` | `null`, `collection_not_started`, `command_timeout`, or `collection_failed`. Host command output is not exposed. |
| `age_seconds` | Age of the last successful observation, measured using a monotonic clock; `null` before success. |
| `stale` | Whether the last observation is absent or older than the configured threshold. |
| `health` | Effective overall state; see below. |
| `healthy` | True only when effective health is `healthy`. |
| `raids` / `raid` | Array for the aggregate route; one object for the UUID route. |

On collection failure the last successful sample is retained for context. Its effective health changes immediately to `error`, even before the stale timeout. After age expiry, a sample becomes `stale`. **Do not interpret retained member or capacity values as a fresh successful observation.**

Overall state precedence is `error`, `stale`, `missing`, `offline`, `degraded`, `rebuilding`, `unknown`, then `healthy`. No sets is `missing`, not healthy. `collection_ok: true` means that collection succeeded, not that any set is healthy.

## RAID object

| Fields | Meaning |
| --- | --- |
| `uuid`, `name`, `device`, `type` | Stable set UUID, user-facing name, current BSD device identifier, and OS-reported RAID level. A device identifier may change after reconnection. |
| `status` | Raw set state reported by diskutil. |
| `health`, `healthy` | Effective normalized state and boolean, including stale/collection-error overrides. |
| `observed_health` | Normalized state when the retained sample was collected, before freshness/error overrides. |
| `capacity_bytes` | RAID's reported capacity, distinct from filesystem usable capacity. |
| `auto_rebuild` | `true`, `false`, or `null` when policy is unavailable/unrecognized. |
| `member_count`, `online_members`, `members_summary` | Member count, online member count, and text such as `2/2`. Spares are excluded. |
| `rebuilding` | Whether the retained observation reports active rebuild/resync. |
| `rebuild_percent` | Explicit member percentage, or `null`. With several rebuilding members, the minimum is used only if every rebuilding member reports a valid percentage. |
| `rebuild_summary` | Ready-to-display text: percentage, unavailable-progress explanation, `Not rebuilding`, `Unknown`, or a freshness/error explanation. |
| `members`, `spares` | Member objects, each with `uuid`, `device`, raw `status`, normalized `health`, and nullable `rebuild_percent`. |
| `volume` | Linked filesystem/container space and mount observations, below. |

Unknown raw states stay unknown. A rebuild at `100%` remains rebuilding until the OS reports completion. Explicit `N% (Rebuilding)` or `N% (Resyncing)` member states are recognized; no rate, byte-derived progress, or ETA is estimated.

Expected UUIDs missing from diskutil are represented by an object whose `status` is `Missing`, `health` is `missing`, measurements are unavailable, and member lists are empty. Consumers must not infer health from `members_summary: "0/0"`.

## Filesystem/container object

| Field | Meaning |
| --- | --- |
| `status` | `available`, `unavailable`, or `ambiguous`. |
| `filesystem` | `apfs` or the reported legacy filesystem type; `null` if unavailable. |
| `scope` | `container` for APFS, `volume` for legacy filesystem space. |
| `mounted` | Whether any related volume is mounted, or `null` if unavailable. |
| `total_bytes`, `free_bytes`, `used_bytes`, `used_percent` | Space for the stated scope, or `null`. |
| `volumes` | Related volumes with `name`, `device`, and `mounted`; absolute mount paths are not published. |

APFS free space is **shared container space**, not a per-volume allocation or quota allowance. The collector requires one unambiguous container with a single physical store equal to the RAID device. It checks related volume/container identities before including them. Ambiguous ancestry is not guessed. The legacy fallback asks only the RAID device, never a filesystem path that could silently fall back to the system disk.

The sequence of read-only diskutil commands is **not an atomic storage snapshot**. Space and mount observations can change while collection runs. This is a monitoring API, not a storage-operation precondition checker.

## Example response

Abbreviated synthetic per-set response; omitted fields remain part of the documented contract:

```json
{
  "schema_version": 1,
  "version": "0.2.0",
  "collected_at": "2026-01-01T12:00:00Z",
  "attempted_at": "2026-01-01T11:59:59Z",
  "collection_ok": true,
  "error": null,
  "age_seconds": 2.5,
  "stale": false,
  "health": "healthy",
  "healthy": true,
  "raid": {
    "uuid": "11111111-1111-4111-8111-111111111111",
    "name": "Archive",
    "health": "healthy",
    "members_summary": "2/2",
    "rebuild_percent": null,
    "rebuild_summary": "Not rebuilding",
    "volume": {
      "status": "available",
      "scope": "container",
      "free_bytes": 1000000000000
    }
  }
}
```

## Configuration

The installer creates this exact-key JSON structure in `config.json`:

```json
{
  "bind": "127.0.0.1",
  "port": 8788,
  "interval_seconds": 30,
  "timeout_seconds": 10,
  "stale_after_seconds": 90,
  "expected_raids": ["11111111-1111-4111-8111-111111111111"]
}
```

- `bind`: literal IPv4 address. Loopback is the default. Remote access requires an explicit reachable address; `0.0.0.0` listens on all IPv4 interfaces and should only be chosen deliberately.
- `port`: integer `1024–65535`.
- `interval_seconds`: integer `10–3600`; delay after each completed collection. Slow collection increases the actual time between samples.
- `timeout_seconds`: integer `1–30`, applied per diskutil command. The whole collection also has a fixed 30-second deadline; each command is limited to the smaller of its configured timeout and the remaining collection time.
- `stale_after_seconds`: integer `20–86400`, at least twice the polling interval.
- `expected_raids`: array of up to 64 UUIDs; these are tracked for disappearance. It does not hide other discovered sets from the aggregate route.

The sibling `token` file contains 32–256 URL-safe letters, digits, underscores, or hyphens. The installer generates it. Both private files must be owned by the current user, regular files, mode `0600`, and accessed through paths without symlink components. Restart the helper after editing configuration or rotating the token, and update consumers when rotating.

Local lifecycle state is separate from this HTTP contract. The installed
launcher's `status` command reports saved/native startup intent and job state;
`check` additionally verifies live readiness. See [service lifecycle](lifecycle.md).

HTTP serving allows up to eight concurrent handlers, each with a three-second socket timeout. Excess connections are closed. These bounds keep the small private interface responsive; they are not a substitute for a reverse proxy when exposing it to untrusted networks.
