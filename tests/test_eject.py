"""Synthetic storage-control fixtures: never eject, unmount or query a real disk."""
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import apple_raid_status as app

RAID = "00000000-0000-4000-8000-000000000001"
MEMBERS = ["00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000003"]
CONTAINER = "00000000-0000-4000-8000-000000000004"
VOLUMES = ["00000000-0000-4000-8000-000000000005", "00000000-0000-4000-8000-000000000006"]
REQUEST = "AAAAAAAA-0000-4000-8000-000000000001"
REQUEST2 = "AAAAAAAA-0000-4000-8000-000000000002"
TOKEN = "synthetic_monitoring_token_not_a_secret_0123456789"
CONTROL = "synthetic_control_token_not_a_secret_9876543210"
CONFIG = {"bind": "127.0.0.1", "port": 8788, "interval_seconds": 30, "timeout_seconds": 10,
          "stale_after_seconds": 90, "expected_raids": [RAID], "controls": {"enabled": True, "raids": [RAID]}}


class Storage:
    def __init__(self):
        self.calls, self.actions, self.present, self.mounted = [], [], {"disk4", "disk5"}, True
        self.raid = {"AppleRAIDSetUUID": RAID, "BSD Name": "disk6", "Name": "Synthetic mirror", "Level": "Mirror",
                     "Status": "Online", "Size": 2000000000, "Rebuild": "Automatic", "Spares": [],
                     "Members": [{"AppleRAIDMemberUUID": m, "BSD Name": f"disk{4+i}s2", "MemberStatus": "Online"}
                                 for i, m in enumerate(MEMBERS)]}
        self.container = {"APFSContainerUUID": CONTAINER, "ContainerReference": "disk7", "CapacityCeiling": 2000000000,
                          "CapacityFree": 1000000000, "PhysicalStores": [{"DeviceIdentifier": "disk6"}],
                          "Volumes": [{"APFSVolumeUUID": v, "DeviceIdentifier": f"disk7s{i+1}", "Name": "Fixture",
                                       "Roles": [] if i == 0 else ["Backup"], "CryptoMigrationOn": False, "Locked": False}
                                      for i, v in enumerate(VOLUMES)]}
        self.info, self.layouts = {}, {}
        external = {"Internal": False, "OSInternalMedia": False, "SystemImage": False, "Ejectable": True,
                    "RAIDMaster": False, "RAIDSlice": False, "MountPoint": "", "WholeDisk": False}
        self.info["disk6"] = {**external, "DeviceIdentifier": "disk6", "RAIDMaster": True, "RAIDSetUUID": RAID}
        for n, member in zip((4, 5), MEMBERS):
            whole = f"disk{n}"
            self.info[whole] = {**external, "DeviceIdentifier": whole, "WholeDisk": True, "VirtualOrPhysical": "Physical",
                                "Content": "GUID_partition_scheme", "DeviceTreePath": f"/synthetic/usb/{n}", "TotalSize": 2100000000}
            partitions = []
            for i, content in enumerate(("EFI", "Apple_RAID", "Apple_Boot"), start=1):
                bsd = f"disk{n}s{i}"
                partition_uuid = f"00000000-0000-4000-8000-0000000000{n}{i}"
                partitions.append({"DeviceIdentifier": bsd, "DiskUUID": partition_uuid, "Content": content})
                self.info[bsd] = {**external, "DeviceIdentifier": bsd, "DiskUUID": partition_uuid, "ParentWholeDisk": whole,
                                  "Content": content}
                if content == "Apple_RAID":
                    self.info[bsd].update(RAIDSlice=True, RAIDSetUUID=RAID, RAIDMemberUUID=member,
                                          RAIDMemberStatus="Online", RAIDSetStatus="Online", RAIDSetLevelType="Mirror",
                                          BooterDeviceIdentifier=f"disk{n}s3")
            self.layouts[whole] = {"DeviceIdentifier": whole, "Content": "GUID_partition_scheme", "Partitions": partitions}
        for n in (1, 2):
            self.info[f"disk7s{n}"] = {**external, "DeviceIdentifier": f"disk7s{n}", "APFSContainerReference": "disk7", "VolumeUUID": VOLUMES[n-1]}

    def read(self, args):
        self.calls.append(list(args))
        if args == ["appleRAID", "list", "-plist"]:
            return {"AppleRAIDSets": [copy.deepcopy(self.raid)] if self.present else []}
        if args == ["apfs", "list", "-plist"]:
            return {"Containers": [copy.deepcopy(self.container)] if self.present else []}
        if args[:2] == ["info", "-plist"]:
            value = copy.deepcopy(self.info[args[2]])
            if args[2].startswith("disk7s"):
                value["MountPoint"] = "/Volumes/Synthetic" if self.mounted else ""
            return value
        if args == ["list", "-plist"]:
            return {"AllDisks": sorted(self.present), "AllDisksAndPartitions": [self.layouts[p] for p in sorted(self.present)]}
        if args[:2] == ["list", "-plist"] and args[2] in self.present:
            return {"AllDisksAndPartitions": [copy.deepcopy(self.layouts[args[2]])]}
        raise AssertionError("unexpected read-only command: " + repr(args))

    def mutate(self, args):
        self.actions.append(list(args))
        if args[0] == "unmountDisk":
            self.mounted = False
        elif args[0] == "eject":
            self.present.remove(args[-1])
        else:
            raise AssertionError("unexpected mutation fixture")
        return 0


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name).resolve()
        self.state.chmod(0o700)
        self.storage = Storage()
        self.lock = threading.Lock()
        self.controls = app.EjectControls(CONFIG, CONTROL, self.state, self.lock, self.storage.read, self.storage.mutate)
        self.uid = patch.object(app.os, "getuid", return_value=1000)
        self.uid.start()  # CI can run as root; native-control guard is tested separately.

    def tearDown(self):
        self.uid.stop()
        self.temp.cleanup()

    def eject(self, force=False, request=REQUEST):
        # Journal ownership checking uses the actual fixture owner.
        with patch.object(app, "private_file", side_effect=lambda p: Path(p).read_text()):
            return self.controls.eject(RAID, force, request)

    def test_normal_eject_is_unmount_then_exact_two_physical_disks(self):
        code, body = self.eject()
        self.assertEqual(code, 200)
        self.assertEqual(body["outcome"], "ejected")
        self.assertTrue(body["safe_to_disconnect"])
        self.assertEqual(self.storage.actions, [["unmountDisk", "disk6"], ["eject", "disk4"], ["eject", "disk5"]])
        self.assertEqual(self.storage.present, set())
        data = json.loads((self.state / "control-state.json").read_text())
        self.assertEqual(data["requests"][0]["state"], "finished")
        self.assertEqual((self.state / "control-state.json").stat().st_mode & 0o777, 0o600)

    def test_force_is_explicit_on_every_action_never_an_automatic_retry(self):
        self.assertEqual(self.eject(force=True)[0], 200)
        self.assertEqual(self.storage.actions, [["unmountDisk", "force", "disk6"], ["eject", "force", "disk4"], ["eject", "force", "disk5"]])

    def test_active_rebuild_blocks_normal_and_force_before_any_write(self):
        self.storage.raid["Members"][1]["MemberStatus"] = "Rebuilding"
        for force in (False, True):
            code, body = self.eject(force=force)
            self.assertEqual((code, body["reason"]), (409, "rebuild_in_progress"))
        self.assertEqual(self.storage.actions, [])
        self.assertFalse((self.state / "control-state.json").exists())

    def test_unknown_offline_degraded_spares_and_non_mirror_fail_closed(self):
        cases = [("Status", "Unknown"), ("Status", "Offline"), ("Status", "Degraded"), ("Level", "Stripe"),
                 ("Spares", [{"AppleRAIDMemberUUID": VOLUMES[0], "BSD Name": "disk9s2", "MemberStatus": "Online"}])]
        for key, value in cases:
            original = self.storage.raid[key]
            self.storage.raid[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.eject(force=True)[0], 409)
                self.assertFalse(self.storage.actions)
            self.storage.raid[key] = original

    def test_unrelated_partition_internal_media_and_boot_roles_refused(self):
        changes = [(self.storage.info["disk4"], "Internal", True),
                   (self.storage.info["disk4"], "VirtualOrPhysical", "Virtual"),
                   (self.storage.info["disk4s2"], "RAIDSetUUID", VOLUMES[0]),
                   (self.storage.info["disk4s2"], "BooterDeviceIdentifier", "disk99s3"),
                   (self.storage.container["Volumes"][0], "Roles", ["System"])]
        for target, key, value in changes:
            old = target[key]; target[key] = value
            with self.subTest(key=key):
                self.assertEqual(self.eject()[0], 409)
                self.assertFalse(self.storage.actions)
            target[key] = old
        self.storage.layouts["disk4"]["Partitions"].append({"DeviceIdentifier": "disk4s4", "Content": "Apple_APFS"})
        self.assertEqual(self.eject()[0], 409)
        self.assertFalse(self.storage.actions)

    def test_concurrent_requests_and_collection_share_lock(self):
        self.lock.acquire()
        try:
            self.assertEqual(self.eject()[1]["reason"], "operation_busy")
            self.assertEqual(self.controls.eligibility(RAID)["reason"], "operation_busy")
        finally:
            self.lock.release()
        self.assertFalse(self.storage.actions)

    def test_duplicate_request_rejected_even_after_failed_unmount(self):
        self.controls.mutate = lambda args: self.storage.actions.append(args) or 1
        self.assertEqual(self.eject()[1]["reason"], "unmount_refused")
        self.assertEqual(self.eject()[1]["reason"], "duplicate_request")
        self.assertEqual(len(self.storage.actions), 1)
        self.assertEqual(self.eject(force=True, request=REQUEST2)[1]["reason"], "unmount_refused")
        self.assertEqual(len(self.storage.actions), 2)

    def test_unmount_failure_does_not_eject_or_silently_force(self):
        self.controls.mutate = lambda args: self.storage.actions.append(args) or 1
        code, body = self.eject()
        self.assertEqual((code, body["reason"]), (409, "unmount_refused"))
        self.assertEqual(self.storage.actions, [["unmountDisk", "disk6"]])
        self.assertFalse(body["safe_to_disconnect"])

    def test_unmount_success_but_volume_still_mounted_blocks_members(self):
        self.controls.mutate = lambda args: self.storage.actions.append(args) or 0
        code, body = self.eject()
        self.assertEqual((code, body["outcome"]), (503, "unknown"))
        self.assertEqual(body["reason"], "volumes_still_mounted")
        self.assertEqual(len(self.storage.actions), 1)
        self.assertEqual(self.eject(request=REQUEST2)[1]["reason"], "inspection_required")

    def test_second_member_failure_is_partial_and_persistent(self):
        normal = self.storage.mutate
        def action(args):
            return 1 if args[-1] == "disk5" else normal(args)
        self.controls.mutate = action
        code, body = self.eject()
        self.assertEqual((code, body["outcome"], body["members_ejected"]), (503, "partial", 1))
        self.assertTrue(body["inspection_required"])
        self.assertFalse(body["safe_to_disconnect"])
        # New controller simulates service restart; persistent uncertainty remains.
        with patch.object(app.os, "getuid", return_value=self.state.stat().st_uid):
            self.controls = app.EjectControls(CONFIG, CONTROL, self.state, self.lock, self.storage.read, normal)
        self.assertEqual(self.eject(request=REQUEST2)[1]["reason"], "inspection_required")

    def test_native_timeout_marks_unknown_never_retries(self):
        def action(args):
            self.storage.actions.append(args)
            raise subprocess.TimeoutExpired("synthetic action", 65)
        self.controls.mutate = action
        code, body = self.eject()
        self.assertEqual((code, body["outcome"]), (503, "unknown"))
        self.assertEqual(len(self.storage.actions), 1)
        self.assertEqual(self.eject(request=REQUEST2)[1]["reason"], "inspection_required")

    def test_identity_changed_after_unmount_prevents_member_eject(self):
        normal = self.storage.mutate
        def action(args):
            code = normal(args)
            self.storage.info["disk4"]["DeviceTreePath"] = "/replacement/device"
            return code
        self.controls.mutate = action
        self.assertEqual(self.eject()[1]["reason"], "identity_changed")
        self.assertEqual(self.storage.actions, [["unmountDisk", "disk6"]])

    def test_rebuild_starting_during_unmount_blocks_first_physical_eject(self):
        normal = self.storage.mutate
        def action(args):
            code = normal(args)
            self.storage.raid["Members"][1]["MemberStatus"] = "Rebuilding"
            return code
        self.controls.mutate = action
        code, body = self.eject(force=True)
        self.assertEqual((code, body["reason"]), (503, "rebuild_in_progress"))
        self.assertEqual(self.storage.actions, [["unmountDisk", "force", "disk6"]])

    def test_new_spare_after_first_eject_blocks_second_physical_eject(self):
        normal = self.storage.mutate
        def action(args):
            code = normal(args)
            if args[0] == "eject":
                self.storage.raid["Spares"] = [{"AppleRAIDMemberUUID": VOLUMES[0], "BSD Name": "disk9s2", "MemberStatus": "Online"}]
            return code
        self.controls.mutate = action
        code, body = self.eject()
        self.assertEqual((code, body["outcome"]), (503, "partial"))
        self.assertEqual(self.storage.actions, [["unmountDisk", "disk6"], ["eject", "disk4"]])

    def test_journal_failure_prevents_native_mutation(self):
        with patch.object(self.controls, "_save", side_effect=OSError("fixture disk full")):
            self.assertEqual(self.eject()[0], 503)
        self.assertFalse(self.storage.actions)

    def test_root_disabled_and_wrong_uuid_fail_closed(self):
        with patch.object(app.os, "getuid", return_value=0):
            self.assertEqual(self.eject()[1]["reason"], "root_forbidden")
        self.controls.policy["enabled"] = False
        self.assertEqual(self.eject()[1]["reason"], "controls_disabled")
        self.controls.policy["enabled"] = True
        self.controls.policy["raids"] = [VOLUMES[0]]
        self.assertEqual(self.eject()[1]["reason"], "raid_not_allowed")
        self.assertFalse(self.storage.actions)

    def test_pending_journal_from_crash_blocks_later_mutations(self):
        self.controls._save({"schema_version": 1, "requests": [{"id": REQUEST, "raid": RAID, "time": time.time(),
                             "state": "started", "outcome": None}]})
        self.assertEqual(self.eject(request=REQUEST2)[1]["reason"], "inspection_required")
        self.assertFalse(self.storage.actions)


class ControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name).resolve()
        self.state.chmod(0o700)
        self.cache = app.Cache(CONFIG, probe=lambda: [])
        self.controls = app.EjectControls(CONFIG, CONTROL, self.state, self.cache.operation_lock)
        self.server = app.Server(("127.0.0.1", 0), self.cache, TOKEN, self.controls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.action = patch.object(self.controls, "eject", return_value=(200, {"outcome": "ejected"}))
        self.mock_action = self.action.start()

    def tearDown(self):
        self.action.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, auth=CONTROL, headers=None, body=None, suffix="/eject", method="POST"):
        data = json.dumps({"force": False, "confirm": RAID} if body is None else body).encode()
        fields = {"Authorization": "Bearer " + auth, "Content-Type": "application/json", "Idempotency-Key": REQUEST.lower()}
        fields.update(headers or {})
        request = urllib.request.Request(self.base + "/api/v1/raids/" + RAID + suffix, data=data, headers=fields, method=method)
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_exact_control_request_reaches_executor_with_canonical_uuid(self):
        self.assertEqual(self.request()[0], 200)
        self.mock_action.assert_called_once_with(RAID, False, REQUEST)

    def test_monitoring_token_cannot_control_and_control_cannot_read(self):
        self.assertEqual(self.request(auth=TOKEN)[0], 401)
        self.assertEqual(self.request(auth=CONTROL, suffix="", method="GET")[0], 401)
        self.mock_action.assert_not_called()

    def test_basic_auth_cross_origin_forms_and_invalid_confirmation_rejected(self):
        import base64
        basic = "Basic " + base64.b64encode(("status:" + CONTROL).encode()).decode()
        cases = [({"Authorization": basic}, None, 401), ({"Origin": "http://example.invalid"}, None, 400),
                 ({"Sec-Fetch-Site": "same-origin"}, None, 400), ({"Content-Type": "text/plain"}, None, 400),
                 ({"Idempotency-Key": "not-a-uuid"}, None, 400), ({}, {"force": 1, "confirm": RAID}, 400),
                 ({}, {"force": True, "confirm": VOLUMES[0]}, 400), ({}, {"force": True, "confirm": RAID, "command": "anything"}, 400)]
        for headers, body, expected in cases:
            with self.subTest(headers=headers, body=body):
                self.assertEqual(self.request(headers=headers, body=body)[0], expected)
        self.mock_action.assert_not_called()

    def test_no_get_side_effect_and_no_other_control_methods(self):
        with patch.object(self.controls, "eligibility", return_value={"eligible": True}) as preflight:
            self.assertEqual(self.request(auth=TOKEN, method="GET")[0], 200)
            preflight.assert_called_once_with(RAID)
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.request(method=method)[0], 405)
        self.mock_action.assert_not_called()

    def test_force_requires_explicit_boolean_and_query_forms_never_dispatch(self):
        self.assertEqual(self.request(body={"force": True, "confirm": RAID})[0], 200)
        self.mock_action.assert_called_once_with(RAID, True, REQUEST)
        self.mock_action.reset_mock()
        self.assertEqual(self.request(suffix="/eject?force=true")[0], 404)
        self.mock_action.assert_not_called()


