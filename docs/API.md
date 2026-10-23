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

`HEAD` has GET semantics without a response body. `PUT`, `PATCH`, `DELETE`, and `OPTIONS` return `405`. `POST` also returns `405` unless the separately authorized optional eject capability is enabled. Query strings and absolute-form URLs are rejected. Unauthenticated protected requests return `401`; unknown routes return `404` after authentication. There is no CORS configuration. Optional control routes are documented below and never use monitoring authority.

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
| `rebuild_percent` | Explicit member percentage or validated Mirror byte-counter percentage, otherwise `null`. With several rebuilding members, the minimum is used only if every rebuilding member has known progress. |
| `rebuild_summary` | Ready-to-display text: percentage, unavailable-progress explanation, `Not rebuilding`, `Unknown`, or a freshness/error explanation. |
| `members`, `spares` | Member objects, each with `uuid`, `device`, raw `status`, normalized `health`, and nullable `rebuild_percent`. |
| `volume` | Linked filesystem/container space and mount observations, below. |

Unknown raw states stay unknown. A rebuild at `100%` remains rebuilding until the OS reports completion. No rate or ETA is estimated.

For rebuilding members, progress uses this precedence:

1. An explicit `N% (Rebuilding)` or `N% (Resyncing)` member state takes precedence. Values outside `0`–`100` remain `null`, even if a byte counter is also present.
2. For a `Mirror` member in plain `Rebuilding` or `Resyncing` state, compute `100 * MemberRebuildStatus / Size`. AppleRAID reports `MemberRebuildStatus` in bytes; `Size` is the RAID capacity, not free filesystem space or member count. Both values must be integers (excluding booleans) no larger than `2^63 - 1`, with `0 <= MemberRebuildStatus <= Size` and `Size > 0`. Zero bytes is valid progress. Missing, negative, noninteger or out-of-range values remain `null`.
3. Other RAID levels, spares, and members outside rebuilding/resyncing state do not use byte counters. A leftover counter on an online or failed member cannot indicate an active rebuild.

The numeric percentage is not rounded before aggregation. The display summary uses a compact representation; neither a displayed nor numeric `100%` overrides the native member state.

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
  "version": "0.3.0",
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

## Optional eject controls

Controls are absent/disabled by default. The stopped local enable command adds
this optional top-level configuration member without changing the monitoring
token or existing observations:

```json
{"controls":{"enabled":true,"raids":["11111111-1111-4111-8111-111111111111"]}}
```

At most eight unique UUIDs are admitted. `expected_raids` is disappearance
monitoring, never a control allowlist. The sibling mode-0600 `control-token` must
be a distinct 32–256-character URL-safe secret. Monitoring Bearer/Basic tokens
cannot control storage; the control token cannot read ordinary monitoring routes.
No root execution or automatic privilege elevation is supported.

| Route | Authority | Result |
| --- | --- | --- |
| `GET /api/v1/raids/UUID/eject` | Monitoring token | Fresh, read-only eligibility check when controls are enabled. Returns `eligible`, a bounded `reason`, and, when eligible, `members`, `volumes`, `force_supported`. This never reserves or ejects storage; POST repeats all checks. Disabled installations return `404`. |
| `POST /api/v1/raids/UUID/eject` | Separate control Bearer token | One explicitly confirmed normal or forced eject. Disabled installations return `405`. |

POST requires `Content-Type: application/json`, one bounded Content-Length
(1–1024 bytes), and an `Idempotency-Key` containing a canonical hyphenated UUID.
UUIDs are case-insensitive and normalized internally. The body must have exactly
these two keys; the confirmation UUID must match the path:

```json
{"force":false,"confirm":"11111111-1111-4111-8111-111111111111"}
```

`force:true` is the separate force-eject choice. It never follows automatically
from a failed normal request and never bypasses health or topology guards.
Browser Basic credentials, Origin/Fetch Metadata headers, transfer encoding,
forms, duplicate JSON keys/headers and extra fields are refused. There is no
CORS, GET side effect, arbitrary command, device path or mount-point input.
A trusted dashboard backend sends the POST after its own explicit confirmation;
never expose the control token to its browser.

Preflight resolves the UUID afresh, requires a healthy Online Mirror with exactly
two online members and no spares, rejects rebuilding/resync/unknown states, and
verifies external physical GPT ancestry, member UUIDs, partition UUIDs and the
single APFS physical store. Only the exact RAID member, one EFI partition and an
optional member-referenced Apple_Boot helper are allowed on each physical disk.
APFS volume roles must be empty or Backup; system/boot/locked/migrating volumes
are excluded. Cached monitoring results do not grant eligibility.

Execution unmounts the logical RAID (forcing only when explicitly requested),
verifies every linked APFS volume is unmounted, then ejects each exact verified
physical member. Identity and mount state are rechecked before each eject; both
physical disks and the RAID set must be absent for success. It does not remove
members from the RAID metadata, erase disks or promise an atomic two-disk eject.
Concurrent control or collection causes a busy refusal or serialized collection;
monitoring HTTP still reads its cache. Native CLI output is discarded and capped
at 64 KiB, each action at 65 seconds and the operation at 150 seconds. Clients
should allow at least 180 seconds and treat a lost response as unknown.

| HTTP | `outcome` | Meaning |
| --- | --- | --- |
| `200` | `ejected` | Both members verified absent; `members_ejected:2`, `safe_to_disconnect:true`. |
| `409` | `refused` | Guard/busy/duplicate rejection, or native unmount refused with unchanged verified topology. No member eject is attempted for a failed unmount. A failed unmount may have unmounted some volumes. |
| `503` | `partial` | At least one member was verified ejected before failure; `inspection_required:true`, `safe_to_disconnect:false`. |
| `503` | `unknown` | An attempted operation could not be verified, including timeout; inspect on the host. Killing diskutil cannot guarantee cancellation of kernel work. |

Responses contain bounded reason codes and an `operation_id` when applicable;
raw errors, disk paths and credentials are not returned. Common refusal reasons
are `controls_disabled`, `raid_not_allowed`, `root_forbidden`, `operation_busy`,
`duplicate_request`, `inspection_required`, `rebuild_in_progress`,
`raid_not_healthy`, `unsupported_topology`, `unrelated_partitions`,
`identity_changed`, `preflight_unavailable` and `unmount_refused`.

The private `control-state.json` intent/result journal is fsynced before the
first native mutation. Completed request IDs remain rejected for 24 hours;
unresolved started/partial/unknown operations never expire. At most 128 retained
records are permitted; exhaustion refuses another operation. Do not resend an
uncertain POST or generate new IDs to bypass uncertainty. Restart, preparation,
disable and uninstall preserve this journal. After inspection, the stopped local
`controls-clear --confirm UUID` acknowledges uncertainty without operating on
storage; it retains recorded request IDs. See the lifecycle instructions.

The advertised `force_supported` flag means this helper accepts the explicit
force action for an eligible layout; it is not proof of native permission or
successful ejection. The command syntax was reviewed against the installed
macOS 27.0.1 `diskutil` manual. Other macOS versions and actual ejection need
separate disposable-media acceptance; unsupported native behavior fails closed.
Separate diskutil reads and mutations have an unavoidable interval between them:
another local administrator or physical hotplug can change storage in that gap.
The final state check minimizes this interval but cannot make it atomic. The
operation lock covers this one installed service process; manually running
additional control instances is outside the supported deployment.
