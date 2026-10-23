#!/usr/bin/env python3
"""User-scoped launchd lifecycle for AppleRAID Status. Never changes RAID state."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

VERSION = "0.3.0"
LABEL = "io.github.mglaeser.apple-raid-status"
DEFAULT_STATE = Path.home() / "Library/Application Support/apple-raid-status"
LOCK_DIR = Path.home() / "Library/Application Support/apple-raid-status-lifecycle"
JOB = f"gui/{os.getuid()}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
ASSETS = ("apple_raid_status.py", "install.py")
HEX = re.compile(r"[0-9a-f]{64}\Z")


def checked_directory(path, private=False, create=True):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("directory path must not contain '..'")
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


def file_info(path, mode=0o600):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("managed file path contains a symlink")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode):
        raise ValueError("managed file has unexpected ownership, type or permissions")
    return info


def private_file(path):
    if file_info(path).st_size > 65536:
        raise ValueError("private file too large")
    return Path(path).read_text()


def atomic_private(path, data, mode=0o600):
    path = Path(path)
    if path.exists() or path.is_symlink():
        file_info(path, mode)
    fd, temp = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def transaction_plan(state):
    journal = state / ".install-transaction"
    checked_directory(journal, private=True, create=False)
    plan = json.loads(private_file(journal / "plan.json"))
    if (not isinstance(plan, dict) or set(plan) != {"schema", "state_dir", "launchers", "entries", "phase"}
            or plan["schema"] != 1 or plan["state_dir"] != str(state)
            or plan["phase"] not in ("prepared", "committed", "rolled_back")
            or not isinstance(plan["launchers"], list) or len(plan["launchers"]) > 2
            or not isinstance(plan["entries"], list)):
        raise ValueError("invalid interrupted-installation journal")
    launchers = set()
    for value in plan["launchers"]:
        path = Path(value)
        if (not path.is_absolute() or str(path) != value or ".." in path.parts or path == PLIST or path == state
                or (path.is_relative_to(state) and (len(path.relative_to(state).parts) < 2 or path.relative_to(state).parts[0] != "bin"))):
            raise ValueError("invalid launcher in installation journal")
        checked_directory(path.parent, create=False)
        launchers.add(path)
    allowed = {PLIST, state / "receipt.json", *(state / "app" / name for name in ASSETS), *launchers}
    paths = []
    for entry in plan["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode", "before", "after"}:
            raise ValueError("invalid installation journal entry")
        path = Path(entry["path"])
        if path not in allowed or entry["mode"] != (0o700 if path in launchers else 0o600):
            raise ValueError("unexpected installation journal destination")
        if any(value is not None and (not isinstance(value, str) or not HEX.fullmatch(value)) for value in (entry["before"], entry["after"])):
            raise ValueError("invalid installation journal digest")
        paths.append(path)
    if len(paths) != len(set(paths)) or set(paths) != allowed:
        raise ValueError("installation journal destinations differ")
    return journal, plan


def cleanup_transaction(journal):
    # No recursive deletion: only the exact private files produced by staging.
    for path in journal.iterdir():
        if path.name != "plan.json" and not re.fullmatch(r"[0-9]+\.(before|after)", path.name):
            raise ValueError("unexpected file in installation journal")
        file_info(path)
    for path in journal.iterdir():
        if path.name != "plan.json":
            path.unlink()
    (journal / "plan.json").unlink(missing_ok=True)
    journal.rmdir()


def recover_install(state):
    journal = state / ".install-transaction"
    if not journal.exists() and not journal.is_symlink():
        return
    if loaded() is not None:
        raise ValueError("interrupted installation has a loaded job; inspect retained state before recovery")
    checked_directory(journal, private=True, create=False)
    if not (journal / "plan.json").exists():
        # The manifest is published only after every backup is staged. No
        # installed file can have changed if staging never reached that point.
        cleanup_transaction(journal)
        return
    journal, plan = transaction_plan(state)
    target = "after" if plan["phase"] == "committed" else "before"
    restores = []
    for index, entry in enumerate(plan["entries"]):
        path = Path(entry["path"])
        present = path.exists() or path.is_symlink()
        current = None
        if present:
            file_info(path, entry["mode"])
            current = digest(path)
        if current not in (entry["before"], entry["after"]):
            raise ValueError("installation destination changed outside the transaction; recovery refused")
        wanted = entry[target]
        if plan["phase"] != "prepared":
            if current != wanted:
                raise ValueError("completed installation journal differs from installed files")
            continue
        data = None
        if wanted is not None:
            backup = journal / f"{index}.{target}"
            if file_info(backup).st_size > 1048576 or digest(backup) != wanted:
                raise ValueError("installation rollback backup changed")
            data = backup.read_bytes()
        if current != wanted:
            restores.append((path, data, entry["mode"]))
    # Validate all ownership and backups before restoring even one destination.
    for path, data, mode in restores:
        if data is None:
            path.unlink()
        else:
            atomic_private(path, data, mode)
    if plan["phase"] == "prepared":
        plan["phase"] = "rolled_back"
        atomic_private(journal / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
    cleanup_transaction(journal)


def write_installation(state, updates, launchers):
    """Bounded journal for this installation's exact files, never RAID data."""
    journal = checked_directory(state / ".install-transaction", private=True)
    plan = {"schema": 1, "state_dir": str(state), "launchers": sorted(map(str, launchers)),
            "entries": [], "phase": "prepared"}
    try:
        for index, (path, data, mode) in enumerate(updates):
            before = path.read_bytes() if path.exists() else None
            entry = {"path": str(path), "mode": mode, "before": None, "after": None}
            for key, content in (("before", before), ("after", data)):
                if content is not None:
                    entry[key] = hashlib.sha256(content).hexdigest()
                    atomic_private(journal / f"{index}.{key}", content)
            plan["entries"].append(entry)
        atomic_private(journal / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
        for path, data, mode in updates:
            if data is None:
                path.unlink()
            else:
                atomic_private(path, data, mode)
        plan["phase"] = "committed"
        atomic_private(journal / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
    except BaseException:
        try:
            recover_install(state)
        except Exception as recovery_error:
            raise RuntimeError("installation interrupted; private recovery journal retained; rerun reviewed install after resolving: " + str(recovery_error)) from recovery_error
        raise
    cleanup_transaction(journal)


@contextlib.contextmanager
def lifecycle_lock():
    # The label/plist are shared by every --state-dir, so a per-state lock is
    # insufficient. flock is released by the kernel after an interrupted CLI.
    checked_directory(LOCK_DIR, private=True)
    path = LOCK_DIR / (LABEL + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = file_info(path)
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("lifecycle lock was replaced")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another lifecycle operation is active; do not delete its lock") from exc
        yield
    finally:
        os.close(fd)


def command(arguments, timeout=15, check=False):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=check,
                            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()), "LC_ALL": "C"})
    if len(result.stdout) + len(result.stderr) > 262144:
        raise RuntimeError("native diagnostic output exceeded the inspection bound")
    return result


