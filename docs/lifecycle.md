# Native service lifecycle

One user LaunchAgent owns this service. It uses the stable label
`io.github.mglaeser.apple-raid-status`, a private installation receipt and a named
executable. Monitoring authority remains read-only; service lifecycle controls are local
commands, not remote API endpoints. Optional storage eject is separately enabled.

## Command contract

| Command | Behavior |
| --- | --- |
| `install` | Stage the reviewed code, configuration, receipt, named launcher and plist. A fresh installation is disabled. Existing enabled/disabled intent and an explicit native disable are preserved. Does not load or start the job. |
| `start` | Persist `Disabled=false`, explicitly enable the exact job, load it if absent or request a start if loaded but inactive, then check readiness. |
| `stop` | Persist `Disabled=true`, disable the exact job and unload it. The stopped decision survives a later login even if the external launchd override is absent. |
| `restart` | An explicit stop followed by an explicit start, serialized as one lifecycle operation. |
| `status` | Read-only inspection of configured startup intent, native override and loaded job identity/state. Stopped is a valid state. |
| `check` | Require enabled intent, the expected running job/listener and a fresh successful authenticated API response. A ready collector can still report an unhealthy RAID. |
| `uninstall` | Stop and retire the owned LaunchAgent and named launcher; retain configuration, credential, installed source and logs. |

Mutating commands share a nonblocking lock for the stable user/label, including
when callers supply different state directories. Do not remove the lock file to
force progress. A process-held kernel lock is released when that process exits.
Unknown or conflicting native state fails inspection rather than being treated
as a missing job. Source, launcher and plist identity checks prevent adoption of
another executable under this label.

Updates stage a private `STATE/.install-transaction` journal for the installed
source, receipt, plist and owned launcher paths. Caught write failures attempt
rollback. If an interruption leaves the journal behind, rerun `install` from a
reviewed checkout with the same state directory. Recovery requires an unloaded
job and validates every destination against its recorded before/after digest
before restoring anything. Unknown changes retain the journal and fail with a
diagnostic; do not delete it to bypass validation. This is checked recovery,
not a guarantee that several filesystem writes are atomic across power loss.

The plist's strict boolean `Disabled` field records the operator's durable choice.
The native `launchctl` override is inspected separately. Ordinary installation
does not override a system disable; an explicit start is the deliberate resume
operation. A readiness failure does not silently undo that selected intent or
launch a second supervisor. Inspect the reported lifecycle state and private logs
before choosing the next operation.

## Named executable and Login Items

By default the launcher is `STATE/bin/AppleRAID Status`, where `STATE` is the
chosen state directory. A deployment may select another absolute user-owned path
with `install --launcher`. The launcher immediately executes the installed
lifecycle entry point, which runs the collector in the foreground for launchd.
Its other subcommands provide local lifecycle controls without a source checkout.

