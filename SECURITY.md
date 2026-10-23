# Security policy

Security fixes target the latest release. There is no guaranteed response time or support period.

## Reporting

Use [GitHub's private vulnerability reporting](https://github.com/mglaeser/apple-raid-status/security/advisories/new) for suspected security problems. Include the affected version, macOS/Python versions, impact, and a minimal reproduction. Do not include real API tokens, private host addresses, disk serials, or unredacted command output. Avoid public disclosure until the report has been reviewed.

## Deployment assumptions

- Run as an ordinary macOS user. The collector is read-only and does not need root privileges or raw-disk access.
- Loopback is the default bind. If you enable remote access, restrict reachability to intended clients.
- All observation and status-page routes require the API token. `/healthz` intentionally reveals only process liveness without authentication.
- HTTP Bearer and Basic credentials are not encrypted by HTTP. Use trusted private transport or an HTTPS reverse proxy/tunnel. This small service is not intended to be directly Internet-facing.
- The token authorizes reading all exposed observations, not an individual RAID. The API can reveal storage names, UUIDs, device identifiers, and capacity. It never authorizes eject, including when controls are enabled.
- Keep `config.json`, `token`, optional `control-token`, and `control-state.json` user-owned and mode `0600`. Keep populated environment files and copied tokens out of Git.
- Requests read a shared cache. Collection failures are reported explicitly; retained old values are not evidence of current health.

Rotate an exposed token by stopping the helper, replacing its private token file with a securely generated URL-safe value of 32–256 characters, restarting, and updating every consumer. Never put tokens in URLs or issue reports.

## Optional eject authority

Control is disabled unless the stopped local enable command admits explicit RAID
UUIDs and creates a distinct control token. The monitoring credential must never
be promoted or copied into the control slot. Control Bearer credentials are only
for a trusted backend or operator client: they do not authenticate the monitoring
routes and cannot use browser Basic authentication. Mutation accepts one exact
JSON POST, a canonical UUID confirmation and a unique UUID `Idempotency-Key`.
Requests with Origin or Fetch Metadata headers, form bodies, duplicate fields,
transfer encoding, unknown fields and other verbs are rejected. No CORS support,
arbitrary command/path input, sudo escalation or general storage API is added.

A control token grants the ability to deliberately disconnect the admitted
storage, including forced unmounts that can lose unsaved work. Protect its
transport and keep it out of Homepage widgets and browser-visible responses.
Force never bypasses rebuild, identity or topology guards. Only an external,
healthy, two-member APFS Mirror with unambiguous dedicated GPT member disks is
admitted. The checks reduce risk; user-space reads cannot make external storage
changes atomic or stop another local administrator modifying disks concurrently.

A private fsynced journal records intent before the first mutation and blocks
further operations after interruption, unknown outcome or partial ejection.
Preparation, disable, restart and uninstall retain it. The stopped
`controls-clear --confirm UUID` operation acknowledges host inspection; it does
not retry storage work. Never clear it merely to make a failing button run again.
Logs contain bounded outcome/reason/operation IDs, never command output or tokens.
