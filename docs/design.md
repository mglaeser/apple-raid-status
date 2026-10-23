# Requirements and design

## Intended use

One small macOS process observes AppleRAID through supported read-only command-line output. Any dashboard can retrieve the same versioned JSON over HTTP. Homepage uses its built-in Custom API widget; the collector has no dependency on Homepage and remains useful before or after a dashboard migration.

## Functional requirements

| ID | Requirement | Delivered behavior |
| --- | --- | --- |
| F1 | Discover AppleRAID sets | Parse `diskutil appleRAID list -plist`; expose stable set/member UUIDs and current device identifiers. |
| F2 | Report storage state | Raw OS state plus normalized healthy/degraded/rebuilding/offline/missing/unknown states. |
| F3 | Report basic metrics | Set capacity, online/total members, automatic rebuild policy, linked filesystem/container space. |
| F4 | Report rebuilding honestly | Explicit member percentages, or validated mirror byte progress divided by RAID capacity; preserve unknown progress and never infer completion from `100%`. |
| F5 | Detect disappearance | Configured expected UUIDs produce missing-set observations; per-set requests return `404` for absent sets. |
| F6 | Expose remote observations | Authenticated JSON over HTTP, aggregate and UUID-specific routes. |
| F7 | Integrate with Homepage directly | Ready-to-use four-block Custom API configuration, optional member list and secret-variable examples. |
| F8 | Distinguish collection failures | Timestamps, sample age, collection errors, stale state and separate liveness/readiness. |
| F9 | Support another dashboard | Stable versioned data contract independent of widget markup or Homepage internals. |
| F10 | Operate as a local service | One user LaunchAgent, named executable, persistent start/stop intent, serialized lifecycle commands and separate status/readiness checks. |

## Nonfunctional requirements

| ID | Requirement | Design decision |
| --- | --- | --- |
| N1 | Small deployment | Python standard library only; no pip packages, database, container, cloud, or mandatory metrics platform. |
| N2 | Low steady load | One background collector; 30-second default delay after collection; all HTTP clients share a cache. |
| N3 | Separate observation/control authority | Monitoring uses fixed read-only plist commands. Explicit opt-in control adds only guarded unmount/eject with a separate credential; no repair, format, mount or rebuild API. |
| N4 | Limited privileges | Run as the installing user; no root collector, sudo rule or private-framework dependency. |
| N5 | Bounded work | Per-command timeout and 30-second whole-collection deadline; bounded set/member counts; eight HTTP handlers and three-second socket timeouts; monitoring never triggers scans. Optional control preflight has a 30-second bound, action a 150-second overall deadline. |
| N6 | No false reassurance | Failures immediately override retained health; unknown data stays unknown; liveness is independent of RAID health. |
| N7 | Private configuration | Random token, owner-only private files, explicit remote binding, no credential/query logging. |
| N8 | Portable integration | Plain HTTP/JSON and stable UUID selection; any client can consume it. |
| N9 | Maintainability | Small source, documented schema, synthetic regression fixtures and standard-library tests on macOS/Linux. |
| N10 | Reproducible adoption | Tagged source, license, public setup examples, lifecycle commands and explicit limitations. |
| N11 | Predictable operator control | Fresh installation stays disabled; preparation preserves intent; explicit stop survives login; launchd alone recovers process exits. |

These are implementation requirements, not claims about certification, exhaustive hardware compatibility, or a guaranteed resource budget.

The [lifecycle contract](lifecycle.md) describes named executable identity,
durable plist intent, native override handling, per-label serialization and the
guarded migration from the first release. Read-only collection never changes
storage as part of service start, stop or process recovery.

## Collection and HTTP flow

1. The collector asks diskutil for all AppleRAID sets in plist form.
2. It validates identifiers, sizes, states and membership, then normalizes the observations.
3. It discovers APFS containers and relates physical stores to RAID devices. Related volume identity checks supply mount observations. A legacy fallback asks only the RAID device for filesystem space.
4. A successful collection atomically replaces the application's in-memory cache. The underlying diskutil reads are sequential, not an atomic OS snapshot.
5. HTTP requests copy that cache, add age/freshness state and optionally select a UUID. They never initiate storage work. Optional control requests use a separate fresh preflight and operation lock; they do not treat this cache as authority.
6. A failed collection retains the last sample for context but changes effective health to `error`. Excessive age produces `stale`.

Only one polling thread runs. Each diskutil subprocess uses the shorter of its configured timeout and the remaining 30-second collection deadline. Timeout accounting bounds user-space work; it cannot make guarantees about an uninterruptible kernel storage request. The helper waits for its normal next attempt and does not retry rapidly or run repairs.

The server uses Python's standard HTTP server for a narrow, private monitoring interface. It limits concurrent requests and connection timeouts, disables access logs, and exposes no arbitrary filesystem or command endpoint. It does not implement TLS, public Internet hardening, multi-user authorization or rate-limit infrastructure. Use an existing HTTPS proxy/private tunnel when needed.

## Storage semantics

- RAID availability, drive SMART health, filesystem integrity, and backup recoverability are different properties. This tool reports only the first plus basic capacity observations.
- Disk numbers can change; UUIDs select identity. Names are display labels.
- APFS container space is shared among its volumes. It is not a guarantee that a particular volume can allocate that much under its quota/reservation rules.
- APFS ancestry must be unambiguous. A matching pathname alone does not prove a filesystem belongs to the RAID.
- The OS may report rebuilding without a usable percentage. A missing percentage is not zero.
- Mirror, stripe and concatenated sets have different redundancy properties. `healthy` means recognized online membership for the reported level; it does not mean every level tolerates a failed disk.

