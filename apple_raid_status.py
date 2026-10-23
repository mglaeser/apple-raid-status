#!/usr/bin/env python3
"""AppleRAID observations with separately authorized optional eject controls."""

import argparse
import base64
import copy
import datetime as dt
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import uuid

VERSION = "0.3.1"
DISK = re.compile(r"disk\d+(?:s\d+)*\Z")
PROGRESS = re.compile(r"(\d+(?:\.\d+)?)%\s*\((?:Rebuilding|Resyncing)\)\Z", re.I)
LOG = logging.getLogger("apple-raid-status")


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def identifier(value):
    return str(uuid.UUID(value)).upper()


def bounded_text(value, field):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid " + field)
    return value


def device(value):
    if value is not None and (not isinstance(value, str) or not DISK.fullmatch(value)):
        raise ValueError("invalid device identifier")
    return value


def natural(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def member_health(state):
    if state == "Online":
        return "healthy"
    if state == "Missing":
        return "missing"
    if state in ("Failed", "Degraded"):
        return "degraded"
    if state == "Offline":
        return "offline"
    if state in ("Rebuilding", "Resyncing") or PROGRESS.fullmatch(state):
        return "rebuilding"
    return "unknown"


def normalize_member(raw, mirror_size=None):
    state = bounded_text(raw.get("MemberStatus"), "member status")
    health = member_health(state)
    match = PROGRESS.fullmatch(state)
    percent = float(match[1]) if match and 0 <= float(match[1]) <= 100 else None
    if match is None and health == "rebuilding":
        rebuilt = natural(raw.get("MemberRebuildStatus"))
        total = natural(mirror_size)
        # Mirror progress is a byte offset, not a percentage. An explicit
        # percentage above remains authoritative, including invalid values.
        if rebuilt is not None and total is not None and 0 < total and rebuilt <= total:
            percent = rebuilt * 100 / total
    bsd = device(raw.get("BSD Name"))
    if state == "Online" and not bsd:
        raise ValueError("online member has no device")
    return {"uuid": identifier(raw["AppleRAIDMemberUUID"]),
            "device": bsd, "status": state,
            "health": health, "rebuild_percent": percent}


def unavailable_volume(status="unavailable"):
    return {"status": status, "filesystem": None, "scope": None, "mounted": None,
            "total_bytes": None, "free_bytes": None, "used_bytes": None,
            "used_percent": None, "volumes": []}


def normalize_raid(raw):
    members = raw.get("Members")
    spares = raw.get("Spares", [])
    if not isinstance(members, list) or not isinstance(spares, list) or len(members) + len(spares) > 128:
        raise ValueError("invalid RAID membership")
    state = bounded_text(raw.get("Status"), "RAID status")
    level = bounded_text(raw.get("Level"), "RAID type")
    size = natural(raw.get("Size"))
    members = [normalize_member(m, size if level == "Mirror" else None) for m in members]
    spares = [normalize_member(m) for m in spares]
    ids = [m["uuid"] for m in members + spares]
    devices = [m["device"] for m in members + spares if m["device"] is not None]
    if len(ids) != len(set(ids)) or len(devices) != len(set(devices)):
        raise ValueError("duplicate RAID membership")
    online = sum(m["health"] == "healthy" for m in members)
    rebuilding = any(m["health"] == "rebuilding" for m in members) or state in ("Rebuilding", "Resyncing")
    health = "unknown"
    if state in ("Offline", "Failed", "Missing"):
        health = "offline"
    elif rebuilding:
        health = "rebuilding"
    elif state == "Degraded" or any(m["health"] in ("degraded", "missing", "offline") for m in members):
        health = "degraded"
    elif (state == "Online" and level in ("Mirror", "Stripe", "Concat", "Concatenated")
          and len(members) >= (2 if level == "Mirror" else 1) and online == len(members)
          and size is not None and size > 0 and raw.get("BSD Name")):
        health = "healthy"
    progress = [m["rebuild_percent"] for m in members if m["health"] == "rebuilding"]
    progress = min(progress) if progress and all(p is not None for p in progress) else None
    policy = raw.get("Rebuild")
    return {"uuid": identifier(raw["AppleRAIDSetUUID"]), "name": bounded_text(raw.get("Name"), "RAID name"),
            "device": device(raw.get("BSD Name")), "type": level, "status": state,
            "health": health, "healthy": health == "healthy", "capacity_bytes": size,
            "auto_rebuild": True if policy == "Automatic" else False if policy == "Manual" else None,
            "member_count": len(members), "online_members": online,
            "members_summary": f"{online}/{len(members)}", "rebuilding": rebuilding,
            "rebuild_percent": progress,
            "rebuild_summary": (f"{progress:g}%" if progress is not None else "In progress; percentage unavailable")
                               if rebuilding else "Not rebuilding" if health == "healthy" else "Unknown",
            "members": members, "spares": spares, "volume": unavailable_volume()}


def missing_raid(raid_uuid):
    return {"uuid": raid_uuid, "name": "Expected RAID not found", "device": None, "type": "Unknown",
            "status": "Missing", "health": "missing", "healthy": False, "capacity_bytes": None,
            "auto_rebuild": None, "member_count": 0, "online_members": 0, "members_summary": "0/0",
            "rebuilding": False, "rebuild_percent": None, "rebuild_summary": "Unknown",
            "members": [], "spares": [], "volume": unavailable_volume()}


def diskutil(arguments, timeout=10):
    """All callers supply fixed read-only verbs and validated BSD identifiers."""
    result = subprocess.run(["/usr/sbin/diskutil", *arguments], capture_output=True,
                            timeout=timeout, check=True, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("diskutil response too large")
    data = plistlib.loads(result.stdout)
    if not isinstance(data, dict) or data.get("Error"):
        raise ValueError("invalid diskutil response")
    return data


def volume_for(raid, containers, run):
    matches = [c for c in containers if any(s.get("DeviceIdentifier") == raid["device"]
                                           for s in c.get("PhysicalStores", []))]
    if not matches:
        # Legacy HFS+ AppleRAID: ask only the RAID device, never a fallback path.
        if not raid["device"]:
            return unavailable_volume()
        info = run(["info", "-plist", raid["device"]])
        total, free = natural(info.get("TotalSize")), natural(info.get("VolumeFreeSpace"))
        if info.get("DeviceIdentifier") != raid["device"] or not info.get("FilesystemType") or total is None or free is None:
            return unavailable_volume()
        entries = [{"name": info.get("VolumeName", ""), "device": raid["device"], "mounted": bool(info.get("MountPoint"))}]
        filesystem, scope = info["FilesystemType"], "volume"
    else:
        if len(matches) != 1 or len(matches[0].get("PhysicalStores", [])) != 1:
            return unavailable_volume("ambiguous")
        container = matches[0]
        total, free = natural(container.get("CapacityCeiling")), natural(container.get("CapacityFree"))
        entries = []
        volumes = container.get("Volumes", [])
        if not isinstance(volumes, list) or len(volumes) > 64:
            raise ValueError("invalid APFS volumes")
        for raw in volumes:
            bsd = device(raw.get("DeviceIdentifier"))
            if not bsd:
                raise ValueError("missing APFS volume identifier")
            info = run(["info", "-plist", bsd])
            if (info.get("DeviceIdentifier") != bsd
                    or info.get("APFSContainerReference") != container.get("ContainerReference")):
                raise ValueError("APFS identity changed during collection")
            entries.append({"name": bounded_text(raw.get("Name"), "volume name"), "device": bsd,
                            "mounted": bool(info.get("MountPoint"))})
        filesystem, scope = "apfs", "container"
    if total is None or free is None or total <= 0 or free > total:
        return unavailable_volume()
    return {"status": "available", "filesystem": filesystem, "scope": scope,
            "mounted": any(v["mounted"] for v in entries), "total_bytes": total,
            "free_bytes": free, "used_bytes": total - free,
            "used_percent": round((total - free) * 100 / total, 2), "volumes": entries}


def collect(expected=(), timeout=10, run=None):
    deadline = time.monotonic() + 30
    supplied = run

    def run(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("collection", 30)
        result = supplied(args) if supplied else diskutil(args, min(timeout, remaining))
        if time.monotonic() > deadline:
            raise subprocess.TimeoutExpired("collection", 30)
        return result
    raw = run(["appleRAID", "list", "-plist"])
    sets = raw.get("AppleRAIDSets")
    if not isinstance(sets, list) or len(sets) > 64:
        raise ValueError("invalid AppleRAID set list")
    raids = [normalize_raid(r) for r in sets]
    ids = [r["uuid"] for r in raids]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate AppleRAID identity")
    if raids:
        containers = run(["apfs", "list", "-plist"]).get("Containers")
        if not isinstance(containers, list):
            raise ValueError("invalid APFS container list")
        for raid in raids:
            raid["volume"] = volume_for(raid, containers, run)
    raids.extend(missing_raid(r) for r in expected if r not in ids)
    return raids


# Controls are a separately authorized, opt-in boundary. Monitoring credentials
# and cached observations never authorize an operation.
class ControlRefusal(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def control_require(condition, reason="unsupported_topology"):
    if not condition:
        raise ControlRefusal(reason)


def control_uuid(value):
    parsed = identifier(value)
    if not isinstance(value, str) or value.lower() != parsed.lower():
        raise ValueError("expected canonical UUID")
    return parsed


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def control_policy(config):
    policy = config.get("controls", {"enabled": False, "raids": []})
    if (not isinstance(policy, dict) or set(policy) != {"enabled", "raids"}
            or type(policy["enabled"]) is not bool or not isinstance(policy["raids"], list)
            or len(policy["raids"]) > 8):
        raise ValueError("invalid controls policy")
    ids = [identifier(value) for value in policy["raids"]]
    if len(ids) != len(set(ids)) or (policy["enabled"] and not ids) or (not policy["enabled"] and ids):
        raise ValueError("invalid control RAID allowlist")
    return {"enabled": policy["enabled"], "raids": ids}


def load_control_token(path, config, monitoring_token):
    if not control_policy(config)["enabled"]:
        return None
    secret = Path(path).parent / "control-token"
    token = private_file(secret).strip()
    if secret.stat().st_nlink != 1:
        raise ValueError("control token has multiple links")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) or hmac.compare_digest(token, monitoring_token):
        raise ValueError("control token must be distinct from monitoring token")
    return token


def action_command(arguments, timeout=65):
    """Fixed argv only; discard bounded output, never parse localized messages.

    A timeout/overrun means unknown native outcome even after killing diskutil.
    Kernel storage work can outlive its CLI process.
    """
    import selectors
    command = list(arguments)
    if (len(command) not in (2, 3) or command[0] not in ("unmountDisk", "eject")
            or (len(command) == 3 and command[1] != "force") or not DISK.fullmatch(command[-1])):
        raise ValueError("invalid storage action")
    process = subprocess.Popen(["/usr/sbin/diskutil", *command], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                               env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    deadline, size = time.monotonic() + timeout, 0
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("storage action", timeout)
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                size += len(chunk)
                if size > 65536:
                    raise RuntimeError("storage action output exceeded limit")
        return process.wait(timeout=max(0.01, deadline - time.monotonic()))
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass  # Kernel work may remain; caller receives unknown outcome.
        process.stdout.close()


def control_info(run, bsd):
    control_require(isinstance(bsd, str) and DISK.fullmatch(bsd))
    data = run(["info", "-plist", bsd])
    control_require(data.get("DeviceIdentifier") == bsd, "identity_changed")
    return data


def control_external(info):
    control_require(info.get("Internal") is False and info.get("OSInternalMedia") is False
                    and info.get("SystemImage") is False and info.get("Ejectable") is True)


def member_plan(run, raw, raid_uuid):
    bsd, member_uuid = device(raw.get("BSD Name")), identifier(raw["AppleRAIDMemberUUID"])
    info = control_info(run, bsd)
    control_external(info)
    control_require(info.get("WholeDisk") is False and info.get("RAIDSlice") is True
                    and info.get("RAIDMaster") is False and info.get("Content") == "Apple_RAID"
                    and identifier(info.get("RAIDSetUUID")) == raid_uuid
                    and identifier(info.get("RAIDMemberUUID")) == member_uuid
                    and info.get("RAIDMemberStatus") == "Online"
                    and info.get("RAIDSetStatus") in ("Online", "Degraded")
                    and info.get("RAIDSetLevelType") == "Mirror"
                    and not info.get("MountPoint"))
    whole = info.get("ParentWholeDisk")
    control_require(isinstance(whole, str) and re.fullmatch(r"disk\d+", whole))
    # Native list filters apply at whole-disk level, including explicit devices.
    # Full whole-disk info also probes low-level formatting support and can block
    # opening active RAID media. Partition info retains the identity/safety checks.
    inventory = run(["list", "-plist", "external", "physical", whole])
    disks = inventory.get("AllDisksAndPartitions")
    control_require(inventory.get("WholeDisks") == [whole]
                    and isinstance(disks, list) and len(disks) == 1
                    and disks[0].get("DeviceIdentifier") == whole
                    and disks[0].get("Content") == "GUID_partition_scheme"
                    and disks[0].get("OSInternal") is False)
    tree = bounded_text(info.get("DeviceTreePath"), "device tree")
    size = natural(disks[0].get("Size"))
    control_require(size is not None and size > 0)
    partitions = disks[0].get("Partitions")
    control_require(isinstance(partitions, list) and 2 <= len(partitions) <= 3)
    seen, identities, efi, boot, member = set(), [], 0, 0, 0
    for part in partitions:
        part_device = part.get("DeviceIdentifier")
        control_require(part_device not in seen)
        seen.add(part_device)
        detail = control_info(run, part_device)
        control_external(detail)
        content = part.get("Content")
        control_require(detail.get("ParentWholeDisk") == whole and detail.get("WholeDisk") is False
                        and detail.get("Content") == content and not detail.get("MountPoint"))
        partition_uuid = identifier(part.get("DiskUUID"))
        control_require(identifier(detail.get("DiskUUID")) == partition_uuid, "identity_changed")
        if part_device == bsd and content == "Apple_RAID":
            member += 1
        elif content == "EFI" and detail.get("RAIDSlice") is False:
            efi += 1
        elif (content == "Apple_Boot" and part_device == info.get("BooterDeviceIdentifier")
              and detail.get("RAIDSlice") is False):
            boot += 1
        else:
            raise ControlRefusal("unrelated_partitions")
        identities.append((part_device, partition_uuid, content))
    control_require(member == 1 and efi == 1 and boot <= 1
                    and (not info.get("BooterDeviceIdentifier") or boot == 1))
    all_disks = inventory.get("AllDisks")
    control_require(isinstance(all_disks, list) and len(all_disks) == len(seen) + 1
                    and all(isinstance(item, str) for item in all_disks)
                    and set(all_disks) == seen | {whole})
    return {"device": whole, "member_device": bsd, "member_uuid": member_uuid,
            "tree": tree, "size": size, "partitions": sorted(identities)}


def eject_plan(run, raid_uuid):
    sets = run(["appleRAID", "list", "-plist"]).get("AppleRAIDSets")
    control_require(isinstance(sets, list) and len(sets) <= 64)
    matches = [item for item in sets if identifier(item.get("AppleRAIDSetUUID")) == raid_uuid]
    control_require(len(matches) == 1, "raid_not_found")
    raw = matches[0]
    raid = normalize_raid(raw)
    control_require(not raid["rebuilding"], "rebuild_in_progress")
    control_require(raid["type"] == "Mirror" and raid["status"] == "Online" and raid["healthy"]
                    and len(raid["members"]) == 2 and not raid["spares"], "raid_not_healthy")
    logical = control_info(run, raid["device"])
    control_external(logical)
    control_require(logical.get("RAIDMaster") is True and identifier(logical.get("RAIDSetUUID")) == raid_uuid)
    members = [member_plan(run, item, raid_uuid) for item in raw["Members"]]
    control_require(len({m["device"] for m in members}) == 2)
    all_member_devices = {m["member_device"] for m in members}
    for other in sets:
        if identifier(other.get("AppleRAIDSetUUID")) != raid_uuid:
            control_require(not any(item.get("BSD Name") in all_member_devices
                                    for item in other.get("Members", []) + other.get("Spares", [])))
    containers = run(["apfs", "list", "-plist"]).get("Containers")
    control_require(isinstance(containers, list))
    matches = [item for item in containers if any(store.get("DeviceIdentifier") == raid["device"]
                                                for store in item.get("PhysicalStores", []))]
    control_require(len(matches) == 1 and len(matches[0].get("PhysicalStores", [])) == 1)
    container = matches[0]
    container_uuid = identifier(container.get("APFSContainerUUID"))
    volumes = container.get("Volumes")
    control_require(isinstance(volumes, list) and 1 <= len(volumes) <= 16)
    volume_plan = []
    for volume in volumes:
        # Bootable/system volumes are excluded. Backup is the only admitted role.
        roles = volume.get("Roles")
        control_require(isinstance(roles, list) and roles in ([], ["Backup"]))
        control_require(volume.get("CryptoMigrationOn") is False and volume.get("Locked") is False)
        info = control_info(run, volume.get("DeviceIdentifier"))
        control_external(info)
        control_require(info.get("APFSContainerReference") == container.get("ContainerReference")
                        and identifier(info.get("VolumeUUID")) == identifier(volume.get("APFSVolumeUUID")))
        volume_plan.append({"device": volume["DeviceIdentifier"], "uuid": identifier(volume.get("APFSVolumeUUID")),
                            "roles": roles})
    control_require(len({v["uuid"] for v in volume_plan}) == len(volume_plan))
    # Last read checks that identities/status did not move while assembling the plan.
    current = run(["appleRAID", "list", "-plist"]).get("AppleRAIDSets", [])
    current = [item for item in current if identifier(item.get("AppleRAIDSetUUID")) == raid_uuid]
    control_require(len(current) == 1 and normalize_raid(current[0]) == raid, "identity_changed")
    return {"uuid": raid_uuid, "device": raid["device"], "container_uuid": container_uuid,
            "container": container["ContainerReference"], "volumes": volume_plan,
            "members": sorted(members, key=lambda m: m["device"])}


def verify_eject_state(run, plan, ejected):
    """Rebuilds are independent of filesystem mounts; check at each boundary."""
    sets = run(["appleRAID", "list", "-plist"]).get("AppleRAIDSets")
    control_require(isinstance(sets, list) and len(sets) <= 64, "verification_unavailable")
    expected = {m["member_uuid"]: m for m in plan["members"]}
    matching = []
    for raw in sets:
        current_uuid = identifier(raw.get("AppleRAIDSetUUID"))
        if current_uuid == plan["uuid"]:
            matching.append(raw)
        else:
            control_require(not any(identifier(m.get("AppleRAIDMemberUUID")) in expected
                                    for m in raw.get("Members", []) + raw.get("Spares", [])), "identity_changed")
    # The virtual set may disappear as physical members are ejected. It may not
    # disappear before the first physical operation or be replaced/reinterpreted.
    if not matching:
        control_require(ejected > 0, "identity_changed")
        return
    control_require(len(matching) == 1, "identity_changed")
    current = normalize_raid(matching[0])
    control_require(not current["rebuilding"], "rebuild_in_progress")
    control_require(current["type"] == "Mirror" and not current["spares"]
                    and {m["uuid"] for m in current["members"]} == set(expected), "identity_changed")
    if ejected == 0:
        control_require(current["healthy"] and current["device"] == plan["device"], "raid_not_healthy")
    else:
        control_require(current["status"] in ("Online", "Degraded"), "raid_not_healthy")
    remaining = {m["member_uuid"] for m in plan["members"][ejected:]}
    for member in current["members"]:
        if member["uuid"] in remaining:
            control_require(member["device"] == expected[member["uuid"]]["member_device"]
                            and member["health"] == "healthy", "identity_changed")


def verify_unmounted(run, plan, allow_missing=False):
    containers = run(["apfs", "list", "-plist"]).get("Containers")
    control_require(isinstance(containers, list))
    matching = [c for c in containers if identifier(c.get("APFSContainerUUID")) == plan["container_uuid"]]
    if allow_missing and not matching:
        return
    control_require(len(matching) == 1, "identity_changed")
    container = matching[0]
    control_require(container.get("ContainerReference") == plan["container"]
                    and [s.get("DeviceIdentifier") for s in container.get("PhysicalStores", [])] == [plan["device"]],
                    "identity_changed")
    volumes = container.get("Volumes", [])
    expected = {v["uuid"]: v for v in plan["volumes"]}
    control_require({identifier(v.get("APFSVolumeUUID")) for v in volumes} == set(expected), "identity_changed")
    for volume in volumes:
        old = expected[identifier(volume.get("APFSVolumeUUID"))]
        control_require(volume.get("DeviceIdentifier") == old["device"] and volume.get("Roles") == old["roles"],
                        "identity_changed")
        info = control_info(run, old["device"])
        control_require(info.get("APFSContainerReference") == plan["container"]
                        and identifier(info.get("VolumeUUID")) == old["uuid"], "identity_changed")
        control_require(not info.get("MountPoint"), "volumes_still_mounted")


class EjectControls:
    def __init__(self, config, token, state_dir, lock, run=None, mutate=None):
        self.policy, self.token = control_policy(config), token
        self.state_dir, self.lock = Path(state_dir), lock
        if any(p.is_symlink() for p in (self.state_dir, *self.state_dir.parents)):
            raise ValueError("control state path contains symlinks")
        info = self.state_dir.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("control state directory must be private and user-owned")
        self.run = run or diskutil
        self.mutate = mutate or action_command
        self.journal = self.state_dir / "control-state.json"

    def _load(self):
        if not self.journal.exists() and not self.journal.is_symlink():
            return {"schema_version": 1, "requests": []}
        data = strict_json(private_file(self.journal))
        if self.journal.stat().st_nlink != 1:
            raise ValueError("control journal has multiple links")
        if (not isinstance(data, dict) or set(data) != {"schema_version", "requests"}
                or data["schema_version"] != 1 or not isinstance(data["requests"], list) or len(data["requests"]) > 128):
            raise ValueError("invalid control journal")
        for record in data["requests"]:
            if (not isinstance(record, dict) or set(record) != {"id", "raid", "time", "state", "outcome"}
                    or record["state"] not in ("started", "blocked", "finished")
                    or type(record["time"]) not in (float, int) or not 0 < record["time"] < 2**53
                    or record["outcome"] not in (None, "ejected", "refused", "partial", "unknown")):
                raise ValueError("invalid control journal record")
            identifier(record["id"]); identifier(record["raid"])
        return data

    def _save(self, data):
        import tempfile
        # Same-directory atomic private journal; never logs tokens or native output.
        if self.journal.exists() or self.journal.is_symlink():
            private_file(self.journal)
        fd, temp = tempfile.mkstemp(prefix=".control-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, separators=(",", ":"), allow_nan=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, self.journal)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _allowed(self, raid_uuid):
        control_require(self.policy["enabled"] and self.token is not None, "controls_disabled")
        control_require(raid_uuid in self.policy["raids"], "raid_not_allowed")
        control_require(os.getuid() != 0, "root_forbidden")

    def eligibility(self, raid_uuid):
        if not self.lock.acquire(blocking=False):
            return {"eligible": False, "reason": "operation_busy"}
        try:
            self._allowed(raid_uuid)
            data = self._load()
            control_require(not any(r["raid"] == raid_uuid and r["state"] != "finished" for r in data["requests"]),
                            "inspection_required")
            plan = self._plan(raid_uuid)
            return {"eligible": True, "reason": None, "members": len(plan["members"]),
                    "volumes": len(plan["volumes"]), "force_supported": True}
        except ControlRefusal as error:
            return {"eligible": False, "reason": error.reason}
        except Exception:
            return {"eligible": False, "reason": "preflight_unavailable"}
        finally:
            self.lock.release()

    def _plan(self, raid_uuid):
        deadline = time.monotonic() + 30
        def bounded(args):
            remaining = deadline - time.monotonic()
            control_require(remaining > 0, "preflight_timeout")
            result = diskutil(args, min(10, remaining)) if self.run is diskutil else self.run(args)
            control_require(time.monotonic() < deadline, "preflight_timeout")
            return result
        return eject_plan(bounded, raid_uuid)

    def eject(self, raid_uuid, force, request_id):
        if not self.lock.acquire(blocking=False):
            return 409, {"outcome": "refused", "reason": "operation_busy"}
        record, data, stage, ejected = None, None, "preflight", 0
        deadline = time.monotonic() + 150
        def run(args):
            remaining = deadline - time.monotonic()
            control_require(remaining > 0, "operation_timeout")
            result = diskutil(args, min(10, remaining)) if self.run is diskutil else self.run(args)
            control_require(time.monotonic() < deadline, "operation_timeout")
            return result
        def mutate(args):
            remaining = deadline - time.monotonic()
            control_require(remaining > 0, "operation_timeout")
            return action_command(args, min(65, remaining)) if self.mutate is action_command else self.mutate(args)
        try:
            self._allowed(raid_uuid)
            data = self._load()
            control_require(not any(r["id"] == request_id for r in data["requests"]), "duplicate_request")
            control_require(not any(r["raid"] == raid_uuid and r["state"] != "finished" for r in data["requests"]),
                            "inspection_required")
            # Completed request keys expire after 24h; interrupted/partial actions never expire.
            data["requests"] = [r for r in data["requests"] if r["state"] != "finished" or time.time()-r["time"] < 86400]
            control_require(len(data["requests"]) < 128, "operation_history_full")
            plan = self._plan(raid_uuid)
            record = {"id": request_id, "raid": raid_uuid, "time": time.time(), "state": "started", "outcome": None}
            data["requests"].append(record)
            self._save(data)  # Durable intent precedes the first native mutation.
            stage = "unmount"
            code = mutate(["unmountDisk", *(["force"] if force else []), plan["device"]])
            if code != 0:
                # Partial volume unmount is possible. Only an unchanged complete
                # fresh topology permits a new explicit attempt (including force).
                control_require(self._plan(raid_uuid) == plan, "identity_changed")
                record.update(state="finished", outcome="refused"); self._save(data)
                LOG.info("eject operation refused at unmount (%s)", request_id)
                return 409, {"outcome": "refused", "reason": "unmount_refused", "operation_id": request_id,
                             "safe_to_disconnect": False, "members_ejected": 0}
            verify_unmounted(run, plan)
            for member in plan["members"]:
                stage = "member_eject"
                current = member_plan(run, {"BSD Name": member["member_device"],
                                                "AppleRAIDMemberUUID": member["member_uuid"]}, raid_uuid)
                control_require(current == member, "identity_changed")
                verify_unmounted(run, plan, allow_missing=ejected > 0)
                # Check storage state last: unmount and topology reads can take
                # time, and a mirror can begin rebuilding without mounted files.
                verify_eject_state(run, plan, ejected)
                # Never re-resolve a degraded/reconstructed RAID after first eject:
                # this is the preserved exact physical membership plan.
                code = mutate(["eject", *(["force"] if force else []), member["device"]])
                disks = run(["list", "-plist"]).get("AllDisks")
                control_require(isinstance(disks, list), "verification_unavailable")
                control_require(code == 0 and member["device"] not in disks, "member_eject_incomplete")
                ejected += 1
            stage = "verify"
            disks = run(["list", "-plist"]).get("AllDisks")
            control_require(isinstance(disks, list) and all(m["device"] not in disks for m in plan["members"]),
                            "verification_unavailable")
            sets = run(["appleRAID", "list", "-plist"]).get("AppleRAIDSets")
            control_require(isinstance(sets, list) and not any(identifier(s.get("AppleRAIDSetUUID")) == raid_uuid for s in sets),
                            "verification_unavailable")
            record.update(state="finished", outcome="ejected"); self._save(data)
            LOG.info("eject operation completed (%s)", request_id)
            return 200, {"outcome": "ejected", "reason": None, "operation_id": request_id,
                         "members_ejected": ejected, "safe_to_disconnect": True}
        except Exception as error:
            reason = error.reason if isinstance(error, ControlRefusal) else "native_outcome_unknown" if record else "preflight_unavailable"
            outcome = "refused" if record is None else "partial" if ejected else "unknown"
            if record:
                record.update(state="blocked", outcome=outcome)
                try:
                    self._save(data)
                except Exception:
                    pass  # Durable started record already blocks future operations.
            LOG.warning("eject operation %s at %s (%s)", outcome, stage, reason)
            return (409 if record is None else 503), {"outcome": outcome, "reason": reason,
                    "operation_id": request_id, "members_ejected": ejected, "safe_to_disconnect": False,
                    "inspection_required": record is not None}
        finally:
            self.lock.release()


class Cache:
    def __init__(self, config, probe=None, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.probe = probe or (lambda: collect(config["expected_raids"], config["timeout_seconds"]))
        self.lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.raids, self.collected_at, self.attempted_at = [], None, None
        self.monotonic_at, self.error = None, "collection_not_started"

    def refresh(self):
        attempted = timestamp()
        try:
            with self.operation_lock:
                raids = self.probe()
        except Exception as exc:
            # Never publish subprocess stderr, paths, or raw host diagnostics.
            code = "command_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "collection_failed"
            with self.lock:
                changed = self.error != code
                self.attempted_at, self.error = attempted, code
            if changed:
                LOG.warning("%s (%s)", code, type(exc).__name__)
            return
        with self.lock:
            self.raids, self.collected_at = raids, timestamp()
            self.monotonic_at, self.attempted_at, self.error = self.clock(), attempted, None

    def snapshot(self, raid_uuid=None):
        with self.lock:
            age = max(0.0, self.clock() - self.monotonic_at) if self.monotonic_at is not None else None
            stale = age is None or age > self.config["stale_after_seconds"]
            raids = copy.deepcopy(self.raids)
            error, collected_at, attempted_at = self.error, self.collected_at, self.attempted_at
        found = raid_uuid is None or any(r["uuid"] == raid_uuid and r["status"] != "Missing" for r in raids)
        if raid_uuid:
            raids = [r for r in raids if r["uuid"] == raid_uuid] or [missing_raid(raid_uuid)]
        for raid in raids:
            raid["observed_health"] = raid["health"]
            if error or stale:
                raid["health"], raid["healthy"] = "error" if error else "stale", False
                raid["rebuild_summary"] = "Observation unavailable" if error else "Observation stale"
        states = {r["health"] for r in raids}
        health = ("error" if error else "stale" if stale else
                  next((s for s in ("missing", "offline", "degraded", "rebuilding", "unknown") if s in states),
                       "healthy" if raids else "missing"))
        data = {"schema_version": 1, "version": VERSION, "collected_at": collected_at,
                "attempted_at": attempted_at, "collection_ok": error is None, "error": error,
                "age_seconds": round(age, 3) if age is not None else None, "stale": stale,
                "health": health, "healthy": health == "healthy"}
        data.update({"raid": raids[0]} if raid_uuid else {"raids": raids})
        return data, found


def private_file(path):
    path = Path(path)
    # Reject symlinked parents as well as the final file.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("private file path contains a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private file must be user-owned mode 0600")
    if info.st_size > 65536:
        raise ValueError("private file too large")
    return path.read_text()


def load_config(path, require_control=True):
    config = strict_json(private_file(path))
    keys = {"bind", "port", "interval_seconds", "timeout_seconds", "stale_after_seconds", "expected_raids"}
    if not isinstance(config, dict) or set(config) not in (keys, keys | {"controls"}):
        raise ValueError("invalid configuration keys")
    address = ipaddress.ip_address(config["bind"])
    if address.version != 4:
        raise ValueError("bind must be a literal IPv4 address")
    for key, low, high in (("port", 1024, 65535), ("interval_seconds", 10, 3600),
                           ("timeout_seconds", 1, 30), ("stale_after_seconds", 20, 86400)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("invalid " + key)
    if config["stale_after_seconds"] < config["interval_seconds"] * 2:
        raise ValueError("stale threshold must cover at least two polling intervals")
    if not isinstance(config["expected_raids"], list) or len(config["expected_raids"]) > 64:
        raise ValueError("invalid expected RAID list")
    config["expected_raids"] = [identifier(v) for v in config["expected_raids"]]
    control_policy(config)
    token = private_file(Path(path).parent / "token").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("token must contain 32–256 URL-safe characters")
    if require_control:
        load_control_token(path, config, token)
    return config, token


PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>AppleRAID Status</title><style>body{font:16px system-ui;max-width:70rem;margin:3rem auto;padding:0 1rem;background:#10151b;color:#e4ecf4}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#7fcfff}</style>
<h1>AppleRAID Status</h1><p>Read-only macOS RAID observations. This does not verify SMART health, file integrity, or backups.</p>
<p><a href="/api/v1/status">JSON API</a></p><pre id="status">Loading...</pre>
<script src="/status.js"></script></html>'''
PAGE_JS = b'''async function refresh(){const el=document.getElementById('status');try{const r=await fetch('/api/v1/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);el.textContent=JSON.stringify(await r.json(),null,2)}catch(e){el.textContent='Observation unavailable: '+e.message}}refresh();setInterval(refresh,30000);'''


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, cache, token, controls=None):
        self.cache, self.token, self.controls = cache, token, controls
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # A peer's malformed/aborted connection must not create traceback logs.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "AppleRAIDStatus/" + VERSION
    sys_version = ""

    def log_message(self, *args):
        pass  # No per-request logs, credentials, or unbounded access log.

    def authorized(self):
        auth = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        if hmac.compare_digest(auth.encode(), expected.encode()):
            return True
        if auth.startswith("Basic "):
            try:
                value = base64.b64decode(auth[6:], validate=True)
                return hmac.compare_digest(value, ("status:" + self.server.token).encode())
            except ValueError:
                pass
        return False

    def respond(self, status, body, content_type="application/json; charset=utf-8", challenge=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        if challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="AppleRAID Status"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def do_GET(self):
        try:
            route = urlsplit(self.path)
        except ValueError:
            return self.respond(400, {"error": "invalid_path"})
        if route.query or route.fragment or route.scheme or route.netloc:
            return self.respond(400, {"error": "invalid_path"})
        if route.path == "/healthz":
            return self.respond(200, {"status": "ok"})
        if not self.authorized():
            return self.respond(401, {"error": "unauthorized"}, challenge=True)
        if route.path == "/":
            return self.respond(200, PAGE, "text/html; charset=utf-8")
        if route.path == "/status.js":
            return self.respond(200, PAGE_JS, "text/javascript; charset=utf-8")
        if route.path in ("/api/v1/status", "/readyz"):
            data, _ = self.server.cache.snapshot()
            ready = data["collection_ok"] and not data["stale"]
            return self.respond(200 if route.path != "/readyz" or ready else 503, data)
        action = re.fullmatch(r"/api/v1/raids/([A-Fa-f0-9-]{36})/eject", route.path)
        if action and self.server.controls:
            try:
                return self.respond(200, self.server.controls.eligibility(identifier(action[1])))
            except ValueError:
                return self.respond(400, {"error": "invalid_uuid"})
        match = re.fullmatch(r"/api/v1/raids/([A-Fa-f0-9-]{36})", route.path)
        if match:
            try:
                data, found = self.server.cache.snapshot(identifier(match[1]))
            except (ValueError, AttributeError):
                return self.respond(400, {"error": "invalid_uuid"})
            return self.respond(200 if found else 404, data)
        return self.respond(404, {"error": "not_found"})

    do_HEAD = do_GET

    def read_only(self):
        self.respond(405, {"error": "read_only"})

    def do_POST(self):
        controls = self.server.controls
        if controls is None:
            return self.read_only()
        auth = self.headers.get_all("Authorization", [])
        if (len(auth) != 1 or not controls.token or not hmac.compare_digest(
                auth[0].encode(), ("Bearer " + controls.token).encode())):
            return self.respond(401, {"error": "control_unauthorized"})
        # Browser Basic, cross-origin requests and forms never authorize eject.
        if (self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") is not None
                or self.headers.get("Transfer-Encoding") is not None):
            return self.respond(400, {"error": "invalid_control_request"})
        match = re.fullmatch(r"/api/v1/raids/([A-Fa-f0-9-]{36})/eject", self.path)
        if not match:
            return self.respond(404, {"error": "not_found"})
        try:
            raid_uuid = control_uuid(match[1])
            key = self.headers.get_all("Idempotency-Key", [])
            if len(key) != 1:
                raise ValueError("invalid request ID")
            request_id = control_uuid(key[0])
            length = self.headers.get_all("Content-Length", [])
            if (len(length) != 1 or not re.fullmatch(r"[0-9]{1,4}", length[0])
                    or not 1 <= int(length[0]) <= 1024
                    or self.headers.get_all("Content-Type", []) != ["application/json"]):
                raise ValueError("invalid body framing")
            raw = self.rfile.read(int(length[0]))
            if len(raw) != int(length[0]):
                raise ValueError("incomplete body")
            body = strict_json(raw)
            if (not isinstance(body, dict) or set(body) != {"force", "confirm"}
                    or type(body["force"]) is not bool or control_uuid(body["confirm"]) != raid_uuid):
                raise ValueError("invalid confirmation")
        except (ValueError, TypeError, AttributeError, socket.timeout):
            return self.respond(400, {"error": "invalid_control_request"})
        code, result = controls.eject(raid_uuid, body["force"], request_id)
        self.respond(code, result)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = read_only


def serve(config, token, control_token=None, state_dir=None):
    cache, stopped = Cache(config), threading.Event()
    controls = EjectControls(config, control_token, state_dir, cache.operation_lock) if control_token else None
    server = Server((config["bind"], config["port"]), cache, token, controls)

    def poll():
        while not stopped.is_set():
            cache.refresh()
            stopped.wait(config["interval_seconds"])

    def stop(*_):
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=poll, daemon=True, name="collector").start()
    LOG.info("started version %s", VERSION)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stopped.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", type=Path, help="private config.json beside token")
    parser.add_argument("--once", action="store_true", help="print one read-only local observation; no server")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        config = {"expected_raids": [], "timeout_seconds": 10, "stale_after_seconds": 90}
        if args.config:
            config, _ = load_config(args.config)
        cache = Cache(config)
        cache.refresh()
        data, _ = cache.snapshot()
        print(json.dumps(data, indent=2, allow_nan=False))
        return 0 if data["collection_ok"] else 1
    if not args.config:
        parser.error("--config is required unless --once is used")
    config, token = load_config(args.config)
    log_path = args.config.parent / "collector.log"
    if any(p.is_symlink() for p in (log_path, log_path.with_name("collector.log.1"))):
        raise ValueError("log path must not be a symlink")
    os.umask(0o077)
    handler = RotatingFileHandler(log_path, maxBytes=262144, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().handlers = [handler]
    serve(config, token, load_control_token(args.config, config, token), args.config.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
