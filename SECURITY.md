# Security policy

Security fixes target the latest release. There is no guaranteed response time or support period.

## Reporting

Use [GitHub's private vulnerability reporting](https://github.com/mglaeser/apple-raid-status/security/advisories/new) for suspected security problems. Include the affected version, macOS/Python versions, impact, and a minimal reproduction. Do not include real API tokens, private host addresses, disk serials, or unredacted command output. Avoid public disclosure until the report has been reviewed.

## Deployment assumptions

- Run as an ordinary macOS user. The collector is read-only and does not need root privileges or raw-disk access.
- Loopback is the default bind. If you enable remote access, restrict reachability to intended clients.
- All observation and status-page routes require the API token. `/healthz` intentionally reveals only process liveness without authentication.
- HTTP Bearer and Basic credentials are not encrypted by HTTP. Use trusted private transport or an HTTPS reverse proxy/tunnel. This small service is not intended to be directly Internet-facing.
- The token authorizes reading all exposed observations, not an individual RAID. The API can reveal storage names, UUIDs, device identifiers, and capacity. There are no write endpoints.
- Keep `config.json` and `token` user-owned and mode `0600`. Keep populated environment files and copied tokens out of Git.
- Requests read a shared cache. Collection failures are reported explicitly; retained old values are not evidence of current health.

Rotate an exposed token by stopping the helper, replacing its private token file with a securely generated URL-safe value of 32–256 characters, restarting, and updating every consumer. Never put tokens in URLs or issue reports.
