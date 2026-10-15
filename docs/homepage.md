# Use with Homepage

AppleRAID Status integrates through Homepage's existing [Custom API service widget](https://gethomepage.dev/widgets/services/customapi/). It requires no Homepage fork, third-party JavaScript, plugin loader, Docker socket, or direct disk access.

## 1. Install on the Mac

Follow the [quick start](../README.md#quick-start). Select an explicit LAN IPv4 bind address if Homepage runs remotely or in a container. The collector must stay on macOS; Homepage may run on another platform.

Use the actual set UUID from `python3 apple_raid_status.py --once`. UUID selection survives BSD disk-number changes and avoids depending on a renameable volume name. Configure the same UUID with `--expect` so disappearance is visible in aggregate observations too.

## 2. Make the token available to Homepage

Homepage substitutes variables with its supported `HOMEPAGE_VAR_` and `HOMEPAGE_FILE_` prefixes. Choose one approach.

**Environment secret**: set `HOMEPAGE_VAR_APPLE_RAID_TOKEN` to the private contents of the Mac's `token` file in Homepage's environment. Reference it in `services.yaml`:

```yaml
headers:
  Authorization: "Bearer {{HOMEPAGE_VAR_APPLE_RAID_TOKEN}}"
```

For Docker Compose, an existing protected environment file can supply it:

```yaml
services:
  homepage:
    environment:
      HOMEPAGE_VAR_APPLE_RAID_TOKEN: ${HOMEPAGE_VAR_APPLE_RAID_TOKEN}
```

Do not commit a populated `.env` file or the token. Environment variables can be visible to administrators of the container runtime.

**File-backed secret**: securely copy the token to a file on the Homepage host, mount it read-only, and set `HOMEPAGE_FILE_APPLE_RAID_TOKEN` to the path **inside** Homepage's container:

```yaml
services:
  homepage:
    volumes:
      - /absolute/private/apple-raid-token:/run/secrets/apple-raid-token:ro
    environment:
      HOMEPAGE_FILE_APPLE_RAID_TOKEN: /run/secrets/apple-raid-token
```

Then use this header instead:

```yaml
headers:
  Authorization: "Bearer {{HOMEPAGE_FILE_APPLE_RAID_TOKEN}}"
```

Make the mounted file readable by Homepage's runtime user. It should contain only the token, without a trailing newline, because Homepage substitutes file contents verbatim. These snippets extend your existing Homepage deployment; they are not a complete Compose application.

The helper's own token file can end in a newline. To prepare a separate newline-free copy on the Mac before securely transferring it to the Homepage host:

```sh
raid_state="$HOME/Library/Application Support/apple-raid-status"
umask 077
printf '%s' "$(cat "$raid_state/token")" > "$HOME/apple-raid-token"
```

Protect that copy as a credential and remove it once no longer needed.

Source: [Homepage environment secrets](https://gethomepage.dev/installation/docker/#using-environment-secrets).

## 3. Add the service

Copy [examples/services.yaml](../examples/services.yaml), replace the address and UUID, and add its group or service to your existing Homepage `services.yaml`:

```yaml
- Storage:
    - AppleRAID:
        icon: mdi-harddisk
        description: macOS RAID observations
        href: http://192.0.2.10:8788/
        widget:
          type: customapi
          url: http://192.0.2.10:8788/api/v1/raids/11111111-1111-4111-8111-111111111111
          refreshInterval: 30000
          headers:
            Authorization: "Bearer {{HOMEPAGE_VAR_APPLE_RAID_TOKEN}}"
          mappings:
            - field: health
              label: State
              format: text
            - field: raid.members_summary
              label: Members
              format: text
            - field: raid.volume.free_bytes
              label: Free
              format: bytes
            - field: raid.rebuild_summary
              label: Rebuild
              format: text
```

This is one row of four blocks, matching Homepage's normal service-widget layout. The API's top-level `health` includes freshness and collection failures; do not replace it with raw `raid.status`, which can represent an older retained observation.

`Free` means APFS container free space when `raid.volume.scope` is `container`. Unknown free space remains unavailable. Rebuild text remains informative even when no numeric percentage is reported.

Homepage fetches the API through its backend. **The URL must be reachable from the Homepage server/container.** Its `href` opens in the user's browser and may require a separately reachable address. The helper's browser page accepts username `status` and the token as its password.

## Optional member list

Use `widgets:` instead of `widget:` if you want both the summary above and a second widget. Append this member widget to that list:

```yaml
- type: customapi
  url: http://192.0.2.10:8788/api/v1/raids/11111111-1111-4111-8111-111111111111
  refreshInterval: 30000
  headers:
    Authorization: "Bearer {{HOMEPAGE_VAR_APPLE_RAID_TOKEN}}"
  display: dynamic-list
  mappings:
    items: raid.members
    name: uuid
    label: status
    format: text
```

Members are named by stable member UUID, including when their BSD device is absent. This list shows the last collected raw member states; keep the summary's freshness-aware State block beside it. Newer Homepage versions support `dynamic-list`; use the four-field summary if your version does not.

## Verify and troubleshoot

1. Check `python3 install.py status` on the Mac.
2. Check `http://MAC_LAN_IP:8788/healthz` from the Homepage host or its container. This only proves reachability and process liveness.
3. Request `/readyz` with the token. A `200` proves a fresh successful collection; inspect `health` separately.
4. Request the exact `/api/v1/raids/UUID` URL with the token. Confirm `health`, `raid.members_summary`, `raid.volume.free_bytes`, and `raid.rebuild_summary`.
5. Reload Homepage and confirm the same four values.

| Symptom | Check |
| --- | --- |
| Connection refused or timeout | Helper running; selected Mac interface/address; firewall; route from Homepage container. Loopback inside a container is not the Mac. |
| `401` | Token matches; header begins `Bearer `; correct Homepage variable prefix; no newline in a file-backed secret. |
| `404` on a RAID URL | Correct UUID; set still present. Homepage displays its normal API error when a selected set disappears. The aggregate route provides a structured missing-set observation. |
| `error` / `stale` | Collector logs and `/readyz`; do not treat retained member/space values as fresh. |
| Free space unavailable | The OS did not provide usable capacity or the helper could not prove a unique filesystem/container relationship. |
| Rebuild percentage unavailable | OS supplied a rebuild state without an explicit percentage; the helper does not estimate one. |
| No conditional colors | Custom API does not support block highlighting. This is a Homepage limitation, not an API failure. |

The helper does not send alerts or supply long-term history. Homepage's widget presents current observations. Storage-management actions remain outside this integration.
