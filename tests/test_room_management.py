"""Portal contract tests over real Unix sockets with the resident's SQLite alarms."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.client import HTTPConnection, RemoteDisconnected
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from vector_puck.alarms import Alarms
from vector_puck.config import Config, example
from vector_puck.ring_signals import IndicatorLease, WEATHER_KINDS
from vector_puck.room_management import decorate_indicator, make_room_api


DEFAULT = dict(brightness=80, color="white", spoilers="ask")


def http(path, method="GET", target="/state", body=None, headers=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        stream.connect(str(path))
        client = HTTPConnection("localhost")
        client.sock = stream
        try:
            data = (
                body if isinstance(body, bytes) else json.dumps(body) if body is not None else None
            )
            # A header-only rejection can arrive before HTTPConnection sends its body.
            try:
                client.request(
                    method, target, data, {"Content-Type": "application/json", **(headers or {})}
                )
            except BrokenPipeError:
                pass
            with client.getresponse() as response:
                return response.status, json.load(response)
        finally:
            client.close()


class RoomManagementTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.directory = self.root / "sockets"
        self.now = datetime.fromisoformat("2026-11-17T06:59:00-06:00").timestamp()
        self.alarms = Alarms(
            self.root / "alarms.sqlite3", clock=lambda: self.now, timezone="America/Chicago"
        )
        self.api = self.make()
        self.api.start()
        self.addCleanup(self.api.close)
        self.path = self.directory / "office.sock"

    def make(self, **kwargs):
        args = dict(
            state_dir=self.root, alarms=self.alarms, room_id="office", socket_dir=self.directory
        )
        return make_room_api(**(args | kwargs))

    def post(self, route, body, headers=None):
        return http(self.path, "POST", route, body, headers)

    def state(self):
        status, state = http(self.path)
        self.assertEqual(status, 200)
        return state

    def test_health_exposes_only_fresh_public_fields(self):
        offline = dict(online=False, muted=True, version="unknown", alarms=[], preferences=DEFAULT)
        health = self.root / "health.json"
        self.assertEqual(self.state(), offline)
        sample = dict(
            updated=time.time(),
            connected=True,
            muted=False,
            native_version="0.14.0",
            unrelated="room-local",
        )
        health.write_text(json.dumps(sample))
        self.assertEqual(self.state(), dict(offline, online=True, muted=False, version="0.14.0"))
        health.write_text(json.dumps(dict(sample, muted=True)))
        self.assertEqual(self.state()["muted"], True)
        for delta in [
            dict(updated=time.time() - 60),
            dict(updated=time.time() + 60),
            dict(connected=False),
            dict(muted=1),
            dict(updated=float("nan")),
        ]:
            with self.subTest(delta=delta):
                health.write_text(json.dumps(sample | delta))
                self.assertEqual(self.state(), offline)
        health.unlink()
        source = self.root / "other.json"
        source.write_text(json.dumps(sample))
        health.symlink_to(source)
        self.assertEqual(self.state(), offline)

    def test_two_rooms_share_only_the_socket_directory(self):
        den = self.root / "den"
        api = self.make(room_id="den", state_dir=den, alarms=Alarms(den / "alarms.sqlite3"))
        api.start()
        self.addCleanup(api.close)
        self.assertEqual(self.post("/preferences", {"color": "green"})[0], 200)
        self.assertEqual(
            self.post("/alarms", {"action": "set", "name": "School", "minutes": 5})[0], 200
        )
        self.assertEqual(http(api.socket_path)[1]["preferences"], DEFAULT)
        self.assertEqual(http(api.socket_path)[1]["alarms"], [])
        self.api.close()
        self.assertEqual(http(api.socket_path)[0], 200)

    def test_callback_custom_health_filename_and_flat_import(self):
        from vector_puck import room_management

        spec = importlib.util.spec_from_file_location("flat_management", room_management.__file__)
        flat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flat)
        args = dict(
            alarms=self.alarms, state_dir=self.root, socket_dir=self.directory, room_id="den"
        )
        sample = dict(connected=True, updated=time.time(), muted=False, version="build-1")
        (self.root / "device.json").write_text(json.dumps(sample))
        for options in [dict(health_filename="device.json"), dict(health=lambda: sample)]:
            with self.subTest(options=options):
                api = flat.make_room_api(**args, **options)
                api.start()
                try:
                    self.assertEqual(http(api.socket_path)[1]["version"], "build-1")
                finally:
                    api.close()

    def test_recurring_alarm_create_update_ring_snooze_dismiss_and_cancel(self):
        args = dict(
            action="set", name="School", at="07:15", days=[0, 1, 2, 3, 4], date=None, minutes=None
        )
        status, created = self.post("/alarms", args, {"If-None-Match": "*"})
        self.assertEqual(status, 200)
        (first,) = created["alarms"]
        self.assertEqual(
            first,
            dict(
                id=first["id"],
                name="School",
                at="07:15",
                days=args["days"],
                date=None,
                status="scheduled",
                due=self.now + 16 * 60,
            ),
        )
        status, changed = self.post(
            "/alarms", args | dict(name="school", at="07:00"), {"If-Match": '"' + first["id"] + '"'}
        )
        self.assertEqual(status, 200)
        (updated,) = changed["alarms"]
        self.assertNotEqual(updated["id"], first["id"])
        self.assertEqual(updated["due"], self.now + 60)
        self.assertEqual(self.state()["alarms"], [updated])
        self.assertEqual(
            self.post(
                "/alarms",
                dict(action="cancel", name="School"),
                {"If-Match": '"' + first["id"] + '"'},
            )[0],
            412,
        )
        self.now += 61
        self.assertEqual(self.alarms.tick()[0]["id"], updated["id"])
        self.assertEqual(self.state()["alarms"][0]["status"], "ringing")
        status, snoozed = self.post("/alarms", dict(action="snooze", name="School", minutes=2))
        self.assertEqual(status, 200)
        self.assertEqual(snoozed["alarms"][0]["due"], self.now + 120)
        self.now += 121
        self.alarms.tick()
        status, dismissed = self.post("/alarms", dict(action="dismiss", name="School"))
        self.assertEqual(status, 200)
        (recurring,) = dismissed["alarms"]
        self.assertEqual(recurring["due"], updated["due"] + 86400)
        self.assertEqual(recurring["status"], "scheduled")
        self.assertEqual(self.post("/alarms", dict(action="cancel", name="School"))[0], 200)
        self.assertEqual(self.state()["alarms"], [])
        self.assertEqual(self.alarms.ringing(), [])

    def test_competing_creates_and_edits_only_apply_once(self):
        args = dict(action="set", name="wake", minutes=2)
        headers = {"If-None-Match": "*"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.post("/alarms", args, headers)[0], range(2)))
            self.assertEqual(sorted(results), [200, 412])
            (current,) = self.state()["alarms"]
            headers = {"If-Match": '"' + current["id"] + '"'}
            results = list(
                pool.map(lambda m: self.post("/alarms", args | dict(minutes=m), headers)[0], [3, 4])
            )
            self.assertEqual(sorted(results), [200, 412])
        (current,) = self.state()["alarms"]
        self.alarms.handle(dict(args, minutes=5, at=None, date=None, days=[]))
        status, _ = self.post(
            "/alarms", dict(action="cancel", name="wake"), {"If-Match": '"' + current["id"] + '"'}
        )
        self.assertEqual(status, 412)
        self.assertEqual(self.state()["alarms"][0]["due"], self.now + 300)

    def test_partial_preferences_are_atomic_durable_and_live(self):
        self.assertEqual(self.state()["preferences"], DEFAULT)
        endpoint = "/preferences"
        self.assertEqual(self.post(endpoint, dict(color="cyan"))[0], 200)
        path = self.root / "room-preferences.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with path.open() as before:
            with ThreadPoolExecutor(max_workers=2) as pool:
                changes = [dict(spoilers="avoid"), dict(brightness=20)]
                self.assertEqual(
                    list(pool.map(lambda v: self.post(endpoint, v)[0], changes)), [200, 200]
                )
            self.assertEqual(json.load(before), dict(DEFAULT, color="cyan"))
        expected = dict(color="cyan", brightness=20, spoilers="avoid")
        returned = self.api.preferences()
        self.assertEqual(returned, expected)
        returned["color"] = "violet"
        for invalid in [
            dict(color="cyan", brightness=True),
            dict(brightness=20.0),
            dict(brightness=99),
            dict(color="red"),
            dict(color=[]),
            dict(spoilers="system"),
            dict(muted=False),
            dict(color="green", unknown=1),
            {},
            [],
            None,
            b'{"color":"cyan","color":"violet"}',
        ]:
            with self.subTest(body=invalid):
                self.assertEqual(self.post(endpoint, invalid)[0], 400)
                self.assertEqual(self.api.preferences(), expected)
                self.assertEqual(json.loads(path.read_text()), expected)
        with patch("vector_puck.room_management.os.replace", side_effect=OSError):
            self.assertEqual(self.post(endpoint, dict(color="green"))[0], 500)
        self.assertEqual(self.api.preferences(), expected)
        self.assertEqual(json.loads(path.read_text()), expected)
        self.api.close()
        fresh = self.make()
        self.addCleanup(fresh.close)
        fresh.start()
        self.assertEqual(self.state()["preferences"], expected)

    def test_portal_sha256_revisions_guard_alarms_and_preferences(self):
        def revision(value):
            data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
            return {"If-Match": '"' + hashlib.sha256(data).hexdigest() + '"'}

        prefs = revision(self.state()["preferences"])
        self.assertEqual(self.post("/preferences", dict(spoilers="allow"), prefs)[0], 200)
        self.assertEqual(self.post("/preferences", DEFAULT, prefs)[0], 412)
        self.assertEqual(self.state()["preferences"]["spoilers"], "allow")
        cmd = dict(action="set", name="Wake", minutes=1)
        self.assertEqual(self.post("/alarms", cmd, {"If-None-Match": "*"})[0], 200)
        old = revision(self.state()["alarms"][0])
        self.now += 61
        self.alarms.tick()  # Same ID, but changed from scheduled to ringing.
        self.assertEqual(self.post("/alarms", cmd, old)[0], 412)
        current = revision(self.state()["alarms"][0])
        self.assertEqual(self.post("/alarms", dict(action="dismiss", name="Wake"), current)[0], 200)

    def test_alarm_input_and_http_boundaries(self):
        for args in [
            dict(action="shell"),
            dict(action="status"),
            dict(action="set", minutes=True),
            dict(action="set", at="7:00"),
            dict(action="set", minutes=0),
            dict(action="set", at="07:00", minutes=1),
            dict(action="set", at="07:00", days=[True]),
            dict(action="set", at="07:00", days=[7]),
            dict(action="set", at="07:00", days=[1, 1]),
            dict(action="set", at="07:00", date="../etc"),
            dict(action="set", at="07:00", date="2026-02-30"),
            dict(action="set", at="07:00", date="2026-11-18", days=[1]),
            dict(action="set", name="x\ny", minutes=1),
            dict(action="set", name="x" * 61, minutes=1),
            dict(action="cancel", at="07:00"),
            dict(action="dismiss", minutes=1),
            dict(action="snooze", minutes=61),
            dict(action="set", at="09:30", room_id="den"),
            b'{"action":"set","action":"cancel"}',
            b"{}{}",
            [],
            None,
        ]:
            with self.subTest(args=args):
                self.assertEqual(self.post("/alarms", args)[0], 400)
        for target in [
            "/state?room=den",
            "/../den/state",
            "/%73tate",
            "//state",
            "/home",
            "/shell",
        ]:
            self.assertEqual(http(self.path, target=target)[0], 404)
        self.assertEqual(self.post("/state", {})[0], 405)
        self.assertEqual(http(self.path, target="/alarms")[0], 405)
        self.assertEqual(self.post("/preferences", b" " * 4097)[0], 413)
        self.assertEqual(self.post("/alarms", {}, {"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.post("/alarms", {}, {"Content-Type": "text/plain"})[0], 415)
        for headers in [{"If-Match": "*"}, {"If-None-Match": "bad"}]:
            self.assertEqual(self.post("/alarms", dict(action="set", minutes=1), headers)[0], 400)
        self.assertEqual(self.state()["alarms"], [])

    def test_mode_same_uid_live_owner_and_shutdown(self):
        self.assertTrue(stat.S_ISSOCK(self.path.stat().st_mode))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(self.path.stat().st_uid, os.geteuid())
        with self.assertRaises(ValueError):
            self.make().start()
        self.assertEqual(http(self.path)[0], 200)
        # The connected peer UID comes from the kernel. Compare it to a different
        # server UID to exercise the denial branch even on an unprivileged CI runner.
        with patch("vector_puck.room_management.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises((RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
                http(self.path)
        with socket.socket(socket.AF_UNIX) as slow:
            slow.connect(str(self.path))
            slow.sendall(b"GET /state HTTP/1.1\r\n")
            self.api.close()
        self.assertFalse(self.path.exists())
        self.api.close()

    def test_stale_socket_recovery_and_foreign_paths_preserved(self):
        self.api.close()
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(self.path))
        self.api.start()
        self.assertEqual(http(self.path)[0], 200)
        self.path.unlink()
        self.path.write_text("keep")
        self.api.close()
        self.assertEqual(self.path.read_text(), "keep")
        with self.assertRaises(ValueError):
            self.api.start()
        self.assertEqual(self.path.read_text(), "keep")
        self.path.unlink()
        self.path.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            self.api.start()
        self.assertTrue(self.path.is_symlink())

    def test_factory_and_strict_config(self):
        self.assertIsNone(self.make(socket_dir=None))
        self.assertIsNone(self.make(socket_dir=""))
        self.assertEqual(example()["management_socket_dir"], "")
        self.assertEqual(Config().management_socket_dir, "")
        cfg = self.root / "config.json"
        cfg.write_text(
            json.dumps(
                dict(
                    household_file="/config/office.json", management_socket_dir=str(self.directory)
                )
            )
        )
        self.assertEqual(Config.load(cfg).management_socket_dir, str(self.directory))
        for invalid in [None, 1, "relative", "/", "/tmp/../store"]:
            with self.subTest(directory=invalid), self.assertRaises(ValueError):
                Config(household_file="/config/office.json", management_socket_dir=invalid)
        with self.assertRaises(ValueError):
            Config(management_socket_dir="/run/rooms")
        for bad in ["", "../office", "Office", "two/rooms", "x" * 25, "é", None, "office\n"]:
            with self.subTest(room=bad), self.assertRaises(ValueError):
                self.make(room_id=bad)
        link = self.root / "link"
        link.symlink_to(self.directory)
        for args in [
            dict(socket_dir="relative"),
            dict(socket_dir=self.root / ("x" * 110)),
            dict(socket_dir=link),
            dict(socket_dir=self.root / "a/../b"),
            dict(health_filename="../health.json"),
            dict(health_filename="/file"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.make(**args)
        loose = self.root / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        with self.assertRaises(ValueError):
            self.make(socket_dir=loose).start()
        self.assertEqual(list(loose.iterdir()), [])

    def test_indicator_preferences_and_lease_refresh(self):
        prefs = self.api.preferences()
        msg = dict(kind="message", duration_ms=60000)
        lease = IndicatorLease()
        lease.sent(decorate_indicator(msg, prefs), now=10)
        self.post("/preferences", dict(color="violet", brightness=50))
        prefs = self.api.preferences()
        styled = decorate_indicator(msg, prefs)
        self.assertEqual(styled, dict(msg, color="violet", brightness_pct=50))
        self.assertTrue(lease.due(styled, now=11))
        lease.sent(styled, now=11)
        self.assertFalse(lease.due(styled, now=12))
        for kind in WEATHER_KINDS:
            original = dict(msg, kind=kind)
            self.assertEqual(decorate_indicator(original, prefs), dict(original, brightness_pct=50))
        for kind in ["alarm", "call", "none", "update", "updated", "mute"]:
            original = dict(kind=kind)
            self.assertEqual(decorate_indicator(original, prefs), original)
        self.assertNotIn("color", msg)


class ResidentIntegrationTests(unittest.TestCase):
    def test_public_run_starts_api_before_session_and_closes_it_on_error(self):
        from vector_puck import resident
        from vector_puck.ambient_runtime import AmbientRuntime
        from vector_puck.household_runtime import RoomRuntime

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state, directory = root / "room-state", root / "sockets"
            household = root / "household.json"
            household.write_text(
                json.dumps(
                    dict(
                        root=str(root / "shared"),
                        room="office",
                        timezone="UTC",
                        rooms=[dict(id=r, name=r.title(), aliases=[]) for r in ("office", "den")],
                    )
                )
            )
            household.chmod(0o600)
            config = Config(household_file=str(household), management_socket_dir=str(directory))
            path = directory / "office.sock"

            async def exercise(_config, _speech, active_dialogue, _state):
                prefs = dict(spoilers="allow", color="amber", brightness=50)
                self.assertEqual(http(path, "POST", "/preferences", prefs)[0], 200)
                self.assertEqual(active_dialogue.room_api.preferences()["spoilers"], "allow")
                self.assertEqual(active_dialogue.essentials.room_preferences()["spoilers"], "allow")
                self.assertEqual(
                    http(path, "POST", "/alarms", dict(action="set", minutes=1))[0], 200
                )
                satellite = resident.Satellite(None, None, config, active_dialogue, asyncio.Event())
                satellite.native_indicator = True
                satellite.gate.muted = False
                hub = active_dialogue.household
                hub.household.announce(["office"], "Test announcement")
                room = RoomRuntime(hub.household, satellite, active_dialogue, alarms=hub.alarms)
                satellite.room_runtime = room
                try:
                    ambient = AmbientRuntime(active_dialogue.essentials, satellite)
                    self.assertIn("amber pulse", ambient.describe_ring())
                    sent = []

                    async def capture(event, payload):
                        sent.append((event, payload))
                        satellite.stop.set()

                    with patch.object(satellite, "send", capture):
                        await asyncio.wait_for(room.maintain(), 2)
                    self.assertEqual(
                        sent,
                        [
                            (
                                "vector-indicator",
                                dict(
                                    kind="message",
                                    duration_ms=60000,
                                    brightness_pct=50,
                                    color="amber",
                                ),
                            )
                        ],
                    )
                finally:
                    room.audio.close()
                raise RuntimeError("end session loop")

            with (
                patch.object(Config, "validate_runtime"),
                patch.object(resident, "read_key"),
                patch.object(resident, "Speech"),
                patch.object(resident, "_run_sessions", exercise),
                self.assertRaisesRegex(RuntimeError, "end session"),
            ):
                asyncio.run(resident.run(config, state))
            self.assertFalse(path.exists())
            alarms = Alarms(state / "alarms.sqlite3")
            self.assertIn(
                "alarm,",
                alarms.handle(
                    dict(action="status", name=None, at=None, days=[], date=None, minutes=None)
                ),
            )