def launchctl(*args, check=True):
    return command(["/bin/launchctl", *args], check=check)


def loaded():
    result = launchctl("print", JOB, check=False)
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 113 and "Could not find service" in result.stderr + result.stdout:
        return None
    raise RuntimeError("cannot inspect launchd job")


def disabled_override():
    result = launchctl("print-disabled", f"gui/{os.getuid()}", check=False)
    if result.returncode:
        raise RuntimeError("cannot inspect launchd disabled intent")
    label = '"' + re.escape(LABEL) + '"'
    occurrences = re.findall(label, result.stdout)
    values = re.findall(r"^[ \t]*" + label + r"[ \t]*=>[ \t]*(true|false|enabled|disabled)[ \t]*$", result.stdout, re.M)
    if len(occurrences) > 1 or (occurrences and len(values) != 1):
        raise ValueError("ambiguous or malformed launchd disabled intent")
    return values[0] in ("true", "disabled") if values else None


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_receipt(state):
    receipt = json.loads(private_file(state / "receipt.json"))
    if not isinstance(receipt, dict):
        raise ValueError("invalid installation receipt")
    legacy = receipt.get("version") == "0.1.0" and "lifecycle_schema" not in receipt
    fields = {"version", "python", "sha256", "source_commit"}
    if not legacy:
        fields |= {"lifecycle_schema", "python_sha256", "launcher", "launcher_sha256"}
    if set(receipt) != fields or (not legacy and receipt.get("lifecycle_schema") != 2):
        raise ValueError("unsupported installation receipt")
    python = receipt.get("python")
    if not isinstance(python, str) or not Path(python).is_absolute() or ".." in Path(python).parts:
        raise ValueError("invalid recorded interpreter")
    hashes = receipt.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(ASSETS) or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in hashes.values()):
        raise ValueError("invalid recorded source hashes")
    if not legacy:
        launcher = receipt.get("launcher")
        if (not isinstance(launcher, str) or not Path(launcher).is_absolute() or ".." in Path(launcher).parts
                or not all(isinstance(receipt.get(k), str) and HEX.fullmatch(receipt[k]) for k in ("python_sha256", "launcher_sha256"))):
            raise ValueError("invalid recorded launcher or interpreter digest")
    return receipt


