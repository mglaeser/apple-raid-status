# Changelog

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
