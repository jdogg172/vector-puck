"""Synthetic local audio, no device or API: stop must invalidate every old result."""
from array import array
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from vector_puck.config import Config
from vector_puck.resident import Satellite, respond
from vector_puck.spoken_stop import (
    audio_level, interrupt_requested, interruptions, stop_phrases, wake_audio,
)
from vector_puck.transport import MIC_FORMAT, send_event


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False
    def write(self, data):
        self.data.extend(data)
    async def drain(self):
        await asyncio.sleep(0)
    def close(self):
        self.closed = True


class Detector:
    def Reset(self):
        pass
    def AcceptWaveform(self, pcm):
        return False
    def PartialResult(self):
        return json.dumps({"partial": ""})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class StopTests(unittest.IsolatedAsyncioTestCase):
    def satellite(self, enabled=True):
        dialogue = SimpleNamespace(clear=lambda: None, expire=lambda: None)
        sat = Satellite(asyncio.StreamReader(), Writer(),
                        replace(Config(), experimental_spoken_stop=enabled), dialogue, asyncio.Event())
        sat.gate.muted = False
        sat.barge_stream = True
        sat.interrupt_serial = 1
        return sat

    async def feed(self, sat):
        pcm = array("h", [40] * 1600).tobytes()
        for _ in range(16):
            sat.interrupt_audio.put_nowait((sat.gate.epoch, sat.interrupt_serial, MIC_FORMAT, pcm))

    def test_addressed_stop_and_custom_wake_only(self):
        for phrase in ["Hey Vector, stop", "vector please stop", "Victor be quiet",
                       "hey vector could you please cancel"]:
            self.assertTrue(interrupt_requested(phrase, "hey vector"), phrase)
        for phrase in ["stop", "pause the TV", "there is a stop sign", "hey vector stopwatch"]:
            self.assertFalse(interrupt_requested(phrase, "hey vector"), phrase)
        self.assertTrue(interrupt_requested("hey computer stop", "hey computer"))
        self.assertFalse(interrupt_requested("hey vector stop", "hey computer"))
        self.assertIn("computer stop", stop_phrases("hey computer"))

    def test_gain_is_bounded_and_leaves_silence_unchanged(self):
        silence = bytes(3200)
        self.assertEqual(wake_audio(silence), silence)
        quiet = array("h", [100] * 1600).tobytes()
        boosted = array("h", wake_audio(quiet))
        self.assertLessEqual(max(boosted), 600)
        self.assertGreater(audio_level(quiet), 0)
        with self.assertRaises(ValueError):
            wake_audio(b"x")

    async def test_whisper_confirms_stop_even_when_vosk_misses(self):
        sat = self.satellite()
        sat.followup.begin()
        sat.active = asyncio.create_task(asyncio.sleep(60))
        speech = SimpleNamespace(interrupt_detector=Detector,
                                 transcribe=lambda pcm: "Hey Vector stop")
        task = asyncio.create_task(interruptions(sat, speech))
        try:
            await self.feed(sat)
            await until(lambda: sat.interrupt_checks["confirmed"] == 1)
            await asyncio.sleep(0)
            self.assertTrue(sat.active.cancelled())
            self.assertIn(b'"vector-stop"', sat.writer.data)
            self.assertFalse(sat.gate.allowed(0))
            self.assertEqual(sat.followup.remaining, 0)
            self.assertEqual(sat.events.qsize(), 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_bare_stop_or_unrelated_audio_does_not_cancel(self):
        for heard in ["stop", "television background speech", "hey vector continue"]:
            sat = self.satellite()
            speech = SimpleNamespace(interrupt_detector=Detector, transcribe=lambda pcm: heard)
            task = asyncio.create_task(interruptions(sat, speech))
            try:
                await self.feed(sat)
                await until(lambda: sat.interrupt_checks["local_checks"] == 1)
                await asyncio.sleep(0.02)
                self.assertNotIn(b'"vector-stop"', sat.writer.data)
                self.assertEqual(sat.interrupt_checks["confirmed"], 0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_delayed_asr_cannot_stop_muted_or_newer_reply(self):
        for change in ("mute", "new_reply"):
            sat = self.satellite()
            started, release, returned = threading.Event(), threading.Event(), threading.Event()
            def transcribe(pcm):
                started.set()
                release.wait(2)
                returned.set()
                return "hey vector stop"
            speech = SimpleNamespace(interrupt_detector=Detector, transcribe=transcribe)
            task = asyncio.create_task(interruptions(sat, speech))
            try:
                await self.feed(sat)
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                if change == "mute":
                    sat.gate.muted = True
                    sat.invalidate()
                else:
                    sat.interrupt_serial += 1
                release.set()
                self.assertTrue(await asyncio.to_thread(returned.wait, 1))
                await asyncio.sleep(0.02)
                self.assertNotIn(b'"vector-stop"', sat.writer.data)
                self.assertEqual(sat.interrupt_checks["confirmed"], 0)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_stop_worker_failure_disconnects_instead_of_silent_loss(self):
        sat = self.satellite()
        def failure():
            raise ValueError("synthetic unavailable model")
        await interruptions(sat, SimpleNamespace(interrupt_detector=failure))
        self.assertFalse(sat.gate.connected)
        self.assertFalse(sat.interrupt_worker_running)
        self.assertTrue(sat.writer.closed)

    async def test_barge_frames_are_bounded_and_never_become_conversation(self):
        for enabled in (True, False):
            sat = self.satellite(enabled)
            writer = Writer()
            await send_event(writer, "run-pipeline", {"vector_barge_in": True})
            for _ in range(40):
                await send_event(writer, "audio-chunk", MIC_FORMAT, bytes(3200))
            sat.reader.feed_data(writer.data)
            pump = asyncio.create_task(sat.pump())
            try:
                await asyncio.sleep(0.04)
                self.assertTrue(sat.events.empty())
                self.assertEqual(sat.interrupt_audio.qsize(), 30 if enabled else 0)
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

    async def test_reply_prearms_stop_and_health_contains_no_transcript(self):
        sat = self.satellite()
        sat.dialogue.respond = AsyncMock(return_value=("Synthetic reply.", bytes(4800)))
        sat.barge_stream = False
        await respond(sat, "Test request", 0)
        self.assertLess(sat.writer.data.index(b'vector-listen'), sat.writer.data.index(b'audio-start'))
        self.assertNotIn(b'vector-stop', sat.writer.data)
        with tempfile.TemporaryDirectory() as temp:
            sat.interrupt_worker_running = True
            task = asyncio.create_task(sat.monitor(Path(temp)))
            try:
                await until(lambda: (Path(temp) / "health.json").exists())
                health = json.loads((Path(temp) / "health.json").read_text())
                self.assertTrue(health["spoken_stop"])
                self.assertTrue(health["interrupt_worker_running"])
                self.assertNotIn("Synthetic reply", json.dumps(health))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