The plist uses this launcher as its executable, instead of naming the Python
interpreter directly. Apple documents that an unattributed unsigned legacy
helper is displayed using its executable name from `Program` or
`ProgramArguments`: [Apple's helper migration guidance](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos).
The stable launch label remains unchanged. A display-name correction is not a
reason to install another owner or reset the background-items database.

Verify the actual item in **System Settings → General → Login Items & Extensions**
after installation. macOS can retain historical display records; inspect the
loaded program/path to distinguish an old label from a second active service.

## Recovery boundary

An enabled job uses `KeepAlive=true`, `ThrottleInterval=30` and
`ProcessType=Standard`. Launchd restarts it after process exit. The named wrapper
does not detach, loop, poll process state or create another daemon. A temporarily
unavailable configured network address can prevent binding; launchd may retry
according to its throttle while the enabled intent remains selected.

Collector command deadlines and API freshness checks make collection failures
visible. They are not a watchdog that kills a running but hung process. An
intentional stop disables recovery. Login/FileVault unlock is still required;
neither a process-exit test nor a simulated missing override proves a full host
reboot or pre-login availability.

The installed launcher records an exact Python executable and digest. If a
package upgrade removes that executable, use a working supported Python with a
reviewed checkout's `install.py stop --state-dir STATE`, then reinstall and
explicitly start. This emergency stop validates the owned job without importing
the collector or requiring the retired interpreter. Do not edit the receipt to
silently adopt a different runtime.

## Upgrading from 0.1.0

Version 0.1.0 launched Python directly and only stopped the current login session.
Its plist also allowed a future login to start the service before an explicit
start command. Version 0.2.0 corrects these behaviors and separates lifecycle
status from readiness checks. The HTTP schema remains version 1.

1. Record whether the service should be running after the upgrade.
2. Use the **old installed** lifecycle code to stop its exact existing owner:

   ```sh
   raid_state="$HOME/Library/Application Support/apple-raid-status"
   python3 "$raid_state/app/install.py" stop --state-dir "$raid_state"
   ```

3. Obtain the reviewed new release. From that checkout, stage the update:

   ```sh
   python3 install.py install --state-dir "$raid_state"
   ```

   Add `--launcher "/absolute/user/path/My AppleRAID Status"` for a site-specific
   name/location. Existing bind settings, expected RAID UUIDs and token are
   preserved. A validated legacy installation is migrated only while unloaded;
   it is staged disabled. A leftover enabled override from the old session-only
   stop is not evidence that the operator wants it resumed; migration disables
   that exact owned job and verifies the native setting before returning.

4. If the service should run, explicitly resume it and check its consumers:

   ```sh
   python3 install.py start --state-dir "$raid_state"
   python3 install.py check --state-dir "$raid_state"
   ```

Use the same custom state directory throughout. If any ownership, source or
native-state check fails, retain the diagnostic evidence and correct the exact
discrepancy before continuing. Do not overwrite an unrecognized LaunchAgent or
stop unrelated services as part of this migration.

## Optional control policy

Install the reviewed 0.3.0 release through the existing stopped update workflow.
Ordinary install never enables controls, changes their allowlist, rotates either
token or clears an unresolved operation. The following exact commands only
change private policy/credentials; none calls diskutil or changes storage.
Replace the example UUID with the deliberately selected set identity.

**Run on:** the Mac owning the RAID — **as:** its ordinary installing user.

```sh
python3 install.py stop
python3 install.py controls-enable --allow-eject 11111111-1111-4111-8111-111111111111
python3 install.py start
```

Use the same `--state-dir` on every command when customized. Repeat
`--allow-eject` for up to eight explicitly admitted UUIDs. Enabling requires the
exact job unloaded and durable disabled intent. It creates a new private
`control-token` only when missing and refuses a token equal to the monitoring
secret. Repeating enable preserves the token but deliberately replaces the
allowlist. It never starts the helper automatically. Share this control token
only with an authorized backend; Homepage's existing token stays read-only.

Disable through explicit stop, `controls-disable`, then optional start. Disable
removes the optional policy but preserves both credentials and operation history.
Uninstall likewise retains them, including unresolved uncertainty.

An interrupted/partial/unknown eject is blocked after restart. First inspect the
native RAID/mount/member state on the host and resolve any ongoing storage work.
Only then acknowledge that inspection using the exact UUID:

**Run on:** the Mac owning the RAID — **as:** its ordinary installing user.

```sh
python3 install.py stop
python3 install.py controls-clear --confirm 11111111-1111-4111-8111-111111111111
python3 install.py start
```

Acknowledgement retains request IDs and does not retry an action, mount storage,
change membership, or certify safe disconnection. A future POST repeats fresh
eligibility checks and needs a new request ID. Do not edit/delete the journal to
bypass uncertainty. The next regular collector sample reports intentionally
ejected storage as missing; ejection does not rewrite health thresholds or hide
an expected RAID.
