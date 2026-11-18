import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest

from vector_puck.household import Household, SharedKitchen, parse, tools
from vector_puck.household_runtime import CallAudio, HEADER, RoomRuntime
from vector_puck.kitchen import Kitchen

ROOMS = [
    {"id": "office", "name": "Office", "aliases": ["original puck", "small puck"]},
    {"id": "kitchen", "name": "Kitchen", "aliases": ["kitchen echo"]},
]


class HouseholdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = Household(self.root / "house", "office", ROOMS)
        self.b = Household(self.root / "house", "kitchen", ROOMS)
        self.a.heartbeat(muted=False, busy=False)
        self.b.heartbeat(muted=False, busy=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_shared_after_restart_but_tasks_stay_local(self):
        a = SharedKitchen(Kitchen(self.root / "a.db"), self.root / "shopping.db")
        b = SharedKitchen(Kitchen(self.root / "b.db"), self.root / "shopping.db")
        a.handle("shopping_list", dict(action="add", items=["milk", "eggs"], offset=0))
        self.assertEqual([v["text"] for v in b.items()], ["milk", "eggs"])
        b.handle("shopping_list", dict(action="complete", items=["milk"], offset=0))
        self.assertEqual(Kitchen(self.root / "shopping.db").items()[1]["done"], 1)
        a.handle(
            "kitchen_task",
            dict(action="start", kind="timer", name="pasta", seconds=600, when=None, message=None),
        )
        self.assertEqual(len(a.tasks()), 1)
        self.assertEqual(b.tasks(), [])

    def test_simultaneous_wakes_choose_one_louder_room(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(
                    lambda pair: pair[0].nominate(pair[1], 0.12), [(self.a, 0.02), (self.b, 0.08)]
                )
            )
        self.assertEqual(results, [False, True])
        self.assertFalse(self.a.owns_turn())
        self.assertTrue(self.b.owns_turn())
        self.assertFalse(self.a.nominate(1, 0.01))

    def test_released_turn_tail_only_suppresses_the_other_room(self):
        self.assertTrue(self.a.nominate(.1, .01))
        self.a.release()
        self.assertFalse(self.b.nominate(.2, .01))
        self.assertTrue(self.a.nominate(.1, .01))
        self.assertTrue(self.a.owns_turn())

    def test_muted_candidate_cannot_win_and_does_not_block_other(self):
        self.b.heartbeat(muted=True, busy=False)
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(
                    lambda pair: pair[0].nominate(pair[1], 0.12), [(self.a, 0.02), (self.b, 1)]
                )
            )
        self.assertEqual(results, [True, False])

    def test_lost_coordinator_does_not_grant_ownership(self):
        self.assertFalse(self.a.owns_turn())
        self.a.token = "old"
        self.a.valid_until = time.time() - 1
        self.assertFalse(self.a.owns_turn())

    def test_mute_revokes_lease_immediately(self):
        self.assertTrue(self.a.nominate(0.02, 0.01))
        self.a.heartbeat(muted=True, busy=False)
        self.assertFalse(self.a.owns_turn())
        self.assertTrue(self.b.nominate(0.01, 0.01))

    def test_announcement_targets_persist_and_are_leased_once(self):
        self.a.announce(["everywhere"], "Dinner is ready")
        other = Household(self.root / "house", "kitchen", ROOMS)
        message = other.claim_message()
        self.assertEqual(message["text"], "Dinner is ready")
        self.assertIsNone(self.b.claim_message())
        other.finish_message(message, True)
        self.assertIsNone(self.b.claim_message())
        self.assertIsNotNone(self.a.claim_message())

    def test_cancelled_announcement_write_has_no_side_effect(self):
        def cancel():
            raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            self.a.announce(["Kitchen"], "Test", cancel)
        self.assertIsNone(self.b.claim_message())

    def test_muted_room_remains_muted_when_message_is_queued(self):
        self.b.heartbeat(muted=True, busy=False)
        self.a.announce(["Kitchen"], "Dinner time")
        row = next(x for x in self.a.directory() if x["id"] == "kitchen")
        self.assertTrue(row["muted"])

    def test_call_requires_answer_and_mute_ends_it(self):
        self.a.start_call("Kitchen")
        self.assertEqual(self.b.call()["status"], "ringing")
        self.b.answer_call()
        self.assertEqual(self.a.call()["status"], "active")
        self.b.heartbeat(muted=True, busy=False)
        self.assertIsNone(self.a.call())

    def test_unavailable_or_busy_room_cannot_be_called(self):
        for muted, busy in [(True, False), (False, True)]:
            self.b.heartbeat(muted=muted, busy=busy)
            self.assertIn("have not opened", self.a.start_call("Kitchen"))
            self.assertIsNone(self.b.call())

    def test_hangup_and_decline_end_both_sides(self):
        self.a.start_call("Kitchen")
        self.b.end_call()
        self.assertIsNone(self.a.call())
        self.a.start_call("Kitchen")
        self.b.answer_call()
        self.a.end_call()
        self.assertIsNone(self.b.call())

    def test_named_peer_can_be_answered_and_hung_up(self):
        self.a.start_call("Kitchen")
        self.b.handle("room_call", dict(action="answer", room="Office"))
        self.assertEqual(self.a.call()["status"], "active")
        self.a.handle("room_call", dict(action="end", room="Kitchen"))
        self.assertIsNone(self.b.call())
        with self.assertRaises(ValueError):
            self.a.handle("room_call", dict(action="end", room="Kitchen"))

    def test_dashboard_projection_does_not_expose_private_details(self):
        import json

        self.a.handle(
            "feature_request",
            dict(
                action="create",
                summary="Fridge statistics",
                details="Private device setup detail",
                requested_by="the owner",
            ),
        )
        self.a.export_public()
        value = json.loads((self.a.root / "public/status.json").read_text())
        self.assertNotIn("details", value["requests"][0])
        self.assertNotIn("requested_by", value["requests"][0])
        self.assertEqual(self.a.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.a.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.a.root / "public/status.json").stat().st_mode & 0o777, 0o644)

    def test_call_timeout(self):
        self.a.start_call("Kitchen")
        with self.a.tx() as db:
            db.execute("UPDATE calls SET expires=0")
        self.b.heartbeat(muted=False, busy=False)
        self.assertIsNone(self.a.call())

    def test_unknown_rooms_and_bad_arguments_fail_closed(self):
        for names in [["bedroom"], ["Kitchen", "garage"]]:
            with self.assertRaises(ValueError):
                self.a.announce(names, "Test")
        with self.assertRaises(ValueError):
            self.a.handle("room_call", {"action": "answer", "room": "Kitchen"})
        self.assertIsNone(self.b.claim_message())

    def test_features_are_persistent_requests_and_never_device_commands(self):
        reply = self.a.handle(
            "feature_request",
            dict(
                action="create",
                summary="Fridge ice controls",
                details="Add ice and dashboard statistics",
                requested_by="the owner",
            ),
        )
        self.assertIn("VR-", reply)
        self.assertIn("nothing has been installed", reply)
        b = Household(self.root / "house", "kitchen", ROOMS)
        self.assertIn(
            "Fridge ice controls",
            b.handle(
                "feature_request",
                dict(action="status", summary=None, details=None, requested_by=None),
            ),
        )
        self.assertIsNone(b.call())
        self.assertIsNone(b.claim_message())

    def test_natural_fast_paths_and_tool_schema(self):
        self.assertEqual(
            parse("Hey Vector, call the Kitchen")[1], dict(action="start", room="Kitchen")
        )
        self.assertEqual(
            parse("Tell Office that dinner is ready")[1],
            dict(rooms=["Office"], message="dinner is ready"),
        )
        self.assertEqual(parse("announce everywhere dinner time")[0], "room_announcement")
        self.assertIsNone(parse("don't call Kitchen"))
        self.assertEqual(
            {t["name"] for t in tools()}, {"room_announcement", "room_call", "feature_request"}
        )

    def test_audio_is_private_ephemeral_and_replay_checked(self):
        a, b = CallAudio(self.a), CallAudio(self.b)
        try:
            self.a.start_call("Kitchen")
            self.b.answer_call()
            call = self.a.call()
            a.set_call(call)
            b.set_call(call)
            pcm = struct.pack("<h", 700) * 1600
            a.capture(dict(rate=16000, width=2, channels=1), pcm)
            packet, address = b.sock.recvfrom(16384)
            self.assertEqual(b.decode(packet, address), pcm)
            self.assertIsNone(b.decode(packet, address))
            self.assertIsNone(b.decode(packet, "/untrusted.sock"))
            bad = HEADER.pack(bytes(16), 999, time.monotonic()) + pcm
            self.assertIsNone(b.decode(bad, address))
            stale = HEADER.pack(bytes.fromhex(call["id"]), 999, time.monotonic() - 2) + pcm
            self.assertIsNone(b.decode(stale, address))
        finally:
            a.close()
            b.close()
        self.assertFalse(list(self.a.root.glob("*.sock")))


class RuntimeMuteTest(unittest.IsolatedAsyncioTestCase):
    async def test_muted_room_never_plays_announcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Household(Path(tmp), "office", ROOMS)
            b = Household(Path(tmp), "kitchen", ROOMS)
            a.heartbeat(muted=False, busy=False)
            b.heartbeat(muted=True, busy=False)
            a.announce(["Kitchen"], "Dinner time")
            stop = asyncio.Event()
            s = SimpleNamespace(
                epoch=0,
                busy=False,
                speaking=False,
                recording=False,
                connected=True,
                stop=stop,
                allowed=lambda e: False,
                allowed_base=lambda e: False,
                followup=SimpleNamespace(active=lambda: False, ready=False),
            )
            runtime = RoomRuntime(b, s, None)
            task = asyncio.create_task(runtime.run())
            await asyncio.sleep(0.05)
            stop.set()
            await task
            # Message remains pending and no audio socket survives shutdown.
            self.assertIsNotNone(b.claim_message())
            self.assertFalse(list(Path(tmp).glob("*.sock")))


if __name__ == "__main__":
    unittest.main()
