# Validation record

## Version 0.2.0 lifecycle coverage

The portable lifecycle suite adds tests for fresh disabled preparation, persisted
start/stop intent when the external macOS override is absent, preparation after a
deliberate stop, exact legacy migration, conflicting job identities, failure
ordering, shared-label locking and installed commands without a source checkout.
It also verifies interrupted-update recovery and the listener's PID/address
before an API credential is sent.
These fixtures do not bootstrap or stop a real launchd service.

Live acceptance for this release is recorded separately in the
[v0.2.0 release notes](https://github.com/mglaeser/apple-raid-status/releases/tag/v0.2.0).
The measurements below belong to the initial release, not the corrected
lifecycle. Neither fixture tests nor process recovery establish full host reboot
acceptance.

## Initial release

Validation performed on 2026-10-16 for version 0.1.0. This records what was tested; it is not a claim of universal macOS or RAID compatibility.

## Native deployment

The helper was installed as an ordinary user's LaunchAgent on **macOS 27.0.1, Apple Silicon**. Read-only live collection reported a real AppleRAID mirror with **2/2 online members**, and linked APFS container capacity/free-space data. Authenticated HTTP requests also succeeded from a separate Linux container, exercising the intended remote-dashboard connection.

The observed helper process used **14–16 MiB resident memory** and showed **0.0% CPU in idle samples**. The figures exclude transient diskutil subprocesses, the operating system and dashboard. Polling necessarily uses some CPU; these samples are neither a continuous average nor a guaranteed maximum.

The installed job recovered after process termination through launchd supervision in approximately **4.45 seconds** in one test. This is an observation, not a restart-time guarantee. The configuration starts after user login; a restart test does not establish boot-before-login support.

During deployment, launchd's `Background` process scheduling caused diskutil calls to exceed their deadline. The shipped LaunchAgent uses **`ProcessType: Standard`** while retaining low-priority I/O. Avoid changing it to `Background` merely to reduce the apparent service priority.

No disk was disconnected, repaired, reformatted, or deliberately rebuilt to produce this evidence. Actual rebuilding percentages, legacy HFS+ space reporting and other macOS releases were not validated on live storage in this release.

## Portable tests and clean source checkout

The **29-test** standard-library suite covers collection/state normalization, identity and capacity checks, stale/error transitions, real local HTTP requests, authentication, read-only methods, malformed request handling, bounded handlers, installer safety and the shipped Homepage field contract.

The suite passed from a fresh `git archive` extraction of commit [`e063ce8`](https://github.com/mglaeser/apple-raid-status/commit/e063ce8fa8b0051ec9f478e6b252bd44d4256d47), independent of the development working directory. The same runtime commit passed [GitHub Actions](https://github.com/mglaeser/apple-raid-status/actions/runs/38826604145) on the four configured combinations:

| Platform | Python |
| --- | --- |
| Ubuntu | 3.10 |
| Ubuntu | 3.14 |
| macOS | 3.10 |
| macOS | 3.14 |

CI uses synthetic fixtures and mocked storage/lifecycle commands. It does not require real RAID hardware or start a persistent native service.

## Homepage boundary

The shipped example's four field paths are tested against representative fresh and failed API observations. Its HTTP/authentication pattern follows Homepage's supported Custom API interface. The native service and a separate dashboard consumer were tested together; this release does **not** claim an end-to-end test inside a running Homepage installation.

For your deployment, follow the [Homepage verification steps](homepage.md#verify-and-troubleshoot). Rebuild and failure fixtures verify parser behavior without risking healthy storage; they do not prove how every OS/hardware combination formats a live rebuild.
