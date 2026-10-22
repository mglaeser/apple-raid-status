import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import apple_raid_status as app
import install as lifecycle

RAID = "00000000-0000-4000-8000-000000000001"
MEMBER1 = "00000000-0000-4000-8000-000000000002"
MEMBER2 = "00000000-0000-4000-8000-000000000003"
CONFIG = {"bind": "127.0.0.1", "port": 8788, "interval_seconds": 30,
          "timeout_seconds": 10, "stale_after_seconds": 90, "expected_raids": [RAID]}
TOKEN = "synthetic_test_token_not_a_real_secret_123456789"


def fixture():
    return {"AppleRAIDSets": [{"AppleRAIDSetUUID": RAID, "Name": "Example mirror", "BSD Name": "disk6",
        "Level": "Mirror", "Status": "Online", "Rebuild": "Automatic", "Size": 2000000000000,
        "Members": [{"AppleRAIDMemberUUID": MEMBER1, "BSD Name": "disk4s2", "MemberStatus": "Online"},
                    {"AppleRAIDMemberUUID": MEMBER2, "BSD Name": "disk5s2", "MemberStatus": "Online"}], "Spares": []}]}


def apfs():
    return {"Containers": [{"ContainerReference": "disk7", "CapacityCeiling": 2000000000000,
        "CapacityFree": 1500000000000, "PhysicalStores": [{"DeviceIdentifier": "disk6"}],
        "Volumes": [{"DeviceIdentifier": "disk7s1", "Name": "Example volume"}]}]}


def run_fixture(args):
    if args == ["appleRAID", "list", "-plist"]:
        return fixture()
    if args == ["apfs", "list", "-plist"]:
        return apfs()
    if args == ["info", "-plist", "disk7s1"]:
        return {"DeviceIdentifier": "disk7s1", "APFSContainerReference": "disk7", "MountPoint": "/Volumes/Example"}
    raise AssertionError("unexpected command: " + repr(args))