Apple's [AppleRAID definitions](https://github.com/apple-oss-distributions/AppleRAID/blob/57b220120900b68694f642838aa25249417fe4a8/AppleRAIDUserLib.h#L126) document rebuild progress as bytes completed. The [mirror implementation](https://github.com/apple-oss-distributions/AppleRAID/blob/57b220120900b68694f642838aa25249417fe4a8/AppleRAIDMirrorSet.cpp#L133-L137) uses the mirror's logical size as the total. Native `diskutil appleRAID list -plist` observations expose this counter as `MemberRebuildStatus` and the total as `Size`. The helper uses those plist fields only for rebuilding mirror members with valid bounded integer values; it does not call a private API or use filesystem used space as the denominator. See the [API contract](API.md) for precedence and unavailable-progress handling.

## Why the existing alternatives do not provide this interface

| Tool | Relevant capability | Remaining gap |
| --- | --- | --- |
| [Homepage](https://gethomepage.dev/widgets/services/customapi/) | Supported configurable JSON API display | Does not itself collect native AppleRAID state. |
| [node_exporter](https://github.com/prometheus/node_exporter#collectors) | Darwin system/filesystem/disk metrics | `mdadm` RAID collection reads Linux `/proc/mdstat`; a custom AppleRAID collector is still needed. |
| [Glances](https://github.com/nicolargo/glances#requirements-) | Cross-platform host metrics and HTTP API | Its RAID dependency is Linux-only `pymdstat`. |
| [Netdata](https://github.com/netdata/netdata/blob/master/src/collectors/proc.plugin/proc_mdstat.c) | Metrics history and monitoring | The examined software RAID collector reads Linux `/proc/mdstat`, not AppleRAID. |
| [Scrutiny](https://github.com/AnalogJ/scrutiny) | Drive SMART monitoring | Drive telemetry does not supply AppleRAID set membership and rebuild state. |

This comparison describes the specific interfaces inspected, not a claim that no third-party collector exists anywhere.

## Homepage conventions and deliberate limits

The default example uses one row with four blocks, minimizes API requests, reads only, and uses Homepage's existing proxy and formatting. These align with [Homepage's service-widget guidance](https://gethomepage.dev/widgets/authoring/getting-started/#service-widget-guidelines).

This repository provides a **Custom API integration**, not an upstream `type: appleraid` plugin. Native Homepage widgets are registered in its source tree; a separate GitHub repository does not automatically add one. Upstream submissions require demonstrated community interest and are subject to the project's [contribution policy](https://github.com/gethomepage/homepage/blob/dev/CONTRIBUTING.md).

Custom API does not support conditional block highlighting. Text labels carry health state; a separate dashboard may apply colors using the same JSON. The helper does not add a frontend framework, widget build pipeline, dashboard-specific endpoint, historical store, notification service, Prometheus dependency, or repair UI.

## Validation boundaries

Automated tests should cover realistic state transitions and failure modes: healthy membership, missing/degraded/rebuilding states, unknown formats, invalid identities, APFS ancestry ambiguity, collection timeouts, stale data, authentication, HTTP routing and configuration privacy. Synthetic fixtures must never contain a user's real device identity or token.

On a real Mac, verify `/readyz`, compare the API with read-only diskutil output, check the actual dashboard, and exercise helper stop/start independently of storage. Do not disconnect a healthy member or trigger a rebuild merely to test the UI. Rebuild parsing fixtures do not prove every macOS version reports progress identically.

## Optional eject boundary (0.3.0)

The operator requested a deliberate normal/force eject API. Reuse Apple's
`diskutil unmountDisk [force]` and `eject [force]` in the existing user process;
no new daemon or privileged helper is needed. A virtual RAID eject is not assumed
to release every physical member. The supported workflow unmounts the complete
logical RAID, verifies all related APFS volumes unmounted, then ejects each
validated physical member and verifies both are absent. Its sequential partial
failure is explicit; no automatic force fallback, disk removal, repair or retry
is admitted.

Fresh UUID/member/partition/APFS identity checks allow only a healthy two-member
Mirror, no spare, with dedicated external physical GPT disks. Each member disk
may contain its own EFI and the Apple_Boot helper explicitly referenced by that
member; any unrelated partition is refused. APFS roles are limited to ordinary
volumes or Backup; boot/system volumes, locked volumes and crypto migration are
excluded. The preserved physical identity plan is rechecked before each ejection;
the second member is never selected by reinterpreting a now-degraded set. Native
mount checks follow unmount and precede every member eject.

A separate control allowlist/token and Bearer-only POST leave existing clients
read-only. A shared operation lock serializes preflight/action with collection.
The private intent/result journal refuses duplicate request IDs for 24 hours and
retains unresolved operations indefinitely. A timeout remains unknown even if
the CLI process is killed, because kernel storage work may outlive it. A stopped
host-side acknowledgement after inspection is the only way to clear uncertainty.
The API contract documents bounded body parsing, output discard, returned outcomes
and the absence of a claim that two physical ejections are atomic.
