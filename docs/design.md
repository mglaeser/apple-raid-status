# Requirements and design

## Intended use

One small macOS process observes AppleRAID through supported read-only command-line output. Any dashboard can retrieve the same versioned JSON over HTTP. Homepage uses its built-in Custom API widget; the collector has no dependency on Homepage and remains useful before or after a dashboard migration.

## Functional requirements

| ID | Requirement | Delivered behavior |
| --- | --- | --- |
| F1 | Discover AppleRAID sets | Parse `diskutil appleRAID list -plist`; expose stable set/member UUIDs and current device identifiers. |
| F2 | Report storage state | Raw OS state plus normalized healthy/degraded/rebuilding/offline/missing/unknown states. |
| F3 | Report basic metrics | Set capacity, online/total members, automatic rebuild policy, linked filesystem/container space. |
| F4 | Report rebuilding honestly | Explicit member percentages only; preserve unknown progress and never infer completion from `100%`. |
| F5 | Detect disappearance | Configured expected UUIDs produce missing-set observations; per-set requests return `404` for absent sets. |
| F6 | Expose remote observations | Authenticated JSON over HTTP, aggregate and UUID-specific routes. |
| F7 | Integrate with Homepage directly | Ready-to-use four-block Custom API configuration, optional member list and secret-variable examples. |
| F8 | Distinguish collection failures | Timestamps, sample age, collection errors, stale state and separate liveness/readiness. |
| F9 | Support another dashboard | Stable versioned data contract independent of widget markup or Homepage internals. |
| F10 | Operate as a local service | User LaunchAgent, install/start/stop/restart/status/uninstall commands and configurable state directory. |

## Nonfunctional requirements

| ID | Requirement | Design decision |
| --- | --- | --- |
| N1 | Small deployment | Python standard library only; no pip packages, database, container, cloud, or mandatory metrics platform. |
| N2 | Low steady load | One background collector; 30-second default delay after collection; all HTTP clients share a cache. |
| N3 | Read-only operation | Fixed `diskutil appleRAID list`, `apfs list`, and `info` commands with plist output; no repair, format, mount, rebuild, or write API. |
| N4 | Limited privileges | Run as the installing user; no root collector, sudo rule or private-framework dependency. |
| N5 | Bounded work | Per-command timeout and 30-second whole-collection deadline; bounded set/member counts; eight HTTP handlers and three-second socket timeouts; no request-triggered disk scans. |
| N6 | No false reassurance | Failures immediately override retained health; unknown data stays unknown; liveness is independent of RAID health. |
| N7 | Private configuration | Random token, owner-only private files, explicit remote binding, no credential/query logging. |
| N8 | Portable integration | Plain HTTP/JSON and stable UUID selection; any client can consume it. |
| N9 | Maintainability | Small source, documented schema, synthetic regression fixtures and standard-library tests on macOS/Linux. |
| N10 | Reproducible adoption | Tagged source, license, public setup examples, lifecycle commands and explicit limitations. |

These are implementation requirements, not claims about certification, exhaustive hardware compatibility, or a guaranteed resource budget.

## Collection and HTTP flow

1. The collector asks diskutil for all AppleRAID sets in plist form.
2. It validates identifiers, sizes, states and membership, then normalizes the observations.
3. It discovers APFS containers and relates physical stores to RAID devices. Related volume identity checks supply mount observations. A legacy fallback asks only the RAID device for filesystem space.
4. A successful collection atomically replaces the application's in-memory cache. The underlying diskutil reads are sequential, not an atomic OS snapshot.
5. HTTP requests copy that cache, add age/freshness state and optionally select a UUID. They never initiate storage work.
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

Apple's [AppleRAID definitions](https://github.com/apple-oss-distributions/AppleRAID/blob/main/AppleRAIDUserLib.h) document its states and a native byte-progress property. This helper does not call that private API or assume that its properties have identical names in diskutil output.

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
