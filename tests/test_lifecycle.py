"""Lifecycle fixtures: never bootstrap, stop, or inspect a real launchd job."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import apple_raid_status as app
import install as lifecycle

RAID = "00000000-0000-4000-8000-000000000001"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.plist = self.root / "LaunchAgents" / (lifecycle.LABEL + ".plist")
        self.native_snapshot = None
        self.override = None
        self.native_calls = []
        self.patches = [patch.object(lifecycle, "PLIST", self.plist),
                        patch.object(lifecycle, "LOCK_DIR", self.root / "locks"),
                        patch.object(lifecycle, "loaded", side_effect=lambda: self.native_snapshot),
                        patch.object(lifecycle, "disabled_override", side_effect=lambda: self.override),
                        patch.object(lifecycle, "launchctl", side_effect=self.native),
                        patch.object(lifecycle, "command", side_effect=self.command),
                        patch("sys.stdout", new_callable=io.StringIO)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def native(self, *arguments, **kwargs):
        self.native_calls.append(arguments)
        if arguments[0] == "enable":
            self.override = False
        elif arguments[0] == "disable":
            self.override = True
        elif arguments[0] in ("bootstrap", "kickstart"):
            self.native_snapshot = self.snapshot()
        elif arguments[0] == "bootout":
            self.native_snapshot = None
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def command(self, arguments, **kwargs):
        if arguments[0] == "/usr/bin/git":
            return subprocess.CompletedProcess(arguments, 0, "a" * 40 + "\n", "")
        self.fail("unexpected native command: " + repr(arguments))

    def install(self, **kwargs):
        args = {"bind": "127.0.0.1", "port": 8788, "expect": [RAID], "launcher": None}
        args.update(kwargs)
        lifecycle.install(self.state, SimpleNamespace(**args))

    def definition(self):
        return plistlib.loads(self.plist.read_bytes())

    def snapshot(self, arguments=None, program=None, plist_path=None, state="running", pid="1234"):
        arguments = arguments or self.definition()["ProgramArguments"]
        return (f"{lifecycle.JOB} = {{\n\tpath = {plist_path or self.plist}\n"
                f"\tprogram = {program or arguments[0]}\n\targuments = {{\n"
                + "".join("\t\t" + value + "\n" for value in arguments)
                + f"\t}}\n\tstate = {state}\n" + (f"\tpid = {pid}\n" if pid else "") + "}\n")

    def ready(self):
        return {"version": lifecycle.VERSION, "collection": "ready", "raid_health": "healthy", "age_seconds": 1}

    def start(self):
        with patch.object(lifecycle, "inspect_listener"), patch.object(lifecycle, "readiness", return_value=self.ready()):
            lifecycle.start(self.state)

    def make_legacy(self):
        self.install()
        receipt = lifecycle.read_receipt(self.state)
        Path(receipt["launcher"]).unlink()
        receipt = {key: receipt[key] for key in ("version", "python", "sha256", "source_commit")}
        receipt["version"] = "0.1.0"
        (self.state / "receipt.json").write_text(json.dumps(receipt))
        self.plist.write_bytes(plistlib.dumps(lifecycle.expected_plist(self.state, receipt)))
        return receipt

    def test_fresh_prepare_is_private_stopped_and_never_calls_native_mutations(self):
        self.install()
        token = (self.state / "token").read_bytes()
        definition = self.definition()
        self.assertIs(definition["Disabled"], True)
        self.assertEqual(definition["ProgramArguments"], [str(self.state / "bin/AppleRAID Status"), "serve"])
        self.assertNotIn("Program", definition)
        self.assertEqual((self.state / "bin/AppleRAID Status").stat().st_mode & 0o777, 0o700)
        self.assertEqual(lifecycle.status(self.state, quiet=True)["service"], "stopped")
        self.install()
        self.assertEqual((self.state / "token").read_bytes(), token)
        self.assertEqual(self.native_calls, [])

    def test_start_persists_before_enable_and_bootstrap_and_stop_before_disable(self):
        self.install()
        original = self.native
        def observe(*arguments, **kwargs):
            if arguments[0] in ("enable", "bootstrap"):
                self.assertIs(self.definition()["Disabled"], False)
            elif arguments[0] in ("disable", "bootout"):
                self.assertIs(self.definition()["Disabled"], True)
            return original(*arguments, **kwargs)
        with patch.object(lifecycle, "launchctl", side_effect=observe):
            self.start()
            lifecycle.stop(self.state)
        self.assertEqual([c[0] for c in self.native_calls], ["enable", "bootstrap", "disable", "bootout"])
        self.assertIsNone(self.native_snapshot)
        # Simulate login/reboot losing the macOS override: plist intent survives.
        self.override = None
        result = lifecycle.status(self.state, quiet=True)
        self.assertTrue(result["configured_disabled"] and result["effective_disabled"])
        with self.assertRaisesRegex(RuntimeError, "not enabled and running"):
            lifecycle.status(self.state, quiet=True, strict=True)

    def test_enabled_intent_survives_absent_override_and_prepare(self):
        self.install()
        self.start()
        self.override = None
        self.install()
        with patch.object(lifecycle, "readiness", return_value=self.ready()):
            result = lifecycle.status(self.state, quiet=True)
        self.assertIs(result["configured_disabled"], False)
        self.assertIs(result["effective_disabled"], False)
        self.assertEqual(result["raid_health"], "healthy")

    def test_stopped_prepare_preserves_explicit_os_disable(self):
        self.install()
        lifecycle.persist_disabled(self.state, False)
        self.override = True
        self.install()
        self.assertIs(self.definition()["Disabled"], True)
        self.assertEqual(self.native_calls, [])

    def test_start_loaded_waiting_job_enables_and_kickstarts_exact_job(self):
        self.install()
        self.native_snapshot = self.snapshot(state="waiting", pid=None)
        self.override = True
        self.start()
        self.assertEqual([c[0] for c in self.native_calls], ["enable", "kickstart"])

    def test_running_prepare_rejects_changed_runtime_without_writing(self):
        self.install()
        self.start()
        before = {p: p.read_bytes() for p in [self.plist, self.state / "receipt.json", self.state / "token", self.state / "app/install.py"]}
        different = self.root / "different-python"
        different.write_bytes(b"a different interpreter")
        with patch.object(lifecycle.sys, "executable", str(different)), patch.object(lifecycle, "atomic_private", wraps=lifecycle.atomic_private) as write:
            with self.assertRaisesRegex(ValueError, "stop helper before updating"):
                self.install()
            write.assert_not_called()
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_program_override_and_extra_launch_triggers_rejected_before_start(self):
        self.install()
        original = self.definition()
        for extra in ({"Program": "/usr/bin/false"}, {"WatchPaths": ["/tmp"]}, {"Disabled": "false"}):
            self.plist.write_bytes(plistlib.dumps({**original, **extra}))
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                lifecycle.start(self.state)
        self.assertEqual(self.native_calls, [])

    def test_loaded_identity_requires_exact_arguments_program_and_path(self):
        self.install()
        arguments = self.definition()["ProgramArguments"]
        cases = [self.snapshot(arguments=arguments[:-1] + ["serve-other"]),
                 self.snapshot(arguments=arguments[::-1]), self.snapshot(arguments=arguments + ["--once"]),
                 self.snapshot(program=arguments[0] + ".other"), self.snapshot(plist_path=str(self.plist) + ".other")]
        for snapshot in cases:
            self.native_snapshot = snapshot
            with self.assertRaisesRegex(ValueError, "loaded job does not match"):
                lifecycle.validate_job(self.state)

    def test_stop_with_missing_interpreter_and_tampered_collector_never_executes_it(self):
        self.install()
        self.start()
        receipt = lifecycle.read_receipt(self.state)
        receipt["python"] = str(self.root / "missing-python")
        (self.state / "receipt.json").write_text(json.dumps(receipt))
        installed = self.state / "app/apple_raid_status.py"
        installed.write_text("raise RuntimeError('must never run this collector')\n")
        with patch.object(lifecycle, "collector_module", side_effect=AssertionError("must not import assets")):
            lifecycle.stop(self.state)
        self.assertIs(self.definition()["Disabled"], True)
        self.assertEqual(self.native_calls[-2:], [("disable", lifecycle.JOB), ("bootout", lifecycle.JOB)])

    def test_intent_write_failure_runs_no_launchctl_mutation(self):
        self.install()
        self.start()
        self.native_calls.clear()
        with patch.object(lifecycle, "atomic_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                lifecycle.stop(self.state)
        self.assertEqual(self.native_calls, [])
        self.assertIs(self.definition()["Disabled"], False)

    def test_disable_failure_retains_stop_intent_and_does_not_bootout(self):
        self.install()
        self.start()
        self.native_calls.clear()
        with patch.object(lifecycle, "launchctl", return_value=subprocess.CompletedProcess([], 0, "", "")) as native:
            with self.assertRaisesRegex(RuntimeError, "disable was not confirmed"):
                lifecycle.stop(self.state)
        self.assertIs(self.definition()["Disabled"], True)
        self.assertIsNotNone(self.native_snapshot)
        native.assert_called_once_with("disable", lifecycle.JOB)

    def test_bootstrap_failure_preserves_explicit_enabled_intent(self):
        self.install()
        def fail(*arguments, **kwargs):
            if arguments[0] == "bootstrap":
                raise subprocess.CalledProcessError(5, arguments)
            return self.native(*arguments, **kwargs)
        with patch.object(lifecycle, "launchctl", side_effect=fail), patch.object(lifecycle, "inspect_listener"):
            with self.assertRaises(subprocess.CalledProcessError):
                lifecycle.start(self.state)
        self.assertIs(self.definition()["Disabled"], False)
        self.assertIs(self.override, False)

    def test_status_reports_stopped_and_readiness_errors_but_check_fails(self):
        self.install()
        result = lifecycle.status(self.state, quiet=True)
        self.assertEqual(result["collection"], "not_running")
        self.start()
        with patch.object(lifecycle, "readiness", side_effect=RuntimeError("stale")):
            self.assertEqual(lifecycle.status(self.state, quiet=True)["collection"], "unavailable")
            with self.assertRaisesRegex(RuntimeError, "stale"):
                lifecycle.status(self.state, quiet=True, strict=True)

    def test_listener_owned_pid_and_address_required_before_authentication(self):
        self.install()
        self.native_snapshot = self.snapshot()
        for output in ("p9999\nn127.0.0.1:8788\n", "p1234\nn0.0.0.0:8788\n", "p1234\nn127.0.0.1:8788\np9999\nn*:8788\n"):
            with patch.object(lifecycle, "command", return_value=subprocess.CompletedProcess([], 0, output, "")), \
                 patch.object(lifecycle.urllib.request, "build_opener") as http:
                with self.assertRaisesRegex(RuntimeError, "unexpected listener"):
                    lifecycle.readiness(self.state, self.native_snapshot)
                http.assert_not_called()
        with patch.object(lifecycle, "command", return_value=subprocess.CompletedProcess([], 0, "p1234\nn127.0.0.1:8788\n", "")):
            lifecycle.inspect_listener(self.state, self.native_snapshot, required=True)

    def test_foreign_listener_prevents_start_intent_changes(self):
        self.install()
        with patch.object(lifecycle, "command", return_value=subprocess.CompletedProcess([], 0, "p9999\nn127.0.0.1:8788\n", "")):
            with self.assertRaisesRegex(RuntimeError, "unexpected listener"):
                lifecycle.start(self.state)
        self.assertIs(self.definition()["Disabled"], True)
        self.assertEqual(self.native_calls, [])

    def test_fresh_prepare_refuses_existing_global_owner_or_enabled_override(self):
        self.install()
        other = self.root / "other-state"
        args = SimpleNamespace(bind=None, port=None, expect=None, launcher=None)
        before = self.plist.read_bytes()
        with self.assertRaisesRegex(ValueError, "another installation"):
            lifecycle.install(other, args)
        self.assertEqual(self.plist.read_bytes(), before)
        self.plist.unlink()
        self.override = False
        with self.assertRaisesRegex(ValueError, "enabled system override"):
            lifecycle.install(other, args)

    def test_kernel_lock_serializes_all_state_directories_and_releases_on_exception(self):
        with lifecycle.lifecycle_lock():
            with self.assertRaisesRegex(RuntimeError, "another lifecycle operation"):
                with lifecycle.lifecycle_lock():
                    self.fail("acquired duplicate label lock")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with lifecycle.lifecycle_lock():
                raise RuntimeError("interrupted")
        with lifecycle.lifecycle_lock():
            pass

    def test_legacy_migration_requires_stopped_exact_owner_and_preserves_secrets(self):
        self.make_legacy()
        secrets_before = [(self.state / name).read_bytes() for name in ("config.json", "token")]
        self.native_snapshot = self.snapshot()
        with self.assertRaisesRegex(ValueError, "stop helper before updating"):
            self.install()
        self.native_snapshot = None
        self.override = False  # Old stop did not clear this explicit enabled override.
        self.install()
        self.assertIs(self.definition()["Disabled"], True)
        self.assertEqual([(self.state / name).read_bytes() for name in ("config.json", "token")], secrets_before)
        self.assertEqual(lifecycle.read_receipt(self.state)["lifecycle_schema"], 2)
        self.assertIsNone(self.native_snapshot)
        self.assertIs(self.override, True)
        self.override = None
        self.assertIs(lifecycle.status(self.state, quiet=True)["effective_disabled"], True)

    def test_failed_legacy_disable_leaves_legacy_assets_intact(self):
        self.make_legacy()
        paths = (self.plist, self.state / "receipt.json", self.state / "app/install.py")
        before = {p: p.read_bytes() for p in paths}
        self.override = False
        with patch.object(lifecycle, "launchctl", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(RuntimeError, "migration disable was not confirmed"):
                self.install()
        self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_legacy_install_write_failures_restore_all_previous_identity_files(self):
        self.make_legacy()
        paths = [self.state / "app" / name for name in lifecycle.ASSETS]
        paths += [self.state / "bin/AppleRAID Status", self.state / "receipt.json", self.plist]
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        original = lifecycle.atomic_private
        for failing in paths:
            def fail(path, data, mode=0o600):
                if Path(path) == failing:
                    raise OSError("injected installation write failure")
                return original(path, data, mode)
            with self.subTest(failing=failing), patch.object(lifecycle, "atomic_private", side_effect=fail):
                with self.assertRaises(OSError):
                    self.install()
            self.assertEqual({p: p.read_bytes() if p.exists() else None for p in paths}, before)
            self.assertFalse((self.state / ".install-transaction").exists())
            self.assertIsNone(lifecycle.validate_job(self.state))
        self.install()
        self.assertIs(lifecycle.status(self.state, quiet=True)["effective_disabled"], True)

    def test_interrupted_install_recovers_on_retry_without_adopting_external_changes(self):
        self.make_legacy()
        before = (self.state / "receipt.json").read_bytes()
        original_write, original_recover = lifecycle.atomic_private, lifecycle.recover_install
        recover_calls = 0
        def skip_crash_recovery(state):
            nonlocal recover_calls
            recover_calls += 1
            if recover_calls > 1:
                raise OSError("simulated process interruption")
            return original_recover(state)
        def fail_plist(path, data, mode=0o600):
            if Path(path) == self.plist:
                raise OSError("injected final write failure")
            return original_write(path, data, mode)
        with patch.object(lifecycle, "atomic_private", side_effect=fail_plist), \
             patch.object(lifecycle, "recover_install", side_effect=skip_crash_recovery):
            with self.assertRaisesRegex(RuntimeError, "journal retained"):
                self.install()
        self.assertTrue((self.state / ".install-transaction/plan.json").exists())
        with self.assertRaisesRegex(ValueError, "interrupted installation"):
            lifecycle.stop(self.state)
        receipt = self.state / "receipt.json"
        partial = receipt.read_bytes()
        receipt.write_bytes(b"{}\n")
        with self.assertRaisesRegex(ValueError, "changed outside"):
            lifecycle.recover_install(self.state)
        self.assertTrue((self.state / ".install-transaction/plan.json").exists())
        receipt.write_bytes(partial)
        lifecycle.recover_install(self.state)
        self.assertEqual(receipt.read_bytes(), before)
        self.install()
        self.assertIs(lifecycle.status(self.state, quiet=True)["effective_disabled"], True)

    def test_prepared_install_failures_preserve_current_receipt_and_launcher(self):
        self.install()
        before = {p: p.read_bytes() for p in (self.plist, self.state / "receipt.json", self.state / "bin/AppleRAID Status")}
        original = lifecycle.atomic_private
        def fail_receipt(path, data, mode=0o600):
            if Path(path) == self.state / "receipt.json":
                raise OSError("receipt write failure")
            return original(path, data, mode)
        with patch.object(lifecycle, "atomic_private", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.install(launcher=self.root / "other bin/Friendly RAID")
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertFalse((self.root / "other bin/Friendly RAID").exists())
        self.assertIsNone(lifecycle.validate_job(self.state))

    def test_legacy_unknown_definition_cannot_be_adopted(self):
        self.make_legacy()
        definition = self.definition()
        definition["Program"] = "/usr/bin/false"
        self.plist.write_bytes(plistlib.dumps(definition))
        with self.assertRaisesRegex(ValueError, "identity/configuration differs"):
            self.install()
        self.assertEqual(self.native_calls, [])

    def test_uninstall_removes_owned_launcher_and_retains_state_idempotently(self):
        self.install()
        launcher = Path(lifecycle.read_receipt(self.state)["launcher"])
        config = (self.state / "config.json").read_bytes()
        lifecycle.uninstall(self.state)
        self.assertFalse(self.plist.exists())
        self.assertFalse(launcher.exists())
        self.assertEqual((self.state / "config.json").read_bytes(), config)
        self.assertTrue((self.state / "app/install.py").exists())
        lifecycle.uninstall(self.state)
        self.override = False  # Even an obsolete explicit enable cannot arm reinstall.
        self.install()
        self.assertIs(self.definition()["Disabled"], True)
        self.assertIs(self.override, True)

    def test_uninstall_retries_after_either_removal_was_interrupted(self):
        self.install()
        launcher = Path(lifecycle.read_receipt(self.state)["launcher"])
        self.plist.unlink()  # Simulate the old removal order stopping halfway.
        lifecycle.uninstall(self.state)
        self.assertFalse(launcher.exists())
        self.install()
        launcher.unlink()  # Simulate the new order stopping halfway.
        lifecycle.uninstall(self.state)
        self.assertFalse(self.plist.exists())

    def test_changed_launcher_is_preflighted_and_retired_only_after_valid_update(self):
        self.install()
        old = Path(lifecycle.read_receipt(self.state)["launcher"])
        new = self.root / "custom bin/Friendly RAID Service"
        old.write_text("#!/bin/sh\nexit 1\n")
        before = (self.state / "receipt.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "launcher does not match"):
            self.install(launcher=new)
        self.assertFalse(new.exists())
        self.assertEqual((self.state / "receipt.json").read_bytes(), before)
        old.write_bytes(lifecycle.launcher_bytes(self.state, lifecycle.read_receipt(self.state)["python"]))
        self.install(launcher=new)
        self.assertTrue(new.exists())
        self.assertFalse(old.exists())
        self.assertEqual(self.definition()["ProgramArguments"], [str(new), "serve"])

    def test_launcher_cannot_overwrite_collector_logs_or_installed_code(self):
        self.install()
        for path in (self.state / "collector.log", self.state / "collector.log.1", self.state / "app/extra.py"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "conflicts"):
                self.install(launcher=path)

    def test_unsafe_log_preflight_does_not_change_receipt(self):
        self.install()
        before = (self.state / "receipt.json").read_bytes()
        (self.state / "service.log").chmod(0o666)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual((self.state / "receipt.json").read_bytes(), before)

    def test_serve_requires_enabled_own_launchd_pid_and_runs_in_foreground(self):
        self.install()
        with self.assertRaisesRegex(RuntimeError, "serve requires"):
            lifecycle.serve(self.state)
        lifecycle.persist_disabled(self.state, False)
        self.override = False
        self.native_snapshot = self.snapshot(pid="999999")
        with self.assertRaisesRegex(RuntimeError, "serve requires"):
            lifecycle.serve(self.state)
        self.native_snapshot = self.snapshot(pid=str(os.getpid()))
        module = SimpleNamespace(main=lambda: 17)
        with patch.object(lifecycle, "collector_module", return_value=module), patch.object(sys, "argv", []):
            self.assertEqual(lifecycle.serve(self.state), 17)
            self.assertEqual(sys.argv[1:], ["--config", str(self.state / "config.json")])

    def test_launcher_works_without_source_checkout_and_fixes_embedded_state(self):
        self.install()
        launcher = self.state / "bin/AppleRAID Status"
        text = launcher.read_text()
        self.assertEqual(text.count("exec "), 1)
        self.assertNotIn(str(Path(lifecycle.__file__).parent), text)
        self.assertTrue(text.rstrip().endswith("--state-dir " + lifecycle.shlex.quote(str(self.state))))
        # --help exits before platform checks and never invokes any native tool.
        result = subprocess.run([str(launcher), "--help"], capture_output=True, text=True, timeout=5,
                                cwd=self.root, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/does-not-exist"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check", result.stdout)

    def test_refuses_symlinks_and_world_readable_secrets(self):
        self.install()
        token = self.state / "token"
        token.chmod(0o644)
        with self.assertRaises(ValueError):
            app.load_config(self.state / "config.json")
        token.chmod(0o600)
        link = self.root / "linked"
        link.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(ValueError):
            lifecycle.checked_directory(link)

    def test_asset_and_interpreter_tampering_are_rejected_for_start(self):
        self.install()
        with (self.state / "app/apple_raid_status.py").open("a") as stream:
            stream.write("\n# changed\n")
        with self.assertRaisesRegex(ValueError, "source does not match"):
            lifecycle.start(self.state)
        self.assertEqual(self.native_calls, [])

    def test_changed_interpreter_is_rejected_before_start_intent_changes(self):
        self.install()
        receipt = lifecycle.read_receipt(self.state)
        receipt["python_sha256"] = "0" * 64
        (self.state / "receipt.json").write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "interpreter changed"):
            lifecycle.start(self.state)
        self.assertIs(self.definition()["Disabled"], True)
        self.assertEqual(self.native_calls, [])

    def test_unexpected_config_rejected(self):
        self.install()
        original = json.loads((self.state / "config.json").read_text())
        for key, value in (("port", True), ("bind", "localhost"), ("expected_raids", ["disk6"]),
                           ("stale_after_seconds", 10), ("extra", 1)):
            changed = {**copy.deepcopy(original), key: value}
            (self.state / "config.json").write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                app.load_config(self.state / "config.json")


class NativeParsingTests(unittest.TestCase):
    def test_disabled_override_absent_true_false_and_malformed(self):
        for text, expected in (("disabled services = {}", None),
                               ('"' + lifecycle.LABEL + '" => true\n', True),
                               ('"' + lifecycle.LABEL + '" => false\n', False)):
            with patch.object(lifecycle, "launchctl", return_value=subprocess.CompletedProcess([], 0, text, "")):
                self.assertIs(lifecycle.disabled_override(), expected)
        for text in ('"' + lifecycle.LABEL + '" => maybe\n', ('"' + lifecycle.LABEL + '" => true\n') * 2):
            with patch.object(lifecycle, "launchctl", return_value=subprocess.CompletedProcess([], 0, text, "")):
                with self.assertRaises(ValueError):
                    lifecycle.disabled_override()

    def test_only_expected_launchctl_not_found_means_absent(self):
        with patch.object(lifecycle, "launchctl", return_value=subprocess.CompletedProcess([], 113, "", "Could not find service")):
            self.assertIsNone(lifecycle.loaded())
        with patch.object(lifecycle, "launchctl", return_value=subprocess.CompletedProcess([], 5, "", "permission denied")):
            with self.assertRaises(RuntimeError):
                lifecycle.loaded()


if __name__ == "__main__":
    unittest.main()
