# AppleRAID Status

[![Tests](https://github.com/mglaeser/apple-raid-status/actions/workflows/test.yml/badge.svg)](https://github.com/mglaeser/apple-raid-status/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A small, read-only HTTP service for macOS AppleRAID. See RAID state, member availability, capacity, and available rebuild progress in [Homepage](https://gethomepage.dev/) or any dashboard that consumes JSON.

The collector runs **natively on the Mac that owns the RAID**. Homepage can run elsewhere, including in a container, and retrieve observations over HTTP. There are no Python package dependencies, database, cloud account, or privileged disk operations.

```text
macOS diskutil → cached AppleRAID observations → authenticated HTTP API
                                                        ↓
                                             Homepage / your dashboard
```

## What you get

- Set and member states, online/total member count, RAID capacity and automatic rebuild policy.
- APFS container capacity/free/used space, linked to the RAID's physical store; supported legacy filesystem space where reported.
- Rebuild progress **only when diskutil explicitly reports a percentage**. Otherwise the API says that progress is unavailable.
- Freshness timestamps, stale/error states, expected-RAID disappearance detection, and separate liveness/readiness endpoints.
- Copy-and-paste configuration for Homepage's built-in **Custom API** widget. No custom Homepage build or plugin installation.
- A minimal authenticated status page and a per-user `launchd` installer.

This reports the operating system's observations. It does **not** provide SMART attributes, temperatures, filesystem verification, backup verification, history, alerts, repair controls, or a rebuild ETA. An online RAID is not proof that every disk or file is healthy.

## Requirements

- macOS with AppleRAID and `/usr/sbin/diskutil`.
- **Python 3.10 or newer**, installed separately and available as `python3`. Do not assume macOS includes a suitable Python. A Homebrew installation is one option: `brew install python`.
- An ordinary logged-in macOS user who can read `diskutil` status. Do not run the service as root.
- For remote consumers: a reachable IPv4 address and the generated API token.

The portable test suite runs on macOS and Linux. Actual collection requires macOS; Linux containers cannot inspect the host's AppleRAID through this tool. Resource use depends on the host and number of RAID sets; no fixed RAM or CPU guarantee is claimed.

In the initial Apple Silicon deployment, the helper used **14–16 MiB resident memory** and showed **0.0% CPU in idle samples**. These observations exclude transient diskutil processes and are not resource limits. See [release validation](docs/validation.md) for the tested environment and limits.

## Quick start

Download the tagged source using Git:

```sh
git clone --branch v0.2.0 --depth 1 https://github.com/mglaeser/apple-raid-status.git
cd apple-raid-status
python3 apple_raid_status.py --once
```

This path requires Git; macOS may offer to install Apple's Command Line Tools if Git is not installed. The collector itself uses only Python's standard library and the native diskutil command.

The last command prints one local observation and exits. It opens no listening port and makes no storage changes. Find the wanted set's `uuid` in its output; use that stable UUID instead of a disk number or volume name.

Install, then start the service, replacing the example UUID:

```sh
python3 install.py install --bind 127.0.0.1 --port 8788 \
  --expect 11111111-1111-4111-8111-111111111111
python3 install.py start
python3 install.py status
python3 install.py check
```

The default state directory is `~/Library/Application Support/apple-raid-status`. The installer creates private configuration and a random token there. A fresh installation stays disabled until explicit `start`; repeating installation preserves existing start/stop intent. Use `--state-dir /absolute/path` consistently on every installer command to choose another location.

Read the authenticated JSON API locally:

```sh
raid_state="$HOME/Library/Application Support/apple-raid-status"
curl --fail --silent --show-error \
  -H "Authorization: Bearer $(cat "$raid_state/token")" \
  http://127.0.0.1:8788/api/v1/status
```

For a browser, open `http://127.0.0.1:8788/` and authenticate with username **`status`** and the token as the password. Keep that token private; both authentication methods grant the same read-only access.

## Connect Homepage

If Homepage runs on another machine or inside a container, choose the Mac's appropriate LAN IPv4 address instead of loopback for a **new installation**:

```sh
python3 install.py install --bind 192.0.2.10 --port 8788 \
  --expect 11111111-1111-4111-8111-111111111111
python3 install.py start
```

`192.0.2.10` and the UUID are documentation examples; replace both. From a container, `127.0.0.1` refers to that container, not the Mac. Use a Mac address reachable from the Homepage server.

Already installed on loopback? Stop the helper, edit only `bind` in its private `config.json`, then start it again. Reinstalling preserves existing configuration and refuses conflicting installation options; it does not silently change the listening address.

```sh
python3 install.py stop
nano "$HOME/Library/Application Support/apple-raid-status/config.json"
python3 install.py start
```

In the editor, replace `127.0.0.1` with the Mac's intended LAN IPv4 address, save, and exit. Use your chosen state directory if you changed the default.

Add the configuration in [examples/services.yaml](examples/services.yaml) to Homepage, set its secret, and replace the example address/UUID. The four fields are **State · Members · Free · Rebuild**. See [the Homepage guide](docs/homepage.md) for environment secrets, file-backed secrets, optional member details, and troubleshooting.

HTTP does not encrypt credentials or observations. Keep direct HTTP on a trusted private connection; use an HTTPS reverse proxy or private tunnel where encryption is needed. The helper does not install a reverse proxy or expose a public port automatically.

## Service lifecycle

The installer creates the executable `bin/AppleRAID Status` inside the state directory. Use this installed launcher for normal lifecycle operations; no Git checkout is needed:

```sh
raid_helper="$HOME/Library/Application Support/apple-raid-status/bin/AppleRAID Status"
"$raid_helper" status
"$raid_helper" check
"$raid_helper" stop
"$raid_helper" start
"$raid_helper" restart
```

`status` reports saved startup intent, the native disabled override and the loaded job. An intentionally stopped service is a valid status. `check` additionally requires an enabled, running service and a fresh successful collection; it fails when the helper is stopped or not ready. RAID health is reported separately from collector readiness.

The stable launch label remains `io.github.mglaeser.apple-raid-status`. Explicit `start` saves enabled intent; `stop` saves disabled intent and unloads the exact job. **A stopped helper stays stopped at subsequent logins until explicitly started.** An enabled helper starts when its user logs in, not before login or FileVault unlock. Launchd alone handles process-exit recovery with a 30-second throttle. It does not restart a running but unresponsive process.

For a site-specific display name, choose the full user-owned executable path with `install --launcher "/absolute/path/My AppleRAID Status"`. The named executable is the job's program; its shell immediately replaces itself with the installed Python lifecycle entry point. This avoids displaying a generic Python interpreter as the background item's identity. The stable label and HTTP API do not change. See [lifecycle, naming and migration](docs/lifecycle.md) for exact semantics and upgrade steps.

`python3 install.py uninstall` stops the helper and removes its owned LaunchAgent and launcher while retaining private configuration, token, installed source and logs. Use the chosen `--state-dir` when customized. Remove retained state manually only when it is no longer needed.

Defaults are a **30-second polling interval**, a **10-second timeout per diskutil command**, and a **90-second stale threshold**. Collection also has a **30-second overall deadline**. Requests read cached data and do not trigger additional disk probes. Collection and HTTP serving are separate, so a slow probe does not block the API.

Configuration is `config.json`, and the API secret is `token`, in the state directory. Both must be owned by the service user with mode `0600`; symlinked private paths are rejected. See [configuration and API reference](docs/API.md) before editing. Restart after configuration changes.

Collector diagnostics are in `collector.log` in the same directory, rotated at 256 KiB with one backup. Launch-time errors go to `service.log`. The installer records the Python interpreter and installed-source checksums in `receipt.json`. If a Python upgrade removes that interpreter, stop the old service and reinstall with a supported `python3` before starting the helper.

To upgrade, stop the service using its installed launcher, obtain a reviewed release, and rerun that release's `python3 install.py install` using the same `--state-dir` if customized. Omit bind/port/expected-set options to preserve the existing settings. Explicitly start it when intended, then run `check` and verify the dashboard. **Upgrading from 0.1.0 needs the documented legacy stop step:** that release had no installed launcher and its stop did not persist across login. Follow [the migration instructions](docs/lifecycle.md#upgrading-from-010). There is no automatic update or rollback service.

## Documentation and development

- [API and configuration](docs/API.md)
- [Lifecycle, naming and migration](docs/lifecycle.md)
- [Homepage setup](docs/homepage.md)
- [Requirements, design decisions, and alternatives](docs/design.md)
- [Release validation and measured footprint](docs/validation.md)
- [Contributing and tests](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

```sh
python3 -m unittest discover -s tests -v
```

This is an independent project, not an Apple or Homepage product and not an upstream Homepage widget. Released under the [MIT License](LICENSE).