def launcher_bytes(state, python):
    # -I excludes ambient Python paths and user site packages. The manager loads
    # its one verified sibling by absolute path; no source checkout is needed.
    return ("#!/bin/sh\nexec " + shlex.quote(python) + " -I -B -u "
            + shlex.quote(str(state / "app/install.py")) + ' "$@" --state-dir '
            + shlex.quote(str(state)) + "\n").encode()


def expected_program(state, verify_assets=True, require_runtime=False):
    receipt = read_receipt(state)
    legacy = receipt.get("lifecycle_schema") != 2
    if verify_assets:
        for name in ASSETS:
            if hashlib.sha256(private_file(state / "app" / name).encode()).hexdigest() != receipt["sha256"][name]:
                raise ValueError("installed source does not match receipt: " + name)
        if not legacy:
            launcher = Path(receipt["launcher"])
            file_info(launcher, 0o700)
            data = launcher.read_bytes()
            if data != launcher_bytes(state, receipt["python"]) or hashlib.sha256(data).hexdigest() != receipt["launcher_sha256"]:
                raise ValueError("installed launcher does not match receipt")
    if require_runtime:
        python = receipt["python"]
        if not os.access(python, os.X_OK) or not Path(python).is_file():
            raise ValueError("recorded Python interpreter is unavailable; reinstall with a supported Python")
        if not legacy and digest(python) != receipt["python_sha256"]:
            raise ValueError("recorded Python interpreter changed; stop and reinstall the reviewed runtime")
    if legacy:
        return [receipt["python"], str(state / "app/apple_raid_status.py"), "--config", str(state / "config.json")]
    return [receipt["launcher"], "serve"]


