#!/usr/bin/env python3
"""Read-only AppleRAID observations over HTTP. Python standard library only."""

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

VERSION = "0.2.1"
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


class Cache:
    def __init__(self, config, probe=None, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.probe = probe or (lambda: collect(config["expected_raids"], config["timeout_seconds"]))
        self.lock = threading.Lock()
        self.raids, self.collected_at, self.attempted_at = [], None, None
        self.monotonic_at, self.error = None, "collection_not_started"

    def refresh(self):
        attempted = timestamp()
        try:
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


def load_config(path):
    config = json.loads(private_file(path))
    keys = {"bind", "port", "interval_seconds", "timeout_seconds", "stale_after_seconds", "expected_raids"}
    if not isinstance(config, dict) or set(config) != keys:
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
    token = private_file(Path(path).parent / "token").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("token must contain 32–256 URL-safe characters")
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

    def __init__(self, address, cache, token):
        self.cache, self.token = cache, token
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
        match = re.fullmatch(r"/api/v1/raids/([A-Fa-f0-9-]{36})", route.path)
        if match:
            try:
                data, found = self.server.cache.snapshot(identifier(match[1]))
            except (ValueError, AttributeError):
                return self.respond(400, {"error": "invalid_uuid"})
            return self.respond(200 if found else 404, data)
        return self.respond(404, {"error": "not_found"})

    do_HEAD = do_GET

    def do_POST(self):
        self.respond(405, {"error": "read_only"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST


def serve(config, token):
    cache, stopped = Cache(config), threading.Event()
    server = Server((config["bind"], config["port"]), cache, token)

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
    serve(config, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