class CollectorTests(unittest.TestCase):
    def test_healthy_realistic_plist_and_apfs_capacity(self):
        result = app.collect([RAID], run=run_fixture)[0]
        self.assertTrue(result["healthy"])
        self.assertEqual(result["members_summary"], "2/2")
        self.assertEqual(result["volume"]["used_percent"], 25)
        self.assertEqual(result["volume"]["scope"], "container")
        self.assertIsNone(result["rebuild_percent"])

    def test_states_never_misreport_healthy(self):
        for state, health in (("Failed", "degraded"), ("Missing", "degraded"),
                             ("Offline", "degraded"), ("Rebuilding", "rebuilding"),
                             ("17.4% (Rebuilding)", "rebuilding"), ("Mystery", "unknown")):
            with self.subTest(state=state):
                raw = fixture()["AppleRAIDSets"][0]
                raw["Members"][1]["MemberStatus"] = state
                result = app.normalize_raid(raw)
                self.assertFalse(result["healthy"])
                self.assertEqual(result["health"], health)
                self.assertEqual(result["online_members"], 1)
                self.assertEqual(result["rebuild_percent"], 17.4 if state.startswith("17.4") else None)

    def test_degraded_top_level_and_missing_member_device(self):
        raw = fixture()["AppleRAIDSets"][0]
        raw["Status"] = "Degraded"
        self.assertFalse(app.normalize_raid(raw)["healthy"])
        raw["Members"][1].pop("BSD Name")
        with self.assertRaises(ValueError):
            app.normalize_raid(raw)
        raw["Members"][1]["MemberStatus"] = "Missing"
        self.assertEqual(app.normalize_raid(raw)["members"][1]["health"], "missing")

    def test_malformed_and_duplicate_members_rejected(self):
        for field, value in (("Members", None), ("Size", True), ("Status", ""), ("BSD Name", "disk6;rm -rf /")):
            raw = fixture()["AppleRAIDSets"][0]
            raw[field] = value
            with self.subTest(field=field):
                if field == "Size":
                    self.assertFalse(app.normalize_raid(raw)["healthy"])
                else:
                    with self.assertRaises(ValueError):
                        app.normalize_raid(raw)
        raw = fixture()["AppleRAIDSets"][0]
        raw["Members"].append(raw["Members"][0])
        with self.assertRaises(ValueError):
            app.normalize_raid(raw)

    def test_empty_and_missing_expected(self):
        self.assertEqual(app.collect(run=lambda _: {"AppleRAIDSets": []}), [])
        raids = app.collect([RAID], run=lambda _: {"AppleRAIDSets": []})
        self.assertEqual(raids[0]["health"], "missing")
        self.assertFalse(raids[0]["healthy"])

    def test_duplicate_or_invalid_set_list_rejected(self):
        for raw in ({}, {"AppleRAIDSets": None}, {"AppleRAIDSets": fixture()["AppleRAIDSets"] * 2}):
            with self.assertRaises(ValueError):
                app.collect(run=lambda _: raw)

    def test_apfs_does_not_confuse_unrelated_or_shared_physical_stores(self):
        raid = app.normalize_raid(fixture()["AppleRAIDSets"][0])
        container = apfs()["Containers"][0]
        container["PhysicalStores"].append({"DeviceIdentifier": "disk9"})
        self.assertEqual(app.volume_for(raid, [container], run_fixture)["status"], "ambiguous")
        container["PhysicalStores"] = [{"DeviceIdentifier": "disk9"}]
        volume = app.volume_for(raid, [container], lambda _: {"DeviceIdentifier": "disk6"})
        self.assertIsNone(volume["free_bytes"])
        self.assertEqual(volume["status"], "unavailable")

    def test_mount_and_capacity_not_invented(self):
        raid = app.normalize_raid(fixture()["AppleRAIDSets"][0])
        no_mount = lambda _: {"DeviceIdentifier": "disk7s1", "APFSContainerReference": "disk7"}
        volume = app.volume_for(raid, apfs()["Containers"], no_mount)
        self.assertFalse(volume["mounted"])
        with self.assertRaises(ValueError):
            app.volume_for(raid, apfs()["Containers"], lambda _: {"DeviceIdentifier": "disk7s2"})
        container = apfs()["Containers"][0]
        container["CapacityFree"] = container["CapacityCeiling"] + 1
        self.assertEqual(app.volume_for(raid, [container], no_mount)["status"], "unavailable")

    def test_no_shell_and_timeout_passed_to_native_tool(self):
        payload = plistlib.dumps(fixture())
        with patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, payload, b"")) as command:
            self.assertEqual(app.diskutil(["appleRAID", "list", "-plist"], 2), fixture())
        args, kw = command.call_args
        self.assertEqual(args[0], ["/usr/sbin/diskutil", "appleRAID", "list", "-plist"])
        self.assertEqual(kw["timeout"], 2)
        self.assertNotIn("shell", kw)

    def test_whole_collection_deadline(self):
        with patch.object(app.time, "monotonic", side_effect=[0, 1, 31]):
            with self.assertRaises(subprocess.TimeoutExpired):
                app.collect(run=run_fixture)

    def test_mirror_byte_progress_uses_raid_capacity_not_percent_or_free_space(self):
        raw = fixture()["AppleRAIDSets"][0]
        member = raw["Members"][1]
        member["MemberStatus"] = "Rebuilding"
        member["MemberRebuildStatus"] = 25
        result = app.normalize_raid(raw)
        self.assertEqual(result["rebuild_percent"], 25 * 100 / raw["Size"])
        self.assertNotEqual(result["rebuild_percent"], 25)
        self.assertEqual(result["members"][1]["rebuild_percent"], result["rebuild_percent"])
        member["MemberRebuildStatus"] = raw["Size"] // 4
        result = app.collect(run=lambda args: {"AppleRAIDSets": [raw]}
                             if args == ["appleRAID", "list", "-plist"] else run_fixture(args))[0]
        self.assertEqual(result["rebuild_percent"], 25)
        self.assertEqual(result["rebuild_summary"], "25%")

    def test_mirror_byte_progress_endpoints_do_not_change_native_health(self):
        for state in ("Rebuilding", "Resyncing"):
            for fraction in (0, 1):
                with self.subTest(state=state, fraction=fraction):
                    raw = fixture()["AppleRAIDSets"][0]
                    raw["Members"][1].update(MemberStatus=state, MemberRebuildStatus=raw["Size"] * fraction)
                    result = app.normalize_raid(raw)
                    self.assertEqual(result["rebuild_percent"], fraction * 100)
                    self.assertEqual(result["health"], "rebuilding")
                    self.assertTrue(result["rebuilding"])
                    self.assertFalse(result["healthy"])
                    self.assertEqual(result["online_members"], 1)

    def test_invalid_mirror_byte_counters_stay_unknown(self):
        raw = fixture()["AppleRAIDSets"][0]
        for value in (None, False, True, "25", 25.0, -1, raw["Size"] + 1, 2**63):
            with self.subTest(value=value):
                raw["Members"][1].update(MemberStatus="Rebuilding", MemberRebuildStatus=value)
                result = app.normalize_raid(raw)
                self.assertIsNone(result["rebuild_percent"])
                self.assertIsNone(result["members"][1]["rebuild_percent"])
                self.assertEqual(result["rebuild_summary"], "In progress; percentage unavailable")

    def test_invalid_mirror_capacity_keeps_byte_progress_unknown(self):
        for value in (None, False, True, "100", 100.0, -1, 0, 2**63):
            with self.subTest(value=value):
                raw = fixture()["AppleRAIDSets"][0]
                raw["Size"] = value
                raw["Members"][1].update(MemberStatus="Rebuilding", MemberRebuildStatus=0)
                result = app.normalize_raid(raw)
                self.assertIsNone(result["rebuild_percent"])
                self.assertFalse(result["healthy"])

    def test_explicit_progress_precedes_byte_counter_and_capacity_validation(self):
        for level in ("Mirror", "Stripe"):
            for capacity in (None, 2000000000000):
                for state, expected in (("17.4% (Rebuilding)", 17.4), ("0% (Resyncing)", 0),
                                        ("100% (Rebuilding)", 100), ("101% (Rebuilding)", None)):
                    with self.subTest(level=level, capacity=capacity, state=state):
                        raw = fixture()["AppleRAIDSets"][0]
                        raw.update(Level=level, Size=capacity)
                        raw["Members"][1].update(MemberStatus=state, MemberRebuildStatus=0)
                        result = app.normalize_raid(raw)
                        self.assertEqual(result["rebuild_percent"], expected)
                        self.assertEqual(result["health"], "rebuilding")
                        self.assertFalse(result["healthy"])

    def test_byte_progress_not_inferred_for_other_levels_or_inactive_members(self):
        for level in ("Stripe", "Concat", "Concatenated", "Unknown"):
            with self.subTest(level=level):
                raw = fixture()["AppleRAIDSets"][0]
                raw["Level"] = level
                raw["Members"][1].update(MemberStatus="Rebuilding", MemberRebuildStatus=100)
                self.assertIsNone(app.normalize_raid(raw)["rebuild_percent"])
        for state in ("Online", "Failed", "Missing", "Offline", "Mystery"):
            with self.subTest(state=state):
                raw = fixture()["AppleRAIDSets"][0]
                raw["Members"][1].update(MemberStatus=state, MemberRebuildStatus=100)
                result = app.normalize_raid(raw)
                self.assertIsNone(result["members"][1]["rebuild_percent"])
                self.assertIsNone(result["rebuild_percent"])
                self.assertFalse(result["rebuilding"])
        raw["Status"] = "Rebuilding"
        self.assertIsNone(app.normalize_raid(raw)["rebuild_percent"])

    def test_multiple_rebuilding_members_require_all_progress_before_minimum(self):
        raw = fixture()["AppleRAIDSets"][0]
        raw["Members"][0].update(MemberStatus="Rebuilding", MemberRebuildStatus=raw["Size"] // 4)
        raw["Members"][1].update(MemberStatus="Resyncing", MemberRebuildStatus=raw["Size"] // 2)
        self.assertEqual(app.normalize_raid(raw)["rebuild_percent"], 25)
        raw["Members"][1]["MemberStatus"] = "17.4% (Resyncing)"
        self.assertEqual(app.normalize_raid(raw)["rebuild_percent"], 17.4)
        raw["Members"][1]["MemberStatus"] = "Rebuilding"
        raw["Members"][1].pop("MemberRebuildStatus")
        self.assertIsNone(app.normalize_raid(raw)["rebuild_percent"])
        raw["Members"][1]["MemberRebuildStatus"] = -1
        self.assertIsNone(app.normalize_raid(raw)["rebuild_percent"])

    def test_spare_byte_progress_does_not_infer_member_rebuilding(self):
        raw = fixture()["AppleRAIDSets"][0]
        raw["Spares"] = [{"AppleRAIDMemberUUID": "00000000-0000-4000-8000-000000000004",
                          "BSD Name": "disk8s2", "MemberStatus": "Rebuilding", "MemberRebuildStatus": 25}]
        result = app.normalize_raid(raw)
        self.assertIsNone(result["spares"][0]["rebuild_percent"])
        self.assertIsNone(result["rebuild_percent"])
        self.assertFalse(result["rebuilding"])

    def test_unrecognized_progress_field_is_not_a_percent_or_supported_counter(self):
        raw = fixture()["AppleRAIDSets"][0]
        raw["Members"][1]["MemberStatus"] = "Rebuilding"
        raw["Members"][1]["AppleRAID-Rebuild-Progress"] = 123456789
        self.assertIsNone(app.normalize_raid(raw)["rebuild_percent"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.cache = app.Cache(CONFIG, probe=lambda: app.collect(run=run_fixture), clock=lambda: self.now)

    def test_starting_and_fresh(self):
        self.assertEqual(self.cache.snapshot()[0]["health"], "error")
        self.cache.refresh()
        self.assertTrue(self.cache.snapshot()[0]["healthy"])

    def test_stale_samples_and_clock_do_not_say_healthy(self):
        self.cache.refresh()
        self.now = 91
        data, _ = self.cache.snapshot()
        self.assertTrue(data["stale"])
        self.assertFalse(data["healthy"])
        self.assertFalse(data["raids"][0]["healthy"])
        self.assertEqual(data["raids"][0]["observed_health"], "healthy")

    def test_error_keeps_old_timestamp_but_invalidates_health_then_recovers(self):
        self.cache.refresh()
        original = self.cache.snapshot()[0]["collected_at"]
        def fail():
            raise subprocess.TimeoutExpired("diskutil", 2)
        self.cache.probe = fail
        self.cache.refresh()
        data, _ = self.cache.snapshot()
        self.assertEqual(data["error"], "command_timeout")
        self.assertFalse(data["collection_ok"])
        self.assertFalse(data["raids"][0]["healthy"])
        self.assertEqual(data["collected_at"], original)
        self.cache.probe = lambda: app.collect(run=run_fixture)
        self.cache.refresh()
        self.assertTrue(self.cache.snapshot()[0]["healthy"])

    def test_uuid_selection_missing_and_response_isolation(self):
        self.cache.refresh()
        data, found = self.cache.snapshot(RAID)
        self.assertTrue(found)
        data["raid"]["members"].clear()
        self.assertEqual(len(self.cache.snapshot(RAID)[0]["raid"]["members"]), 2)
        missing, found = self.cache.snapshot(MEMBER1)
        self.assertFalse(found)
        self.assertEqual(missing["health"], "missing")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.cache = app.Cache(CONFIG, probe=lambda: app.collect(run=run_fixture))
        self.cache.refresh()
        self.server = app.Server(("127.0.0.1", 0), self.cache, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, path, token=TOKEN, method="GET", extra=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        headers.update(extra or {})
        request = urllib.request.Request(self.base + path, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_auth_liveness_and_no_token_leaks(self):
        self.assertEqual(self.get("/healthz", None)[0], 200)
        for token in (None, "bad", "", TOKEN[:-1]):
            code, headers, body = self.get("/api/v1/status", token)
            self.assertEqual(code, 401)
            self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.get("/api/v1/status")[0], 200)

    def test_real_http_routes_and_read_only(self):
        code, headers, body = self.get("/api/v1/raids/" + RAID)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["raid"]["healthy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.get("/api/v1/raids/" + MEMBER1)[0], 404)
        self.assertEqual(self.get("/unknown")[0], 404)
        self.assertEqual(self.get("/api/v1/status?token=bad")[0], 400)
        for method in ("POST", "DELETE", "PUT", "PATCH", "OPTIONS"):
            self.assertEqual(self.get("/api/v1/status", method=method)[0], 405)

    def test_browser_basic_auth_and_head(self):
        auth = "Basic " + base64.b64encode(("status:" + TOKEN).encode()).decode()
        self.assertEqual(self.get("/", None, extra={"Authorization": auth})[0], 200)
        self.assertEqual(self.get("/", None, extra={"Authorization": "Basic bad="})[0], 401)
        code, _, body = self.get("/", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")

    def test_polling_never_runs_collector_and_ready_is_not_raid_health(self):
        self.cache.raids[0]["health"] = "degraded"
        self.cache.raids[0]["healthy"] = False
        self.cache.probe = lambda: self.fail("HTTP request triggered collection")
        for _ in range(5):
            code, _, body = self.get("/readyz")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["health"], "degraded")
        self.cache.error = "collection_failed"
        self.assertEqual(self.get("/readyz")[0], 503)
        self.assertEqual(self.get("/api/v1/status")[0], 200)

    def test_handlers_bounded(self):
        for _ in range(8):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        self.assertFalse(self.server.slots.acquire(blocking=False))
        for _ in range(8):
            self.server.slots.release()

    def test_malformed_absolute_url_returns_400_without_traceback(self):
        # urllib validates URLs before sending; use a raw socket to exercise
        # the server's handling of a malformed request-target from a peer.
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with socket.create_connection(self.server.server_address, timeout=2) as peer:
                peer.sendall(b"GET http://[ HTTP/1.1\r\nHost: example.invalid\r\nConnection: close\r\n\r\n")
                chunks = []
                while True:
                    part = peer.recv(4096)
                    if not part:
                        break
                    chunks.append(part)
            response = b"".join(chunks)
            self.assertEqual(response.split(b"\r\n", 1)[0], b"HTTP/1.0 400 Bad Request")
            self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), {"error": "invalid_path"})
            self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(self.get("/healthz", None)[0], 200)


class HomepageContractTests(unittest.TestCase):
    def test_shipped_example_fields_resolve_for_fresh_and_failed_observations(self):
        example = (Path(__file__).resolve().parents[1] / "examples/services.yaml").read_text()
        # This is a field-contract check, not a replacement YAML parser. Keep
        # this simple shipped example dependency-free and exercise its paths
        # against the same payload a real HTTP client receives.
        fields = re.findall(r"^\s+- field:\s*([a-z_]+(?:\.[a-z_]+)*)\s*$", example, re.M)
        self.assertEqual(fields, ["health", "raid.members_summary", "raid.volume.free_bytes", "raid.rebuild_summary"])
        self.assertIn("{{HOMEPAGE_VAR_APPLE_RAID_TOKEN}}", example)
        self.assertNotIn(TOKEN, example)
        cache = app.Cache(CONFIG, probe=lambda: app.collect(run=run_fixture))
        cache.refresh()

        def resolve(payload, field):
            for segment in field.split("."):
                payload = payload[segment]
            return payload

        payload, found = cache.snapshot(RAID)
        self.assertTrue(found)
        self.assertEqual([resolve(payload, f) for f in fields],
                         ["healthy", "2/2", 1500000000000, "Not rebuilding"])
        cache.error = "collection_failed"
        payload, _ = cache.snapshot(RAID)
        values = [resolve(payload, f) for f in fields]
        self.assertEqual(values[0], "error")
        self.assertEqual(values[3], "Observation unavailable")



if __name__ == "__main__":
    unittest.main()
