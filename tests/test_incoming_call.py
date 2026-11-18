"""Incoming call notification tests, with synthetic writers and no hardware."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from vector_puck.household import Household
from vector_puck.household_runtime import RoomRuntime, call_ring_audio
from vector_puck.resident import Satellite
from test_household import ROOMS
from vector_puck.config import Config
from types import SimpleNamespace


class IncomingCallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.caller = Household(root / "house", "kitchen", ROOMS)
        self.callee = Household(root / "house", "office", ROOMS)
        self.caller.heartbeat(muted=False, busy=False)
        self.callee.heartbeat(muted=False, busy=False)
        self.caller.start_call("Office")
        writer = Mock()
        writer.drain = AsyncMock()
        self.s = Satellite(
            None, writer, Config(), SimpleNamespace(clear=lambda: None), asyncio.Event()
        )
        self.s.gate.muted = False
        self.r = RoomRuntime(self.callee, self.s, None)
        self.events = []
        original = self.s.send

        async def send(kind, data=None, payload=b""):
            self.events.append((kind, payload))
            await original(kind, data, payload)
            if kind == "audio-stop":
                self.s.speaking = False
                self.s.alert_played.set()
                self.r.signaling_ready.set()

        self.s.send = send

    async def asyncTearDown(self):
        self.r.audio.close()
        self.tmp.cleanup()

    async def test_local_ring_keeps_the_answer_stream_open_without_turn_ownership(self):
        self.s.activity = "request"  # An expired request lease cannot block a notification.
        self.s.barge_capable = True
        with patch("vector_puck.household_runtime.status"):
            task = asyncio.create_task(self.r.ring_call(self.callee.call()))
            try:
                await asyncio.sleep(1.2)
                self.assertFalse(task.done())
                self.assertNotIn("audio-stop", [kind for kind, pcm in self.events])
                self.callee.end_call()
                self.assertTrue(await task)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any(kind == "audio-chunk" and any(pcm) for kind, pcm in self.events))
        self.assertIsNone(self.r.audio.call, "Ringing opened a microphone bridge before acceptance")

    async def test_muted_or_busy_recipient_does_not_play_ring(self):
        for attr in ["muted", "busy", "recording"]:
            target = self.s.gate if attr == "muted" else self.s
            setattr(target, attr, True)
            self.assertFalse(await self.r.ring_call(self.callee.call()))
            setattr(target, attr, False)
        self.assertEqual(self.events, [])

    async def test_answer_or_mute_cuts_queued_ring(self):
        original = self.s.send

        async def answer(kind, data=None, payload=b""):
            await original(kind, data, payload)
            if kind == "audio-chunk":
                self.callee.answer_call()

        self.s.send = answer
        with patch("vector_puck.household_runtime.status"):
            await self.r.ring_call(self.callee.call())
        self.assertEqual(sum(k == "audio-chunk" for k, p in self.events), 1)
        self.assertEqual(self.callee.call()["status"], "active")
        self.assertNotIn(
            "vector-stop",
            [k for k, p in self.events],
            "Native vector-stop emits cancel and would hang up an accepted call",
        )

    def test_ring_is_audible_bounded_and_uses_supported_format(self):
        rate, pcm = call_ring_audio()
        self.assertEqual(rate, 24000)
        self.assertTrue(any(pcm))
        self.assertLess(len(pcm), rate * 2 * 2)


if __name__ == "__main__":
    unittest.main()
