# Changelog

## 0.3.1 — 2026-10-23

- Avoid a blocking whole-disk `info` probe during eject eligibility and identity rechecks. Native sampling found it stalled opening active RAID media while checking low-level formatting support, causing `preflight_unavailable`.
- Classify each derived physical parent with documented `list -plist external physical DEVICE` filters and exact whole/partition inventory checks. Preserve GPT, system-media, external/ejectable, partition UUID, member UUID, size and opaque member device-path guards; ambiguous or incomplete results remain ineligible.
- Add synthetic regression coverage and a native read-only preflight check. No real eject or unmount is used for validation.

## 0.3.0 — 2026-10-23

- Add disabled-by-default normal/force eject for explicitly admitted healthy external two-member APFS mirrors, using a separate control credential and exact UUID confirmation.
- Resolve fresh complete membership/topology, refuse rebuilds and unsupported storage, unmount all related volumes, verify each exact physical member and report partial/unknown results without automatic retry.
- Serialize control with collection; persist bounded request/uncertainty state and provide stopped local enable/disable/inspection acknowledgement commands that preserve monitoring credentials.
- Preserve the version-1 monitoring schema, routes and read-only token authority. Native mutation behavior is fixture-tested; production storage is never ejected as a deployment test.

## 0.2.1 — 2026-10-22

- Report Mirror rebuild percentages from valid native `MemberRebuildStatus` byte counters and RAID capacity when no explicit percentage is present.
- Preserve explicit-percentage precedence, unknown progress for invalid or incomplete observations, and native rebuild state even at `100%`.
- Add synthetic regression coverage for progress boundaries, malformed counters, other RAID levels, stale member counters and multiple rebuilding members.

## 0.2.0 — 2026-10-16

- Use a named executable for launchd and local lifecycle commands, with an optional deployment-specific launcher path.
- Fresh installation stays disabled; explicit start and stop persist intent in the owned plist as well as the native override. Reinstallation preserves intent.
- Separate read-only lifecycle `status` from authenticated readiness `check`.
- Serialize lifecycle mutations per stable user/label and validate the complete owned plist/launcher contract.
- Guard the stopped migration from 0.1.0 while retaining configuration, token, API schema and launch label.
- Retain a bounded private installation journal so interrupted multi-file updates can be checked and recovered by a reviewed install retry.
- Keep launchd as the sole process-exit recovery owner; no running-process watchdog or additional scheduler.

Behavior change: `stop` now remains stopped after login, and `status` can report a deliberately stopped job successfully. Scripts that require readiness should use `check`.

## 0.1.0 — 2026-10-16

Initial release.

- Read-only AppleRAID collection from native diskutil plist output.
- Versioned authenticated HTTP API with aggregate and per-UUID observations.
- Member availability, set capacity, rebuild policy, explicitly reported rebuild progress and linked filesystem/container capacity.
- Shared observation cache, collection errors, stale-state handling, liveness and readiness endpoints.
- Per-user launchd installation and lifecycle commands.
- Minimal authenticated status page.
- Homepage Custom API configuration with environment/file-backed secrets and optional member details.
- Portable regression tests and CI for Python 3.10 and 3.14 on macOS and Linux.

No SMART telemetry, repair operations, historical store, alert delivery or upstream Homepage plugin is included.
