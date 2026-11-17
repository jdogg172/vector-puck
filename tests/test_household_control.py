"""Portable household wiring with disposable state, synthetic API proposals and PCM."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from vector_puck.assistant import answer, request_body
from vector_puck.cloud_voice import Dialogue
from vector_puck.config import Config
from vector_puck.household_control import HouseholdControl, read_config
from vector_puck.household_runtime import RoomRuntime
from vector_puck.resident import Satellite, session
from vector_puck.transport import MIC_FORMAT, read_event, send_event
from vector_puck.state import Cancelled
from test_core import Writer, load_script
from test_household import ROOMS
from test_tool_routing import Client


class HouseholdControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vector-hub-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = dict(
            root=str(self.root / "house"), room="office", rooms=ROOMS, timezone="Europe/London"
        )
        self.hub = HouseholdControl(self.data, self.root / "state")
        self.hub.alarm_ready = True

    async def test_relative_alarm_model_proposal_and_snooze_survive_restart(self):
        args = dict(action="set", name="test", at=None, days=[], date=None, minutes=2)
        client = Client(
            [dict(type="function_call", name="alarm_clock", arguments=json.dumps(args))]
        )
        result = await answer(
            Config(),
            "synthetic",
            "set a test alarm in two minutes",
            [],
            lambda: None,
            client,
            household=self.hub,
        )
        self.assertIn("in 2 minutes", result)
        again = HouseholdControl(self.data, self.root / "state")
        rows = again.alarms.store.path
        with again.alarms.store.transaction() as db:
            alarm = dict(db.execute("select * from alarms").fetchone())
        self.assertEqual(rows, self.root / "state/alarms.sqlite3")
        self.assertAlmostEqual(alarm["due"] - alarm["updated"], 120)
        self.assertEqual(alarm["status"], "scheduled")

    async def test_private_room_context_has_no_shopping_contents_or_paths(self):
        self.hub.kitchen.shopping(
            dict(action="add", items=["synthetic milk"], offset=0), lambda: None
        )
        body = request_body(Config(), "hello", [], household=self.hub)
        text = json.dumps(body)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("synthetic milk", text)
        self.assertIn("registered_rooms", text)
        self.assertIn("+00:00", self.hub.context()["local_time"])
        self.assertTrue(all(tool["strict"] for tool in body["tools"]))

    async def test_negated_hypothetical_and_unknown_mutations_do_not_write(self):
        args = dict(action="add", items=["milk"], offset=0)
        for text in [
            "don't add milk",
            "explain how to add milk",
            "if I add milk",
            "pretend to add milk",
        ]:
            result = await self.hub.execute("shopping_list", args, lambda: None, utterance=text)
            self.assertIn("Nothing was changed", result["message"])
        self.assertEqual(self.hub.kitchen.items(), [])
        await self.hub.execute("shell", {}, lambda: None, utterance="start a shell")
        self.assertEqual(self.hub.kitchen.items(), [])

    async def test_cancel_guard_prevents_commit(self):
        def guard():
            raise Cancelled("synthetic cancel")

        with self.assertRaises(Cancelled):
            await self.hub.execute(
                "shopping_list",
                dict(action="add", items=["milk"], offset=0),
                guard,
                utterance="add milk",
            )
        self.assertEqual(self.hub.kitchen.items(), [])

    async def test_direct_timer_uses_no_reasoning_request(self):
        config = Config()
        voice = Dialogue(config, "synthetic", self.root / "state", household=self.hub)
        with (
            patch("vector_puck.cloud_voice.answer", new_callable=AsyncMock) as reasoning,
            patch(
                "vector_puck.cloud_voice.cedar", new_callable=AsyncMock, return_value=bytes(4800)
            ),
        ):
            result, _ = await voice.respond("set a pasta timer for two minutes", lambda: None)
        reasoning.assert_not_awaited()
        self.assertIn("pasta", result)
        self.assertEqual(len(self.hub.kitchen.tasks()), 1)

    async def test_optional_call_prompts_cache_static_speech_without_device_access(self):
        dialogue = Dialogue(Config(), "synthetic", self.root / "state", household=self.hub)
        dialogue.quota.reserve = lambda: None
        with (
            patch(
                "vector_puck.cloud_voice.cedar", new_callable=AsyncMock, return_value=bytes(4800)
            ) as voice,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await self.hub.cache_call_prompts(dialogue)
        self.assertEqual(voice.await_count, 2)
        files = list((self.hub.household.root / "call-prompts").glob("*.pcm"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))
        self.assertIsNone(self.hub.household.call())

    async def test_unpatched_satellite_cannot_accept_alarm(self):
        self.hub.alarm_ready = False
        result = await self.hub.execute(
            "alarm_clock",
            dict(action="set", name="test", at=None, days=[], date=None, minutes=2),
            lambda: None,
            utterance="set a test alarm in two minutes",
        )
        self.assertIn("needs the native alarm patch", result["message"])
        self.assertEqual(self.hub.alarms.ringing(), [])

    async def test_natural_weekday_alarm_and_timer_followup_are_allowed(self):
        result = await self.hub.execute(
            "alarm_clock",
            dict(
                action="set",
                name="school",
                at="07:00",
                days=[0, 1, 2, 3, 4],
                date=None,
                minutes=None,
            ),
            lambda: None,
            utterance="wake me at seven AM on weekdays",
        )
        self.assertIn("Set the school alarm", result["message"])
        await self.hub.execute(
            "named_timer",
            dict(action="start", name="pasta", seconds=600),
            lambda: None,
            utterance="set a pasta timer for ten minutes",
        )
        result = await self.hub.execute(
            "named_timer",
            dict(action="extend", name="pasta", seconds=120),
            lambda: None,
            utterance="two more minutes",
        )
        self.assertIn("12 minutes", result["message"])

    async def test_zone_is_owner_selected_for_alarm_and_reminder(self):
        self.assertEqual(str(self.hub.alarms.zone), "Europe/London")
        self.assertEqual(str(self.hub.kitchen.zone), "Europe/London")
        stamp = datetime.fromisoformat("2027-06-01T12:00:00+01:00")
        from vector_puck.kitchen import reminder_time

        self.assertEqual(
            reminder_time(stamp.isoformat(), stamp.timestamp() - 60, self.hub.zone),
            stamp.timestamp(),
        )

    async def test_announcement_waits_for_played_and_mute_invalidates(self):
        config = replace(Config(), working_sound=False, experimental_spoken_stop=False)
        dialogue = Dialogue(config, "synthetic", self.root / "state", household=self.hub)
        s = Satellite(None, Writer(), config, dialogue, asyncio.Event())
        s.gate.muted = False
        runtime = RoomRuntime(self.hub.household, s, dialogue, alarms=self.hub.alarms)
        self.addCleanup(runtime.audio.close)
        original = s.send

        async def send(kind, data=None, payload=b""):
            await original(kind, data, payload)
            if kind == "audio-stop":
                s.speaking = False
                s.alert_played.set()

        s.send = send
        with patch(
            "vector_puck.cloud_voice.cedar", new_callable=AsyncMock, return_value=bytes(4800)
        ):
            self.assertTrue(await runtime.say("Synthetic announcement."))
        s.gate.muted = True
        before = bytes(s.writer.data)
        self.assertFalse(await runtime.say("Must stay silent."))
        self.assertEqual(before, bytes(s.writer.data))

    async def test_mute_and_action_end_call_and_invalidate_work(self):
        other = HouseholdControl({**self.data, "room": "kitchen"}, self.root / "other")
        self.hub.household.heartbeat(muted=False, busy=False)
        other.household.heartbeat(muted=False, busy=False)
        self.hub.household.start_call("Kitchen")
        other.household.answer_call()
        d = Dialogue(Config(), "synthetic", self.root / "state", household=self.hub)
        s = Satellite(None, Writer(), Config(), d, asyncio.Event())
        runtime = RoomRuntime(self.hub.household, s, d)
        s.room_runtime = runtime
        self.addCleanup(runtime.audio.close)
        runtime.active = self.hub.household.call()
        runtime.audio.set_call(runtime.active)
        s.invalidate()
        self.assertIsNone(runtime.audio.call)
        self.assertIsNone(other.household.call())


class MultiRoomSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_real_sessions_elect_one_and_commit_shared_list_once(self):
        class Speech:
            def __init__(self):
                self.woke = False

            def reset(self):
                pass

            def wake_candidate(self, pcm):
                if self.woke:
                    return False
                self.woke = True
                return True

            def transcribe(self, pcm):
                return "hey vector add milk to our shopping list"

            def endpoint_ready(self, pcm, seconds):
                return seconds >= 1.2

        stop, complete = asyncio.Event(), asyncio.Event()
        detections, replies, errors = [], [], []

        async def peer(reader, writer):
            try:
                while True:
                    kind, data, _ = await read_event(reader)
                    if kind == "describe":
                        await send_event(
                            writer,
                            "info",
                            {
                                "satellite": {
                                    "vector_controls": 2,
                                    "vector_muted": False,
                                    "vector_alarm": True,
                                    "vector_indicator": True,
                                }
                            },
                        )
                    elif kind == "run-satellite":
                        await send_event(writer, "run-pipeline", {"start_stage": "wake"})
                        await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(3200))
                    elif kind == "detection":
                        detections.append(1)
                        await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(19200))
                        await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(19200))
                    elif kind == "audio-stop":
                        replies.append(1)
                        await send_event(writer, "played")
                        complete.set()
                    elif kind == "pause-satellite":
                        return
            except (EOFError, ConnectionError):
                pass
            except Exception as error:
                errors.append(type(error).__name__)
                complete.set()
            finally:
                writer.close()
                await writer.wait_closed()

        with tempfile.TemporaryDirectory(prefix="vector-sessions-") as temp:
            root = Path(temp)
            server = await asyncio.start_server(peer, "127.0.0.1", 0)
            config = replace(
                Config(),
                puck_host="127.0.0.1",
                puck_port=server.sockets[0].getsockname()[1],
                working_sound=False,
                experimental_spoken_stop=False,
                followup_seconds=0,
            )
            tasks, hubs = [], []
            try:
                with patch(
                    "vector_puck.cloud_voice.cedar",
                    new_callable=AsyncMock,
                    return_value=bytes(4800),
                ):
                    for room in ("office", "kitchen"):
                        state = root / room
                        hub = HouseholdControl(
                            dict(root=str(root / "h"), room=room, rooms=ROOMS, timezone="UTC"),
                            state,
                        )
                        hubs.append(hub)
                        dialogue = Dialogue(config, "synthetic", state, household=hub)
                        tasks.append(
                            asyncio.create_task(session(config, Speech(), dialogue, stop, state))
                        )
                    await asyncio.wait_for(complete.wait(), 5)
                    await asyncio.sleep(0.2)
                    self.assertEqual(errors, [])
                    self.assertEqual(len(detections), 1)
                    self.assertEqual(len(replies), 1)
                    self.assertEqual(hubs[0].kitchen.items(), hubs[1].kitchen.items())
                    self.assertEqual(hubs[0].kitchen.items()[0]["text"], "milk")
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                server.close()
                await server.wait_closed()


class HouseholdSetupTests(unittest.TestCase):
    def test_setup_is_private_no_overwrite_and_owner_configurable(self):
        setup = load_script("setup_household")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup.prepare(
                root, ["office:192.0.2.10:biscuit", "kitchen:192.0.2.11:radar"], "Europe/London"
            )
            path = root / "config/kitchen.household.local.json"
            data = read_config(path)
            self.assertEqual(data["room"], "kitchen")
            self.assertEqual(data["timezone"], "Europe/London")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config = Config.load(root / "config/kitchen.local.json")
            self.assertEqual(config.hardware, "echo2-xc56py-radar")
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                setup.prepare(
                    root, ["office:192.0.2.10:biscuit", "kitchen:192.0.2.11:radar"], "UTC"
                )
            self.assertEqual(path.read_bytes(), before)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_config(path)

    def test_duplicate_devices_and_invalid_models_create_no_files(self):
        setup = load_script("setup_household")
        for rooms in [
            ["a:192.0.2.10:biscuit", "b:192.0.2.10:radar"],
            ["a:192.0.2.10:biscuit", "b:192.0.2.11:unknown"],
        ]:
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    setup.prepare(Path(temp), rooms, "UTC")
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_check_config_rejects_ambiguous_directory_before_any_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "household.json"
            data = dict(
                root=str(root / "h"),
                room="office",
                timezone="UTC",
                rooms=[*ROOMS, {"id": "third", "name": "Office", "aliases": []}],
            )
            path.write_text(json.dumps(data))
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                read_config(path)
            self.assertFalse((root / "h").exists())