def expected_plist(state, receipt, disabled=True):
    log = str(state / "service.log")
    legacy = receipt.get("lifecycle_schema") != 2
    arguments = ([receipt["python"], str(state / "app/apple_raid_status.py"), "--config", str(state / "config.json")]
                 if legacy else [receipt["launcher"], "serve"])
    definition = {"Label": LABEL, "ProgramArguments": arguments, "WorkingDirectory": str(state),
                  "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                  "ProcessType": "Standard", "LowPriorityIO": True,
                  "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                  "StandardOutPath": log, "StandardErrorPath": log}
    if not legacy:
        if type(disabled) is not bool:
            raise ValueError("configured Disabled must be a boolean")
        definition.update(Disabled=disabled, Umask=63)
    return definition


def read_definition(state):
    receipt = read_receipt(state)
    definition = plistlib.loads(private_file(PLIST).encode())
    disabled = definition.get("Disabled") if isinstance(definition, dict) else None
    if definition != expected_plist(state, receipt, disabled):
        raise ValueError("LaunchAgent identity/configuration differs from this installation")
    return definition


def job_fields(snapshot):
    fields = {}
    for key in ("path", "program", "state", "pid"):
        values = re.findall(r"^\t" + key + r" = (.+)$", snapshot, re.M)
        if len(values) > 1:
            raise ValueError("ambiguous loaded job identity")
        fields[key] = values[0] if values else None
    arguments = re.findall(r"^\targuments = \{\n(.*?)^\t\}", snapshot, re.M | re.S)
    if len(arguments) != 1:
        raise ValueError("ambiguous loaded job arguments")
    fields["arguments"] = [line[2:] for line in arguments[0].splitlines() if line.startswith("\t\t")]
    if not fields["state"] or (fields["pid"] is not None and not fields["pid"].isdigit()):
        raise ValueError("incomplete loaded job state")
    return fields


def validate_job(state, verify_assets=True):
    if (state / ".install-transaction").exists() or (state / ".install-transaction").is_symlink():
        raise ValueError("interrupted installation; rerun reviewed install to recover before lifecycle actions")
    expected = expected_program(state, verify_assets=verify_assets)
    read_definition(state)  # Exact dictionary rejects Program overrides and extra triggers.
    snapshot = loaded()
    if snapshot is not None:
        fields = job_fields(snapshot)
        if fields["arguments"] != expected or fields["program"] != expected[0] or fields["path"] != str(PLIST):
            raise ValueError("loaded job does not match this installation")
    return snapshot


def collector_module(path):
    spec = importlib.util.spec_from_file_location("apple_raid_status", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installed_config(state):
    expected_program(state)
    return collector_module(state / "app/apple_raid_status.py").load_config(state / "config.json")


def install(state, args):
    state = checked_directory(state, private=True)
    recover_install(state)
    checked_directory(state / "app", private=True)
    checked_directory(PLIST.parent)
    source = Path(__file__).resolve().parent
    assets = {name: (source / name).read_bytes() for name in ASSETS}
    current, override = loaded(), disabled_override()
    existing = (state / "receipt.json").exists()
    previous, disabled, retained = None, True, False
    if existing:
        previous = read_receipt(state)
        # A retained receipt after uninstall may be reused, but only when both
        # global plist and loaded job are absent. Reinstallation stays stopped.
        if PLIST.exists() or PLIST.is_symlink():
            validate_job(state)
            disabled = read_definition(state).get("Disabled", True)
        else:
            retained = True
            if current is not None:
                raise ValueError("loaded job has no owned LaunchAgent; nothing was changed")
            for name in ASSETS:
                if hashlib.sha256(private_file(state / "app" / name).encode()).hexdigest() != previous["sha256"][name]:
                    raise ValueError("retained installation source differs from its receipt")
        if override is True:
            disabled = True
    elif current is not None or PLIST.exists() or PLIST.is_symlink():
        raise ValueError("this label already belongs to another installation; nothing was changed")
    elif override is False:
        raise ValueError("fresh installation found an enabled system override; inspect the existing label")
    launcher = Path(getattr(args, "launcher", None) or (previous or {}).get("launcher") or state / "bin/AppleRAID Status").expanduser()
    if not launcher.is_absolute() or ".." in launcher.parts:
        raise ValueError("--launcher must be an absolute path without '..'")
    if (launcher == PLIST or launcher == state
            or (launcher.is_relative_to(state) and (len(launcher.relative_to(state).parts) < 2 or launcher.relative_to(state).parts[0] != "bin"))):
        raise ValueError("launcher path conflicts with managed installation files")
    checked_directory(launcher.parent)
    if launcher.exists() or launcher.is_symlink():
        file_info(launcher, 0o700)
        if not previous or previous.get("launcher") != str(launcher) or launcher.read_bytes() != launcher_bytes(state, previous["python"]):
            raise ValueError("launcher path is not owned by this installation")
    old_launcher = None
    if previous and previous.get("launcher") and previous["launcher"] != str(launcher):
        old = Path(previous["launcher"])
        if old.exists() or old.is_symlink():
            checked_directory(old.parent, create=False)
            file_info(old, 0o700)
            if old.read_bytes() != launcher_bytes(state, previous["python"]):
                raise ValueError("previous launcher changed; nothing was installed")
            old_launcher = old
    python = str(Path(sys.executable).resolve())
    launcher_data = launcher_bytes(state, python)
    receipt = {"lifecycle_schema": 2, "version": VERSION, "python": python, "python_sha256": digest(python),
               "launcher": str(launcher), "launcher_sha256": hashlib.sha256(launcher_data).hexdigest(),
               "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in assets.items()}}
    git = command(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], timeout=5)
    receipt["source_commit"] = git.stdout.strip() if git.returncode == 0 else None
    definition = expected_plist(state, receipt, disabled)
    if current is not None:
        if previous != receipt or read_definition(state) != definition:
            raise ValueError("stop helper before updating its LaunchAgent or source")
    # Preflight every existing destination before writing assets or changing
    # intent. A failed update never adopts an unknown stopped job or log path.
    for path in (state / "receipt.json", PLIST, state / "service.log", *(state / "app" / name for name in ASSETS)):
        if path.exists() or path.is_symlink():
            file_info(path)
    if not (state / "config.json").exists():
        import ipaddress
        if ipaddress.ip_address(args.bind or "127.0.0.1").version != 4:
            raise ValueError("bind must be a literal IPv4 address")
        if args.port is not None and not 1024 <= args.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        config = {"bind": args.bind or "127.0.0.1", "port": args.port or 8788,
                  "interval_seconds": 30, "timeout_seconds": 10, "stale_after_seconds": 90,
                  "expected_raids": [str(uuid.UUID(v)).upper() for v in (args.expect or [])]}
        if not (state / "token").exists():
            atomic_private(state / "token", (secrets.token_urlsafe(48) + "\n").encode())
        atomic_private(state / "config.json", (json.dumps(config, indent=2) + "\n").encode())
    config, _ = collector_module(source / "apple_raid_status.py").load_config(state / "config.json")
    if ((args.bind is not None and args.bind != config["bind"])
            or (args.port is not None and args.port != config["port"])
            or (args.expect is not None and sorted(str(uuid.UUID(v)).upper() for v in args.expect) != sorted(config["expected_raids"]))):
        raise ValueError("existing configuration differs; edit it explicitly while stopped, then reinstall")
    if current is not None:
        # An exact running installation needs no rewrite or transaction. This
        # also keeps failed preparation from affecting a healthy loaded owner.
        print(json.dumps({"installed": True, "started": False, "version": VERSION, "state_dir": str(state),
                          "launcher": str(launcher), "configured_disabled": disabled, "intent": "preserved"}))
        return
    if previous and (previous.get("lifecycle_schema") != 2 or retained):
        # v0.1 stop left an enabled OS override behind, so it cannot establish
        # durable running intent. An exact unloaded migration/reinstallation is
        # prepared stopped in both stores, without bootstrapping a new job.
        launchctl("disable", JOB)
        if disabled_override() is not True:
            raise RuntimeError("migration disable was not confirmed; no installation assets were changed")
    if not (state / "service.log").exists():
        atomic_private(state / "service.log", b"")
    updates = [(state / "app" / name, data, 0o600) for name, data in assets.items()]
    updates += [(launcher, launcher_data, 0o700),
                (state / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode(), 0o600),
                (PLIST, plistlib.dumps(definition), 0o600)]
    if old_launcher:
        updates.append((old_launcher, None, 0o700))
    write_installation(state, updates, [launcher, *([old_launcher] if old_launcher else [])])
    print(json.dumps({"installed": True, "started": False, "version": VERSION, "state_dir": str(state),
                      "launcher": str(launcher), "configured_disabled": disabled, "intent": "preserved"}))


def persist_disabled(state, disabled, verify_assets=True):
    receipt = read_receipt(state)
    if receipt.get("lifecycle_schema") != 2:
        raise ValueError("stop the legacy helper and install the reviewed lifecycle update first")
    expected_program(state, verify_assets=verify_assets)
    definition = read_definition(state)
    if definition["Disabled"] != disabled:
        atomic_private(PLIST, plistlib.dumps(expected_plist(state, receipt, disabled)))
    if read_definition(state)["Disabled"] is not disabled:
        raise RuntimeError("startup intent readback failed; no native lifecycle command was requested")


def inspect_listener(state, snapshot, required=False):
    config, _ = installed_config(state)
    result = command(["/usr/sbin/lsof", "-nP", f"-iTCP:{config['port']}", "-sTCP:LISTEN", "-Fpn"])
    if result.stderr or result.returncode not in (0, 1) or (result.returncode == 1 and result.stdout):
        raise RuntimeError("listener ownership is unknown")
    if result.returncode == 1:
        if required:
            raise RuntimeError("owned listener is not ready")
        return
    pids = [line[1:] for line in result.stdout.splitlines() if line.startswith("p")]
    addresses = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
    address = "*" if config["bind"] == "0.0.0.0" else config["bind"]
    fields = job_fields(snapshot) if snapshot else {}
    if not fields.get("pid") or pids != [fields["pid"]] or addresses != [f"{address}:{config['port']}"]:
        raise RuntimeError("configured port has an unexpected listener; no credential was sent")


def readiness(state, snapshot):
    inspect_listener(state, snapshot, required=True)
    config, token = installed_config(state)
    address = "127.0.0.1" if config["bind"] == "0.0.0.0" else config["bind"]
    request = urllib.request.Request(f"http://{address}:{config['port']}/readyz", headers={"Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = json.loads(response.read(1024 * 1024))
    if data.get("schema_version") != 1 or not data.get("collection_ok") or data.get("stale"):
        raise RuntimeError("collector is not ready")
    return {"version": data["version"], "collection": "ready", "raid_health": data["health"], "age_seconds": data["age_seconds"]}


def status(state, quiet=False, strict=False):
    snapshot = validate_job(state)
    definition = read_definition(state)
    if "Disabled" not in definition:
        raise ValueError("legacy installation needs the lifecycle update")
    override = disabled_override()
    configured = definition["Disabled"]
    effective = configured if override is None else override
    fields = job_fields(snapshot) if snapshot else {}
    running = fields.get("state") == "running" and bool(fields.get("pid"))
    result = {"service": "running" if running else "stopped", "state": fields.get("state", "absent"),
              "pid": int(fields["pid"]) if fields.get("pid") else None,
              "version": read_receipt(state)["version"], "collection": "not_running",
              "raid_health": None, "age_seconds": None, "launch_agent": LABEL, "state_dir": str(state),
              "configured_disabled": configured, "disabled_override": override,
              "effective_disabled": effective, "disabled": effective}
    if strict and (configured or effective or not running):
        raise RuntimeError("helper is not enabled and running")
    if running:
        try:
            if strict:
                expected_program(state, require_runtime=True)
            result.update(readiness(state, snapshot))
        except Exception as exc:
            if strict:
                raise
            result.update(collection="unavailable", readiness_error=type(exc).__name__)
    if not quiet:
        print(json.dumps(result))
    return result


def start(state):
    expected_program(state, require_runtime=True)
    snapshot = validate_job(state)
    disabled_override()  # Refuse unknown OS intent before making changes.
    inspect_listener(state, snapshot)
    persist_disabled(state, False)
    launchctl("enable", JOB)
    if disabled_override() is True:
        raise RuntimeError("enable was not confirmed; enabled plist intent is retained")
    if snapshot is None:
        launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
    elif job_fields(snapshot)["state"] != "running":
        launchctl("kickstart", JOB)
    deadline = time.monotonic() + 45
    last = None
    while time.monotonic() < deadline:
        try:
            return status(state, strict=True)
        except Exception as exc:
            last = type(exc).__name__
            time.sleep(0.5)
    raise RuntimeError("helper did not become ready; enabled intent is retained; inspect private service.log (" + str(last) + ")")


def stop(state):
    # Stopping an exact owner must not execute damaged collector code or require
    # its old interpreter. Use a working Python/source installer for recovery.
    snapshot = validate_job(state, verify_assets=False)
    disabled_override()
    persist_disabled(state, True, verify_assets=False)
    launchctl("disable", JOB)
    if disabled_override() is not True:
        raise RuntimeError("stop intent is persisted but native disable was not confirmed; no job was booted out")
    if snapshot is not None:
        launchctl("bootout", JOB)
    deadline = time.monotonic() + 15
    while validate_job(state, verify_assets=False) is not None:
        if time.monotonic() > deadline:
            raise RuntimeError("stop intent is persisted but launchd job did not stop")
        time.sleep(0.2)
    print(json.dumps({"stopped": True, "configured_disabled": True, "effective_disabled": True}))


def uninstall(state):
    receipt = read_receipt(state)
    launcher = Path(receipt["launcher"]) if receipt.get("lifecycle_schema") == 2 else None
    if not PLIST.exists() and not PLIST.is_symlink():
        if loaded() is not None:
            raise ValueError("incomplete uninstall; inspect retained lifecycle assets")
    else:
        stop(state)
    # Exact retained launcher identity allows a retry after either unlink was
    # interrupted. Never remove an unknown replacement or a loaded owner.
    if launcher and (launcher.exists() or launcher.is_symlink()):
        file_info(launcher, 0o700)
        if launcher.read_bytes() != launcher_bytes(state, receipt["python"]):
            raise ValueError("launcher changed; nothing was removed")
        launcher.unlink()
    if PLIST.exists():
        PLIST.unlink()
    print(json.dumps({"uninstalled": True, "retained_state": str(state)}))


def configure_controls(state, action, allowed=None, confirm=None):
    """Explicit stopped policy/credential maintenance; never a disk operation."""
    expected_program(state)
    if validate_job(state) is not None or read_definition(state).get("Disabled") is not True:
        raise ValueError("stop helper before changing controls")
    if disabled_override() is False:
        raise ValueError("native startup intent must be disabled before changing controls")
    module = collector_module(state / "app/apple_raid_status.py")
    if not hasattr(module, "EjectControls"):
        raise ValueError("install the reviewed control-capable release first")
    config, monitoring = module.load_config(state / "config.json", require_control=action == "controls-enable")
    before = private_file(state / "config.json")
    if action == "controls-enable":
        if not allowed:
            raise ValueError("controls-enable requires --allow-eject UUID")
        policy = {"enabled": True, "raids": [module.control_uuid(value) for value in allowed]}
        module.control_policy({"controls": policy})
        secret = state / "control-token"
        if secret.exists() or secret.is_symlink():
            file_info(secret)
            token = private_file(secret).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) or token == monitoring:
                raise ValueError("invalid distinct control token")
        else:
            atomic_private(secret, (secrets.token_urlsafe(48) + "\n").encode())
        config["controls"] = policy
    elif action == "controls-disable":
        config.pop("controls", None)  # Retain token and uncertain-action evidence.
    elif action == "controls-clear":
        if confirm is None:
            raise ValueError("controls-clear requires --confirm UUID after physical inspection")
        raid_uuid = module.control_uuid(confirm)
        controls = module.EjectControls(config, None, state, __import__("threading").Lock())
        data = controls._load()
        # Preserve completed request IDs, including those associated with an
        # uncertain operation. Clearing acknowledges it; it does not retry it.
        for record in data["requests"]:
            if record["raid"] == raid_uuid and record["state"] != "finished":
                record["state"] = "finished"
                if record["outcome"] is None:
                    record["outcome"] = "unknown"
        controls._save(data)
        print(json.dumps({"controls_cleared": True, "storage_changed": False}))
        return
    else:
        raise ValueError("invalid controls action")
    if private_file(state / "config.json") != before:
        raise ValueError("configuration changed during control update")
    atomic_private(state / "config.json", (json.dumps(config, indent=2) + "\n").encode())
    module.load_config(state / "config.json")
    print(json.dumps({"controls_enabled": action == "controls-enable", "storage_changed": False,
                      "monitoring_token_preserved": True}))


def serve(state):
    expected_program(state, require_runtime=True)
    snapshot = validate_job(state)
    definition = read_definition(state)
    override = disabled_override()
    if (definition.get("Disabled") is not False or override is True or snapshot is None
            or job_fields(snapshot).get("pid") != str(os.getpid())):
        raise RuntimeError("serve requires this enabled launchd job; use start")
    # Keep the same foreground PID owned by launchd. No child supervisor, retry
    # scheduler or detached collector is introduced by the friendly launcher.
    module = collector_module(state / "app/apple_raid_status.py")
    sys.argv = [str(state / "app/apple_raid_status.py"), "--config", str(state / "config.json")]
    return module.main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "restart", "status", "check", "uninstall", "serve", "controls-enable", "controls-disable", "controls-clear"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--launcher", type=Path, help="absolute friendly executable path; install only")
    parser.add_argument("--bind", help="IPv4 address; new installation default 127.0.0.1")
    parser.add_argument("--port", type=int, help="new installation default 8788")
    parser.add_argument("--expect", action="append", help="expected RAID UUID; repeat for multiple sets")
    parser.add_argument("--allow-eject", action="append", help="explicit control RAID UUID; controls-enable only")
    parser.add_argument("--confirm", help="inspected RAID UUID; controls-clear only")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.getuid() == 0:
        parser.error("run as your ordinary macOS user, without sudo")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer required")
    if args.action != "install" and any(v is not None for v in (args.launcher, args.bind, args.port, args.expect)):
        parser.error("--launcher, --bind, --port, and --expect apply only to install")
    if args.allow_eject is not None and args.action != "controls-enable":
        parser.error("--allow-eject applies only to controls-enable")
    if args.confirm is not None and args.action != "controls-clear":
        parser.error("--confirm applies only to controls-clear")
    state = args.state_dir.expanduser().absolute()
    if args.action in ("status", "check", "serve"):
        checked_directory(state, private=True, create=False)
        if args.action == "serve":
            return serve(state)
        status(state, strict=args.action == "check")
    else:
        with lifecycle_lock():
            if args.action == "install":
                install(state, args)
            else:
                checked_directory(state, private=True, create=False)
                if args.action.startswith("controls-"):
                    configure_controls(state, args.action, args.allow_eject, args.confirm)
                elif args.action == "start":
                    start(state)
                elif args.action == "restart":
                    stop(state)
                    start(state)
                elif args.action == "stop":
                    stop(state)
                elif args.action == "uninstall":
                    uninstall(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
