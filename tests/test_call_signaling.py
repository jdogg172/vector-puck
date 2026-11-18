"""Local call controls are independent of the caller's conversation lease."""

import asyncio
import json
from unittest.mock import Mock, patch

from vector_puck.call_signaling import call_control
from vector_puck.household_runtime import RoomRuntime, call_ring_audio
from vector_puck.resident import Satellite
from vector_puck.spoken_stop import interrupt_loop
from test_incoming_call import IncomingCallTest


class CallControlTest(IncomingCallTest):
    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_answer_ignores_caller_lease_and_survives_native_finish(self):
        self.caller.nominate(0.5, 0.01)
        self.s.household = self.callee
        self.s.room_runtime = self.r
        self.s.barge_capable = True
        original = self.s.send

        async def send(kind, data=None, payload=b""):
            await original(kind, data, payload)
            if kind == "vector-stop":
                self.r.cancel()  # Real native cancellation semantics.

        self.s.send = send
        with patch("vector_puck.household_runtime.status"):
            task = asyncio.create_task(self.r.ring_call(self.callee.call()))
            try:
                await self.until(lambda: any(k == "audio-chunk" for k, p in self.events))
                self.assertFalse(self.callee.owns_turn())
                self.assertIsNone(self.r.audio.call)
                self.assertTrue(self.r.control_call("answer", self.s.epoch))
                await task
                self.assertEqual(self.caller.call()["status"], "active")
                self.assertNotIn("vector-stop", [k for k, p in self.events])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_caller_hears_ringback_and_cannot_answer_own_call(self):
        self.r.audio.close()
        self.r = RoomRuntime(self.caller, self.s, None)
        with patch("vector_puck.household_runtime.status"):
            task = asyncio.create_task(self.r.ring_call(self.caller.call()))
            try:
                await self.until(lambda: any(k == "audio-chunk" for k, p in self.events))
                self.assertTrue(any(any(p) for k, p in self.events if k == "audio-chunk"))
                self.assertFalse(self.r.control_call("answer", self.s.epoch))
                self.callee.answer_call()
                await task
                self.assertEqual(self.caller.call()["status"], "active")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_muted_callee_cannot_accept(self):
        self.r.signaling = self.callee.call()
        self.s.gate.muted = True
        self.assertFalse(self.r.control_call("answer", self.s.epoch))
        self.assertEqual(self.callee.call()["status"], "ringing")

    async def test_real_event_pump_drains_ring_without_cancelling_acceptance(self):
        self.s.reader = asyncio.StreamReader()
        self.s.room_runtime = self.r
        self.s.household = self.callee
        self.s.barge_capable = True

        def emit(kind, data=None):
            self.s.reader.feed_data(
                (json.dumps({"type": kind, "data": data or {}}) + "\n").encode()
            )

        async def send(kind, data=None, payload=b""):
            self.events.append((kind, payload))
            await Satellite.send(self.s, kind, data, payload)
            if kind == "audio-start":
                emit("run-pipeline", {"vector_barge_in": True})
            if kind == "vector-stop":
                emit("vector-cancel")
            if kind in {"audio-stop", "vector-stop"}:
                emit("played")
                emit("run-pipeline", {"start_stage": "wake"})

        self.s.send = send
        with patch("vector_puck.household_runtime.status"):
            pump = asyncio.create_task(self.s.pump())
            task = asyncio.create_task(self.r.ring_call(self.callee.call()))
            try:
                await self.until(lambda: self.s.barge_stream)
                self.assertTrue(self.r.control_call("answer", self.s.epoch))
                self.assertTrue(await asyncio.wait_for(task, 2))
                self.assertTrue(self.r.signaling_ready.is_set())
                self.assertEqual(self.callee.call()["status"], "active")
                self.assertNotIn("vector-stop", [k for k, p in self.events])
            finally:
                task.cancel()
                pump.cancel()
                await asyncio.gather(task, pump, return_exceptions=True)

    async def test_local_asr_answer_works_during_ring_without_a_global_turn(self):
        class Detector:
            def Reset(self):
                pass

            def AcceptWaveform(self, payload):
                return False

            def PartialResult(self):
                return '{"partial":"hey vector answer call"}'

        speech = Mock()
        speech.interrupt_detector.return_value = Detector()
        speech.transcribe.return_value = "Hey Vector answer call"
        self.caller.nominate(0.5, 0.01)
        self.r.signaling = self.callee.call()
        self.s.room_runtime = self.r
        self.s.activity = "call_wait"
        self.s.barge_stream = True
        with patch("vector_puck.household_runtime.status"):
            task = asyncio.create_task(interrupt_loop(self.s, speech))
            try:
                for _ in range(3):
                    self.s.interrupt_audio.put_nowait(
                        (
                            self.s.epoch,
                            self.s.interrupt_serial,
                            dict(rate=16000, width=2, channels=1),
                            bytes(3200),
                        )
                    )
                await self.until(lambda: self.callee.call()["status"] == "active")
                self.assertEqual(self.s.interrupt_checks["confirmed"], 1)
                self.assertNotIn("vector-stop", [k for k, p in self.events])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_narrow_addressed_call_controls(self):
        for text in [
            "Hey Vector answer call",
            "Vector, answer the call",
            "Hey Vector could you please pick up?",
            "hey victor accept this call",
        ]:
            self.assertEqual(call_control(text), "answer", text)
        for text in [
            "answer call",
            "yes",
            "Hey Vector do not answer the call",
            "Hey Vector answer the question",
            "Hey Vector turn the TV on",
        ]:
            self.assertIsNone(call_control(text), text)
        self.assertEqual(call_control("Hey Vector reject the call"), "end")

    def test_ringback_differs_from_incoming_ring(self):
        self.assertNotEqual(call_ring_audio(False), call_ring_audio(True))
