#!/usr/bin/env python3
"""User-scoped launchd lifecycle for AppleRAID Status. Never changes RAID state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from apple_raid_status import VERSION, identifier, load_config, private_file

LABEL = "io.github.mglaeser.apple-raid-status"
DEFAULT_STATE = Path.home() / "Library/Application Support/apple-raid-status"
JOB = f"gui/{os.getuid()}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
ASSETS = ("apple_raid_status.py", "install.py")


def checked_directory(path, private=False, create=True):
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError("directory path contains a symlink")
        if parent.exists():
            if not parent.is_dir():
                raise ValueError("not a directory: " + str(parent))
        else:
            if not create:
                raise ValueError("installation directory does not exist")
            parent.mkdir(mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("directory must be owned by current user and not writable by others")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("state directory must have mode 0700")
    return path


def atomic_private(path, data):
    path = Path(path)
    if path.exists() or path.is_symlink():
        private_file(path)  # Refuse replaced files and unexpected ownership/modes.
    fd, temp = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def launchctl(*args, check=True):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True,
                          timeout=15, check=check)


def loaded():
    result = launchctl("print", JOB, check=False)
    if result.returncode == 0:
        return result.stdout
    # A missing job is expected; never interpret every launchctl error as absent.
    if "Could not find service" in result.stderr:
        return None
    raise RuntimeError("cannot inspect launchd job: " + result.stderr.strip())


def expected_program(state, verify_assets=True, require_runtime=False):
    receipt = json.loads(private_file(state / "receipt.json"))
    python = receipt.get("python")
    if not isinstance(python, str) or not Path(python).is_absolute():
        raise ValueError("invalid recorded interpreter")
    if require_runtime and not os.access(python, os.X_OK):
        raise ValueError("recorded Python interpreter is unavailable; reinstall with a supported Python")
    for name in ASSETS if verify_assets else ():
        data = private_file(state / "app" / name).encode()
        if hashlib.sha256(data).hexdigest() != receipt.get("sha256", {}).get(name):
            raise ValueError("installed source does not match receipt: " + name)
    return [python, str(state / "app/apple_raid_status.py"), "--config", str(state / "config.json")]


def validate_job(state, verify_assets=True):
    expected = expected_program(state, verify_assets=verify_assets)
    definition = plistlib.loads(private_file(PLIST).encode())
    if (definition.get("Label") != LABEL or definition.get("ProgramArguments") != expected
            or definition.get("KeepAlive") is not True or definition.get("RunAtLoad") is not True
            or definition.get("WorkingDirectory") != str(state)):
        raise ValueError("LaunchAgent identity/configuration differs from this installation")
    snapshot = loaded()
    if snapshot is not None:
        arguments = re.search(r"^\targuments = \{\n(.*?)^\t\}", snapshot, re.M | re.S)
        actual = [line[2:] for line in arguments[1].splitlines() if line.startswith("\t\t")] if arguments else []
        program = re.search(r"^\tprogram = (.+)$", snapshot, re.M)
        path = re.search(r"^\tpath = (.+)$", snapshot, re.M)
        if actual != expected or not program or program[1] != expected[0] or not path or path[1] != str(PLIST):
            raise ValueError("loaded job does not match this installation")
    return snapshot


def install(state, args):
    state = checked_directory(state, private=True)
    checked_directory(state / "app", private=True)
    checked_directory(PLIST.parent)
    source = Path(__file__).resolve().parent
    assets = {name: (source / name).read_bytes() for name in ASSETS}
    current = loaded()
    if current is not None:
        validate_job(state)
        if any((state / "app" / name).read_bytes() != data for name, data in assets.items()):
            raise ValueError("stop this helper before updating its source")
    if not (state / "config.json").exists():
        import ipaddress
        if ipaddress.ip_address(args.bind or "127.0.0.1").version != 4:
            raise ValueError("bind must be a literal IPv4 address")
        if args.port is not None and not 1024 <= args.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        config = {"bind": args.bind or "127.0.0.1", "port": args.port or 8788,
                  "interval_seconds": 30, "timeout_seconds": 10, "stale_after_seconds": 90,
                  "expected_raids": [identifier(v) for v in (args.expect or [])]}
        # The token is generated only for a new installation and is never printed.
        if not (state / "token").exists():
            atomic_private(state / "token", (secrets.token_urlsafe(48) + "\n").encode())
        atomic_private(state / "config.json", (json.dumps(config, indent=2) + "\n").encode())
    config, _ = load_config(state / "config.json")
    if ((args.bind is not None and args.bind != config["bind"])
            or (args.port is not None and args.port != config["port"])
            or (args.expect is not None and sorted(map(identifier, args.expect)) != sorted(config["expected_raids"]))):
        raise ValueError("existing configuration differs; edit it explicitly while stopped, then reinstall")
    python = str(Path(sys.executable).resolve())
    receipt = {"version": VERSION, "python": python,
               "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in assets.items()}}
    git = subprocess.run(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    receipt["source_commit"] = git.stdout.strip() if git.returncode == 0 else None
    log = state / "service.log"
    definition = {"Label": LABEL, "ProgramArguments": [python, str(state / "app/apple_raid_status.py"), "--config", str(state / "config.json")],
                  "WorkingDirectory": str(state), "RunAtLoad": True, "KeepAlive": True,
                  "ThrottleInterval": 30, "ProcessType": "Background", "LowPriorityIO": True,
                  "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                  "StandardOutPath": str(log), "StandardErrorPath": str(log)}
    if current is not None and plistlib.loads(private_file(PLIST).encode()) != definition:
        raise ValueError("stop helper before updating its LaunchAgent")
    # Complete loaded-job preflight before mutating the receipt or source.
    for name, data in assets.items():
        atomic_private(state / "app" / name, data)
    atomic_private(state / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())
    if not log.exists():
        atomic_private(log, b"")
    elif log.is_symlink() or log.stat().st_uid != os.getuid() or stat.S_IMODE(log.stat().st_mode) != 0o600:
        raise ValueError("unsafe service log")
    atomic_private(PLIST, plistlib.dumps(definition))
    print("Installed AppleRAID Status " + VERSION + "; configuration and token are private and preserved.")
    print("State: " + str(state))
    print("Start explicitly with: python3 install.py start --state-dir '" + str(state) + "'")


def status(state, quiet=False):
    snapshot = validate_job(state)
    if snapshot is None or "state = running" not in snapshot:
        raise RuntimeError("helper is not running")
    config, token = load_config(state / "config.json")
    address = "127.0.0.1" if config["bind"] == "0.0.0.0" else config["bind"]
    request = urllib.request.Request(f"http://{address}:{config['port']}/readyz",
                                     headers={"Authorization": "Bearer " + token})
    # Never send this local credential through an ambient HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = json.loads(response.read(1024 * 1024))
    if data.get("schema_version") != 1 or not data.get("collection_ok") or data.get("stale"):
        raise RuntimeError("collector is not ready")
    if not quiet:
        print(json.dumps({"service": "running", "version": data["version"], "collection": "ready",
                          "raid_health": data["health"], "age_seconds": data["age_seconds"],
                          "launch_agent": LABEL, "state_dir": str(state)}))


def start(state):
    expected_program(state, require_runtime=True)
    if validate_job(state) is None:
        launchctl("enable", JOB)
        launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
    deadline = time.monotonic() + 45
    last = None
    while time.monotonic() < deadline:
        try:
            status(state)
            return
        except Exception as exc:
            last = type(exc).__name__
            time.sleep(0.5)
    raise RuntimeError("helper did not become ready; inspect private service.log (" + str(last) + ")")


def stop(state):
    if validate_job(state, verify_assets=False) is not None:
        launchctl("bootout", JOB)
        deadline = time.monotonic() + 15
        while loaded() is not None:
            if time.monotonic() > deadline:
                raise RuntimeError("launchd job did not stop")
            time.sleep(0.2)
    print("Stopped AppleRAID Status for this login session.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "restart", "status", "uninstall"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--bind", help="IPv4 address; new installation default 127.0.0.1")
    parser.add_argument("--port", type=int, help="new installation default 8788")
    parser.add_argument("--expect", action="append", help="expected RAID UUID; repeat for multiple sets")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.getuid() == 0:
        parser.error("run as your ordinary macOS user, without sudo")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer required")
    if args.action != "install" and any(v is not None for v in (args.bind, args.port, args.expect)):
        parser.error("--bind, --port, and --expect apply only to install")
    state = args.state_dir.expanduser().absolute()
    if args.action == "install":
        install(state, args)
    else:
        checked_directory(state, private=True, create=False)
        if args.action == "start":
            start(state)
        elif args.action == "status":
            status(state)
        elif args.action == "restart":
            stop(state)
            start(state)
        elif args.action == "stop":
            stop(state)
        elif args.action == "uninstall":
            stop(state)
            PLIST.unlink()
            print("Removed the LaunchAgent. Private configuration, token, source and logs retained at " + str(state))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