class CommandRunnerTests(unittest.TestCase):
    def test_command_allowlist_rejects_paths_extra_flags_and_other_verbs(self):
        with patch.object(app.subprocess, "Popen") as native:
            for args in (["eraseDisk", "disk4"], ["eject", "/Volumes/Test"], ["eject", "-force", "disk4"],
                         ["eject", "disk4;whoami"], ["eject", "force", "disk4", "disk5"]):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    app.action_command(args)
            native.assert_not_called()

    def test_fixed_absolute_command_discards_output_and_returns_exit_status(self):
        import io
        from unittest.mock import MagicMock
        process = MagicMock()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"synthetic native output\n"); os.close(write_fd)
        process.stdout = os.fdopen(read_fd, "rb")
        process.wait.return_value = 7
        process.poll.return_value = 7
        with patch.object(app.subprocess, "Popen", return_value=process) as native:
            self.assertEqual(app.action_command(["eject", "force", "disk4"]), 7)
        self.assertEqual(native.call_args.args[0], ["/usr/sbin/diskutil", "eject", "force", "disk4"])
        self.assertNotIn("shell", native.call_args.kwargs)
        self.assertTrue(native.call_args.kwargs["start_new_session"])

    def test_timeout_kills_only_created_process_group_and_reports_unknown(self):
        from unittest.mock import MagicMock
        process = MagicMock(); process.pid = 12345; process.poll.return_value = None
        read_fd, write_fd = os.pipe(); process.stdout = os.fdopen(read_fd, "rb")
        try:
            with patch.object(app.subprocess, "Popen", return_value=process), patch.object(app.os, "killpg") as kill:
                with self.assertRaises(subprocess.TimeoutExpired):
                    app.action_command(["unmountDisk", "disk6"], timeout=0)
                kill.assert_called_once_with(12345, app.signal.SIGKILL)
                process.wait.assert_called_once_with(timeout=1)
        finally:
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
