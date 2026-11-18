"""Local peer events, synthetic transcripts and PCM. No microphone or network."""
from array import array
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from vector_puck.config import Config
from vector_puck.resident import Satellite, listen, session
from vector_puck.spoken_stop import interruptions
from vector_puck.transport import MIC_FORMAT


def frame(marker):
    return array("h", [marker] * 1600).tobytes()


END = frame(701)


class Wire:
    def __init__(self):
        self.sent = []
        self.times = []

    def write(self, packet):
        header, _, pcm = packet.partition(b"\n")
        self.sent.append((json.loads(header)["type"], pcm))
        self.times.append(time.monotonic())

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        pass

    def count(self, kind):
        return sum(event == kind for event, _ in self.sent)


class Detector:
    candidate = "hey vector"
    ended = False

    def Reset(self):
        pass

    def AcceptWaveform(self, pcm):
        return self.ended

    def Result(self):
        return json.dumps({"text": self.candidate})

    def PartialResult(self):
        return json.dumps({"partial": self.candidate})


class LocalSpeech:
    def __init__(self):
        self.detector = Detector()
        self.heard = "Hey Vector set a timer"
        self.calls = []
        self.started = threading.Event()
        self.resume = threading.Event()
        self.resume.set()

    def reset(self):
        pass

    def wake_candidate(self, pcm):
        return True

    def endpoint_ready(self, pcm, seconds):
        return pcm.endswith(END) or seconds >= 30

    def interrupt_detector(self):
        return self.detector

    def transcribe(self, pcm):
        self.started.set()
        self.resume.wait(2)
        self.calls.append(pcm)
        return self.heard


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.003)


class FocusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.speech = LocalSpeech()
        self.wire = Wire()
        self.requests = []
        self.old_reply = "The timer is set. Here is a long explanation."
        self.old_audio = frame(803) * 80
        self.dialogue = SimpleNamespace(clear=Mock(), played=Mock(),
                                        essentials=SimpleNamespace(fast_path=lambda text: None))
        self.dialogue.respond = self.respond
        self.s = Satellite(asyncio.StreamReader(), self.wire,
                           replace(Config(), working_sound=False), self.dialogue, asyncio.Event())
        self.s.gate.muted = False
        self.s.focus_capable = self.s.question_capable = True
        self.tasks = [asyncio.create_task(run) for run in (
            self.s.pump(), listen(self.s, self.speech), interruptions(self.s, self.speech))]

    async def respond(self, query, guard):
        guard()
        self.requests.append(query)
        if len(self.requests) == 1:
            return self.old_reply, self.old_audio
        return "A new answer.", frame(809)

    async def asyncTearDown(self):
        self.s.stop.set()
        self.speech.resume.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def emit(self, kind, data=None, pcm=b""):
        header = {"type": kind, "data": data or {}, "payload_length": len(pcm)}
        self.s.reader.feed_data(json.dumps(header).encode() + b"\n" + pcm)

    def mic(self, pcm):
        self.emit("audio-chunk", MIC_FORMAT, pcm)

    async def original_turn(self, query="set a timer"):
        self.speech.heard = f"Hey Vector {query}"
        self.emit("run-pipeline", {"start_stage": "wake"})
        self.mic(frame(51))
        await until(lambda: self.wire.count("detection") == 1)
        self.mic(END)
        await until(lambda: self.wire.count("audio-chunk") > 0)
        self.emit("run-pipeline", {"start_stage": "wake", "vector_barge_in": True})
        await until(lambda: self.s.barge_stream)
        self.assertEqual(self.requests, [query])
        return self.s.active

    async def hear(self, text):
        self.speech.heard = text
        before = len(self.speech.calls)
        if self.s.focus_until:
            await asyncio.sleep(0.26)
        self.mic(frame(83))
        self.mic(frame(89))
        await until(lambda: len(self.speech.calls) > before)
        # The ASR result returns from a worker thread to the event loop.
        await asyncio.sleep(0.015)

    async def test_wake_and_duplicate_only_light_ring_while_old_audio_continues(self):
        old = await self.original_turn()
        await self.hear("Hey, Vector!")
        self.assertEqual(self.wire.count("vector-focus"), 1)
        deadline = self.s.focus_until
        self.assertGreater(deadline, time.monotonic())
        self.assertLessEqual(deadline, time.monotonic() + 8)
        await self.hear("Hey Vector. Hey Vector.")
        await self.hear("Hey Vector, can you please")
        count = self.wire.count("audio-chunk")
        await until(lambda: self.wire.count("audio-chunk") > count)
        self.assertEqual(self.s.focus_until, deadline)
        self.assertEqual(self.wire.count("vector-focus"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.wire.count("audio-stop"), 0)
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.assertFalse(old.done())
        self.assertEqual(self.s.epoch, 0)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_verified_stop_after_focus_uses_existing_stop(self):
        old = await self.original_turn()
        await self.hear("Hey Vector")
        await self.hear("Hey Vector, please stop.")
        await until(old.cancelled)
        self.assertEqual(self.wire.count("vector-stop"), 1)
        self.assertEqual(self.s.focus_until, 0)
        self.assertIsNone(self.s.pending_history)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_question_after_focus_switches_once_preserving_continuation_and_owner(self):
        old = await self.original_turn()
        house = SimpleNamespace(owns_turn=Mock(return_value=True), call=lambda: None)
        self.s.hub = SimpleNamespace(household=house, fast_path=lambda q: None)
        self.s.activity = "request"
        await self.hear("Hey Vector")
        self.speech.heard = "What time is it"
        self.speech.started.clear()
        self.speech.resume.clear()
        await asyncio.sleep(0.26)
        self.mic(frame(91))
        self.mic(frame(92))
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
        # Continuation arrives during local recognition, before native detection.
        elapsed = self.s.interrupt_checks["audio_seconds"]
        self.mic(frame(97))
        await until(lambda: self.s.interrupt_checks["audio_seconds"] > elapsed)
        self.assertFalse(old.done())
        self.speech.resume.set()
        await until(lambda: self.wire.count("audio-stop") == 1)
        emitted = [kind for kind, _ in self.wire.sent]
        self.assertLess(emitted.index("vector-focus"), emitted.index("vector-question"))
        self.assertLess(emitted.index("vector-question"), emitted.index("audio-stop"))
        self.assertTrue(old.cancelled())
        self.assertEqual(self.s.activity, "request")
        self.assertEqual(self.wire.count("vector-question"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        self.emit("played")  # Delayed old acknowledgement during new capture.
        self.speech.heard = "What time is it in Tokyo?"
        self.mic(END)
        await until(lambda: self.wire.count("audio-stop") == 2)
        self.assertEqual(self.requests, ["set a timer", "What time is it in Tokyo?"])
        self.assertEqual(self.speech.calls[-1], frame(91) + frame(92) + frame(97) + END)
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.dialogue.played.assert_not_called()
        self.dialogue.clear.assert_not_called()
        self.emit("played")
        await until(lambda: self.dialogue.played.called)
        self.dialogue.played.assert_called_once_with("What time is it in Tokyo?", "A new answer.")
        self.assertTrue(house.owns_turn.called)

    async def test_same_breath_after_original_audio_stop_before_played(self):
        self.old_audio = frame(83)
        await self.original_turn()
        await until(lambda: self.s.pending_history is not None)
        await self.hear("Hey Victor, hey Vector, how far is Austin?")
        self.assertEqual(self.wire.count("vector-question"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.wire.count("audio-stop"), 1)  # Already closed.
        self.assertEqual(self.wire.count("vector-focus"), 0)
        self.assertIsNone(self.s.pending_history)
        self.assertIsNone(self.s.followup.pending)
        self.emit("played")
        self.mic(END)
        await until(lambda: self.wire.count("audio-stop") == 2)
        self.assertEqual(self.requests, ["set a timer", "how far is Austin?"])
        self.dialogue.played.assert_not_called()

    async def test_mute_discards_inflight_question_and_old_audio(self):
        old = await self.original_turn()
        await self.hear("Hey Vector")
        self.speech.heard = "When is dinner?"
        self.speech.started.clear()
        self.speech.resume.clear()
        await asyncio.sleep(0.26)
        self.mic(frame(90))
        self.mic(frame(91))
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
        self.emit("vector-mute", {"muted": True})
        await until(lambda: self.wire.count("pause-satellite") == 1)
        self.speech.resume.set()
        self.mic(END)
        await asyncio.sleep(0.04)
        self.assertTrue(old.cancelled())
        self.assertIsNone(self.s.focus_turn)
        self.assertFalse(self.s.recording)
        self.assertEqual(self.s.focus_until, 0)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_final_asr_cannot_replace_confirmed_question_with_an_old_control(self):
        await self.original_turn()
        await self.hear("Hey Vector, what time is it?")
        self.speech.heard = "Hey Vector set a timer"
        self.mic(END)
        await until(lambda: self.wire.count("audio-stop") == 2)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_cedar_echo_cannot_stop_focus_or_take_turn(self):
        self.old_reply = 'Say "Hey Vector stop." What time is it in Austin?'
        await self.original_turn()
        await self.hear("Hey, Victor, stop!")
        self.assertEqual(self.wire.count("vector-stop"), 0)
        # Supply fresh candidate audio for each independent local check.
        for text in ("Hey Victor!", "Hey Vector, what time is it in Austin?",
                     "Hey Vector, what time is it Austin?"):
            self.emit("run-pipeline", {"vector_barge_in": True})
            await self.hear(text)
        self.assertEqual(self.wire.count("vector-focus"), 0)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertTrue(self.s.speaking)

    async def test_expiry_stream_change_and_loss_of_room_lease(self):
        await self.original_turn()
        await self.hear("Hey Vector")
        self.s.focus_until = time.monotonic() - 1
        await self.hear("How far is Dallas?")
        self.emit("run-pipeline", {"vector_barge_in": True})
        await self.hear("Hey Vector")
        self.assertEqual(self.wire.count("vector-focus"), 2)
        self.emit("run-pipeline", {"vector_barge_in": True})
        await self.hear("How far is Dallas?")
        # Deliver a recognized question after losing this room's turn. The
        # transport may already discard new PCM when ownership changes.
        self.s.hub = SimpleNamespace(household=SimpleNamespace(owns_turn=lambda: False))
        self.s.activity = "request"
        accepted = await self.s.focus_input(self.s.gate.epoch, self.s.interrupt_serial,
                                            time.monotonic(), "Hey Vector, how far is Dallas?", frame(83))
        self.assertFalse(accepted)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.wire.count("vector-question"), 0)
        self.assertEqual(self.s.focus_until, 0)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_legacy_peer_never_promotes_barge_audio(self):
        self.s.focus_capable = False
        await self.original_turn()
        self.speech.heard = "Hey Vector, how far is Austin?"
        for _ in range(16):
            self.mic(frame(97))
        await until(lambda: self.s.interrupt_checks["local_checks"] == 1)
        await asyncio.sleep(0.02)
        self.assertTrue(self.s.speaking)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.wire.count("vector-focus"), 0)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_call_ambient_and_emergency_block_focus(self):
        await self.original_turn()
        self.s.room_runtime = SimpleNamespace(active=None, signaling=None,
                                              household=SimpleNamespace(call=lambda: None))
        self.s.ambient = SimpleNamespace(active=False)
        for field, blocked in (("call", True), ("ambient", True), ("activity", "emergency")):
            with self.subTest(field=field):
                await self.hear("Hey Vector")
                self.assertGreater(self.s.focus_until, time.monotonic())
                if field == "call":
                    self.s.room_runtime.signaling = blocked
                elif field == "ambient":
                    self.s.ambient.active = blocked
                else:
                    self.s.activity = blocked
                await self.hear("When is sunrise?")
                self.assertEqual(self.wire.count("detection"), 1)
                self.assertEqual(self.s.focus_until, 0)
                self.s.room_runtime.signaling = None
                self.s.ambient.active = False
                self.s.activity = "idle"
                self.emit("run-pipeline", {"vector_barge_in": True})
                await until(lambda: self.s.interrupt_checks["local_checks"] == 0)

    async def test_native_alarm_revokes_open_focus(self):
        await self.original_turn()
        await self.hear("Hey Vector")
        self.emit("vector-alarm-state", {"ringing": True})
        await until(lambda: self.s.alarm_ringing)
        self.assertEqual(self.s.focus_until, 0)
        self.emit("run-pipeline", {"vector_barge_in": True})
        await self.hear("Hey Vector, when is dinner?")
        self.assertEqual(self.wire.count("detection"), 1)

    async def test_mute_during_final_asr_discards_the_captured_turn(self):
        await self.original_turn()
        await self.hear("Hey Vector when is dinner?")
        self.assertEqual(self.wire.count("vector-question"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        self.speech.started.clear()
        self.speech.resume.clear()
        self.mic(END)
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
        before = self.wire.count("audio-start")
        self.emit("vector-mute", {"muted": True})
        await until(lambda: self.s.hardware_muted)
        self.speech.resume.set()
        await until(lambda: not self.s.busy)
        self.assertEqual(self.requests, ["set a timer"])
        self.assertIsNone(self.s.focus_turn)
        self.assertFalse(self.s.recording)
        self.assertEqual(self.wire.count("audio-start"), before)

    async def test_stream_replacement_during_local_confirmation(self):
        await self.original_turn()
        self.speech.heard = "Hey Vector, when is breakfast?"
        self.speech.started.clear()
        self.speech.resume.clear()
        self.mic(frame(70))
        self.mic(frame(71))
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
        serial = self.s.interrupt_serial
        self.emit("run-pipeline", {"vector_barge_in": True})
        await until(lambda: self.s.interrupt_serial != serial)
        self.speech.resume.set()
        await asyncio.sleep(0.04)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_old_request_cannot_be_replayed_as_a_question(self):
        await self.original_turn()
        await self.hear("Hey Vector, set a timer?")
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_capability_requires_wire_boolean_true(self):
        captured = []

        async def capture(satellite, speech):
            captured.append((satellite.focus_capable, satellite.question_capable,
                             satellite.interrupt_audio.maxsize))

        for advertised, question in ((True, True), (True, None), (True, "true"),
                                     (True, 1), (False, True), ("true", True), (None, None)):
            reader, wire = asyncio.StreamReader(), Wire()
            wire.wait_closed = AsyncMock()
            details = {"vector_controls": 2, "vector_muted": True, "vector_barge_in": True}
            if advertised is not None:
                details["vector_focus"] = advertised
            if question is not None:
                details["vector_question"] = question
            reader.feed_data((json.dumps({"type": "info", "data": {"satellite": details}})
                              + "\n").encode())
            with (
                TemporaryDirectory() as state,
                patch("asyncio.open_connection", AsyncMock(return_value=(reader, wire))),
                patch("vector_puck.resident.listen", capture),
            ):
                await session(self.s.config, self.speech, self.dialogue, asyncio.Event(), Path(state))
        self.assertEqual(captured, [(True, True, 100)] + [(True, False, 100)] * 3
                         + [(False, False, 30)] * 3)

    async def test_capture_cut_off_at_maximum_is_never_submitted(self):
        await self.original_turn()
        await self.hear("Hey Vector, could you set two timers?")
        self.assertEqual(self.wire.count("vector-question"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        for _ in range(16):
            self.mic(bytes(64000))
        await until(lambda: self.wire.count("audio-stop") == 2)
        self.assertEqual(self.requests, ["set a timer"])
        self.assertIsNone(self.s.focus_turn)

    async def test_overflow_during_confirmation_discards_discontinuous_question(self):
        await self.original_turn()
        await self.hear("Hey Vector")
        await asyncio.sleep(0.26)
        self.speech.heard = "How long is the flight?"
        self.speech.started.clear()
        self.speech.resume.clear()
        self.mic(frame(90))
        self.mic(frame(91))
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
        serial = self.s.interrupt_serial
        for _ in range(self.s.interrupt_audio.maxsize + 1):
            self.mic(frame(93))
        await until(lambda: self.s.interrupt_serial != serial)
        self.speech.resume.set()
        await asyncio.sleep(0.04)
        self.assertEqual(self.s.focus_until, 0)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_microphone_end_before_endpoint_discards_partial_turn(self):
        await self.original_turn()
        await self.hear("Hey Vector, what time is it?")
        self.assertEqual(self.wire.count("vector-question"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        self.emit("audio-stop")
        await until(lambda: self.wire.count("audio-stop") == 2)
        self.assertIsNone(self.s.focus_turn)
        self.assertFalse(self.s.recording)
        self.assertEqual(self.requests, ["set a timer"])

    async def test_count_completes_after_wake_mixed_with_playback_and_repeated_name(self):
        self.old_reply = "1, 2, 3, 4, 5, 6, 7, 8, 9, 10."
        # Identifiable 100 ms packets prove the original output actually drains.
        self.old_audio = b"".join(frame(i) + frame(i)[:1600] for i in range(1, 22))
        old = await self.original_turn("count to ten")
        await self.hear("Three, four. Hey, Victor! Five, six?")
        self.assertEqual(self.wire.count("vector-focus"), 1)
        await self.hear("Hey Vector, Vector? Hey?")
        await self.hear("Seven, eight?")
        await asyncio.wait_for(asyncio.shield(old), 3)
        events = [k for k, _ in self.wire.sent]
        self.assertEqual(events.count("vector-focus"), 1)
        self.assertEqual(events.count("vector-question"), 0)
        self.assertEqual(events.count("vector-stop"), 0)
        self.assertEqual(events.count("detection"), 1)
        self.assertEqual(events.count("audio-stop"), 1)
        audio = [(i, pcm) for i, (k, pcm) in enumerate(self.wire.sent) if k == "audio-chunk"]
        self.assertEqual(b"".join(pcm for i, pcm in audio), self.old_audio)
        times = [self.wire.times[i] for i, pcm in audio]
        self.assertLess(max(b - a for a, b in zip(times, times[1:])), 0.35)
        self.assertTrue(any(i > events.index("vector-focus") for i, pcm in audio))
        self.emit("played")
        await until(lambda: self.dialogue.played.called)
        self.dialogue.played.assert_called_once_with("count to ten", self.old_reply)
        self.assertEqual(self.requests, ["count to ten"])

    async def test_unmarked_wake_pipeline_during_reply_cannot_cancel_or_run_normal_wake(self):
        old = await self.original_turn()
        self.emit("run-pipeline", {"start_stage": "wake"})
        self.speech.heard = "Hey Vector stop"
        for _ in range(4):
            self.mic(frame(89))
        await asyncio.sleep(0.21)
        self.assertFalse(old.done())
        self.assertEqual(self.s.epoch, 0)
        self.assertEqual(len(self.speech.calls), 2)
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.emit("run-pipeline", {"start_stage": "wake", "vector_barge_in": True})
        await self.hear("Hey Vector")
        self.assertEqual(self.wire.count("vector-focus"), 1)

    async def test_focus_only_firmware_never_loses_count_to_an_unsupported_handoff(self):
        self.s.question_capable = False
        old = await self.original_turn()
        await self.hear("Hey Vector, when is sunset?")
        before = self.wire.count("audio-chunk")
        await until(lambda: self.wire.count("audio-chunk") > before)
        self.assertEqual(self.wire.count("vector-focus"), 1)
        self.assertEqual(self.wire.count("detection"), 1)
        self.assertEqual(self.wire.count("vector-question"), 0)
        self.assertEqual(self.wire.count("audio-stop"), 0)
        self.assertFalse(old.done())
        self.assertEqual(self.requests, ["set a timer"])

    async def test_final_bare_detector_can_light_before_slow_asr_but_cannot_cancel(self):
        old = await self.original_turn()
        self.speech.detector.ended = True
        self.speech.heard = "Hey Vector stop"  # Conflicts with the detector's final bare wake.
        self.speech.started.clear()
        self.speech.resume.clear()
        self.mic(frame(89))
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 0.8))
        self.assertEqual(self.wire.count("vector-focus"), 1)
        before = self.wire.count("audio-chunk")
        await until(lambda: self.wire.count("audio-chunk") >= before + 2)
        self.speech.resume.set()
        await asyncio.sleep(0.04)
        self.assertFalse(old.done())
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_wake_next_to_cedars_stop_word_is_only_focus(self):
        self.old_reply = "One, two. Stop at ten. Three, four."
        old = await self.original_turn()
        await self.hear("Hey Vector stop")
        self.assertEqual(self.wire.count("vector-focus"), 1)
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.assertFalse(old.done())

    async def test_quoted_stop_amid_counting_is_not_an_explicit_command(self):
        old = await self.original_turn()
        await self.hear("Seven eight, Hey Vector stop, nine ten")
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.assertFalse(old.done())

    async def test_unconfirmed_visual_cue_never_authorizes_overheard_question(self):
        old = await self.original_turn()
        self.speech.detector.ended = True
        self.speech.heard = "Where is Boston?"
        before = len(self.speech.calls)
        self.mic(frame(89))
        await until(lambda: len(self.speech.calls) > before)
        await asyncio.sleep(0.02)
        self.assertEqual(self.wire.count("vector-focus"), 1)
        self.assertEqual(self.s.focus_until, 0)
        self.assertEqual(self.wire.count("vector-question"), 0)
        self.assertFalse(old.done())

    async def test_fresh_detector_wake_preempts_background_asr_cooldown(self):
        self.old_reply = "One two three four five six seven."
        await self.original_turn()
        self.speech.detector.candidate = ""
        self.speech.heard = "One two three"
        before = len(self.speech.calls)
        for _ in range(8):
            self.mic(frame(73))
        await until(lambda: len(self.speech.calls) > before)
        self.speech.detector.candidate = "hey vector"
        self.speech.heard = "Hey Vector"
        started = time.monotonic()
        self.mic(frame(75))
        self.mic(frame(76))
        async with asyncio.timeout(0.65):
            await until(lambda: self.wire.count("vector-focus") == 1)
        self.assertLess(time.monotonic() - started, 0.65)
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_slow_small_detector_does_not_starve_the_playback_writer(self):
        await self.original_turn()
        started, release = threading.Event(), threading.Event()

        def slow(pcm):
            started.set()
            release.wait(0.8)
            return False

        with patch.object(self.speech.detector, "AcceptWaveform", slow):
            self.mic(frame(99))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                before = self.wire.count("audio-chunk")
                async with asyncio.timeout(0.45):
                    await until(lambda: self.wire.count("audio-chunk") >= before + 2)
                audio_times = [at for at, (kind, _) in zip(self.wire.times, self.wire.sent)
                               if kind == "audio-chunk"]
                self.assertLess(max(b - a for a, b in zip(audio_times, audio_times[1:])), 0.35)
            finally:
                release.set()

    async def test_focus_only_peer_still_supports_verified_addressed_stop(self):
        self.s.question_capable = False
        old = await self.original_turn()
        await self.hear("Hey Vector")
        self.speech.detector.candidate = "hey vector stop"
        await self.hear("Hey Vector, stop counting please.")
        await until(old.cancelled)
        self.assertEqual(self.wire.count("vector-stop"), 1)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_output_pacing_accounts_for_time_spent_writing_each_packet(self):
        self.old_audio = frame(806) * 15  # Ten 100 ms reply packets.

        async def slow_drain():
            await asyncio.sleep(0.035)
        with patch.object(self.wire, "drain", slow_drain):
            old = await self.original_turn()
            await self.hear("Hey Vector")
            await asyncio.wait_for(asyncio.shield(old), 2)
        audio = [t for t, (kind, _) in zip(self.wire.times, self.wire.sent)
                 if kind == "audio-chunk"]
        self.assertEqual(len(audio), 10)
        self.assertGreater(audio[-1] - audio[0], 0.84)
        self.assertLess(audio[-1] - audio[0], 1.03)
        self.assertEqual(self.wire.count("vector-question"), 0)
        self.assertEqual(self.wire.count("vector-stop"), 0)

    async def test_agreeing_addressed_stop_detectors_when_reply_mentions_stop(self):
        self.old_reply = "We should stop at ten."
        old = await self.original_turn()
        self.speech.detector.candidate = "hey vector stop"
        await self.hear("Hey Vector, stop.")
        await until(old.cancelled)
        self.assertEqual(self.wire.count("vector-stop"), 1)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_played_reopens_normal_pipeline_while_last_output_write_drains(self):
        self.old_audio = frame(88) * 3
        release = asyncio.Event()

        async def drain():
            if self.wire.sent[-1][0] == "audio-stop":
                await release.wait()
            await asyncio.sleep(0)

        with patch.object(self.wire, "drain", drain):
            try:
                old = await self.original_turn()
                await until(lambda: self.wire.count("audio-stop") == 1)
                self.emit("played")
                self.emit("run-pipeline", {"start_stage": "wake"})
                await until(lambda: self.dialogue.played.called)
                await until(lambda: self.s.events.qsize() > 0)
                self.assertFalse(old.done())
                release.set()
                await until(lambda: self.wire.count("detection") == 2)
                self.assertEqual(self.wire.count("vector-question"), 0)
            finally:
                release.set()

    async def test_controller_logs_focus_and_handoff_without_recognized_text(self):
        await self.original_turn()
        with patch("builtins.print") as printed:
            await self.hear("Hey Vector")
            await self.hear("Hey Vector")
            await self.hear("When is dinner?")
            records = [json.loads(call.args[0]) for call in printed.call_args_list]
        self.assertEqual([r for r in records if r["event"] != "request_cancelled"],
                         [{"event": "focus_acquired"}, {"event": "question_handoff"}])
        self.assertEqual(self.wire.count("vector-question"), 1)

    async def test_partial_wake_seeds_free_recognition_with_real_preroll(self):
        from vector_puck.spoken_stop import wake_audio
        await self.original_turn()
        free = Detector()
        free.AcceptWaveform = Mock(return_value=False)
        self.speech.recognizer = Mock(return_value=free)
        self.speech.vosk = object()
        self.speech.detector.candidate = ""
        self.mic(frame(53))
        await until(lambda: self.s.interrupt_checks["max_rms"] > 0)
        self.speech.detector.candidate = "[unk] hey"
        self.mic(frame(65))
        await until(lambda: free.AcceptWaveform.called)
        self.speech.recognizer.assert_called_once_with(self.speech.vosk, 16000)
        free.AcceptWaveform.assert_called_once_with(wake_audio(frame(53) + frame(65)))
        self.assertEqual(self.wire.count("vector-focus"), 0)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_verifier_survives_stream_invalidation_before_it_resumes(self):
        from vector_puck.spoken_stop import OverlapChecks
        checks = OverlapChecks()
        verifier = asyncio.create_task(checks.verify(self.s, self.speech))
        try:
            await asyncio.sleep(0)
            checks.ready.set()
            checks.discard()
            await asyncio.sleep(0)
            self.assertFalse(verifier.done())
            self.assertEqual(self.s.interrupt_checks["local_checks"], 0)
        finally:
            verifier.cancel()
            await asyncio.gather(verifier, return_exceptions=True)

    async def test_whisper_only_wake_anchors_clean_audio_and_both_vosk_detectors_get_raw_mic(self):
        await self.original_turn()
        reference = Mock()
        reference.clean_mic.side_effect = lambda pcm, *, rate: frame(2400)
        self.s.playback_reference = reference
        self.speech.detector.candidate = ""
        self.speech.detector.AcceptWaveform = Mock(return_value=False)
        free = Detector()
        free.candidate = ""
        free.AcceptWaveform = Mock(return_value=False)
        self.speech.recognizer = Mock(return_value=free)
        self.speech.vosk = object()
        self.speech.heard = "Hey Vector"
        self.speech.started.clear()
        self.speech.resume.clear()
        try:
            for _ in range(8):
                self.mic(frame(1800))
            self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
            self.mic(frame(1850))
            self.mic(frame(1900))
            await until(lambda: self.s.interrupt_checks["audio_seconds"] >= 1)
            self.speech.recognizer.assert_not_called()
            self.speech.resume.set()
            await until(lambda: self.s.focus_until > 0)
            self.assertEqual(self.speech.calls[-1], frame(2400) * 8)
            self.mic(frame(1950))
            await until(lambda: free.AcceptWaveform.call_count >= 4)
        finally:
            self.speech.resume.set()
        raw_frames = [frame(1800)] * 8 + [frame(1850), frame(1900), frame(1950)]
        observed = [call.args[0] for call in self.speech.detector.AcceptWaveform.call_args_list]
        self.assertEqual(observed, raw_frames)
        anchored = [call.args[0] for call in free.AcceptWaveform.call_args_list]
        self.assertEqual(b"".join(anchored), b"".join(raw_frames))
        self.assertEqual(self.wire.count("vector-focus"), 1)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def acoustically_cue(self, *, heard="One two three"):
        # Inject only local detector hypotheses. The real worker owns the ring,
        # timing, ASR requests, transport and final dispatch in these tests.
        self.free = Detector()
        self.free.candidate = ""
        self.speech.recognizer = lambda model, rate: self.free
        self.speech.vosk = object()
        self.speech.heard = heard
        self.speech.detector.candidate = "[unk] hey"
        self.mic(frame(65))
        self.speech.detector.candidate = "[unk] hey vector"
        for _ in range(4):
            self.mic(frame(81))
        await until(lambda: self.s.visual_focus_until > 0)
        self.speech.detector.candidate = ""
        self.assertEqual(self.s.focus_until, 0)

    async def exercise_recovery(self, tail, head, expected):
        self.old_reply = "One two three four five six seven eight."
        self.dialogue.key = "synthetic"
        self.dialogue.quota = Mock()
        self.dialogue.essentials.room_preferences = lambda: {"spoilers": "avoid", "private": "excluded"}
        await self.original_turn("count to eight")
        await self.acoustically_cue()
        self.free.candidate = head
        self.speech.heard = tail
        self.s.dialogue.essentials.fast_path = Mock(side_effect=AssertionError("privileged fast path"))
        with (patch("vector_puck.resident.answer", new_callable=AsyncMock, return_value="An answer.") as answer,
              patch("vector_puck.resident.cedar", new_callable=AsyncMock, return_value=frame(104))):
            for _ in range(4):
                self.mic(frame(101))
            for _ in range(7):
                self.mic(bytes(3200))
            await until(lambda: self.wire.count("audio-stop") == 2)
        self.assertEqual(self.wire.count("vector-question"), 1)
        self.assertEqual(self.wire.count("vector-stop"), 0)
        answer.assert_awaited_once()
        config, key, query, history, guard = answer.await_args.args
        self.assertEqual(query, expected)
        self.assertEqual(history, [])
        self.assertEqual(answer.await_args.kwargs, {"spoiler_mode": "avoid"})
        self.assertEqual(self.requests, ["count to eight"])
        self.dialogue.quota.reserve.assert_called_once()
        self.dialogue.essentials.fast_path.assert_not_called()
        self.assertIsNone(self.s.pending_history)
        self.emit("played")
        await until(lambda: not self.s.speaking)
        self.assertFalse(self.s.followup.open())

    async def test_count_prefix_and_visual_only_wake_recover_a_read_only_question(self):
        await self.exercise_recovery("Five, six, what is the Triforce?", "", "what is the Triforce?")

    async def test_early_acoustic_head_joins_late_subject_and_forces_tail_asr(self):
        await self.exercise_recovery("the Triforce in Zelda?", "what is the", "what is the Triforce in Zelda?")

    async def test_isolated_clip_never_handles_a_stop_call_or_device_command(self):
        from vector_puck.spoken_stop import OverlapCheck, OverlapChecks
        await self.original_turn()
        await self.acoustically_cue()
        job = OverlapCheck(self.s.epoch, self.s.interrupt_serial, time.monotonic(), 16000,
                           frame(81), "", False, 3, True, "", True, self.s.visual_focus_until)
        control_call = Mock()
        self.s.room_runtime = SimpleNamespace(active=None, signaling=None, control_call=control_call)
        for text in ("stop", "Hey Vector stop", "Hey Vector answer the call",
                     "Can you turn on the TV?", "tell the kitchen what is playing"):
            with self.subTest(text=text):
                self.assertFalse(await OverlapChecks().apply(self.s, job, text))
        control_call.assert_not_called()
        self.assertEqual(self.requests, ["set a timer"])
        self.assertEqual(self.wire.count("vector-stop"), 0)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_mute_during_isolated_tail_decode_revokes_capture(self):
        await self.original_turn()
        await self.acoustically_cue()
        self.speech.started.clear()
        self.speech.resume.clear()
        self.speech.heard = "What is the Triforce?"
        for _ in range(4):
            self.mic(frame(104))
        for _ in range(6):
            self.mic(bytes(3200))
        self.assertTrue(await asyncio.to_thread(self.speech.started.wait, 1))
        self.emit("vector-mute", {"muted": True})
        await until(lambda: self.s.hardware_muted)
        self.speech.resume.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.requests, ["set a timer"])
        self.assertIsNone(self.s.focus_turn)
        self.assertEqual(self.wire.count("vector-question"), 0)

    async def test_reference_observes_sent_pcm_and_cleans_marked_recognition_only(self):
        reference = Mock()
        reference.clean_mic.return_value = frame(701)
        self.s.playback_reference = reference
        self.old_audio = frame(820) * 4
        old = await self.original_turn()
        self.mic(frame(99))
        await until(lambda: reference.clean_mic.called)
        reference.clean_mic.assert_called_once_with(frame(99), rate=16000)
        await old
        chunks = [pcm for event, pcm in self.wire.sent if event == "audio-chunk"]
        pushed = [call.args[0] for call in reference.push_playback.call_args_list]
        self.assertEqual(b"".join(chunks), self.old_audio)
        self.assertEqual(pushed, chunks)
        self.assertTrue(all(call.kwargs == {"rate": 24000} for call in reference.push_playback.call_args_list))
        reference.start.assert_called_once()
        self.emit("played")
        await until(lambda: self.s.reference_epoch is None)
        self.assertEqual(reference.method_calls[-1][0], "reset")

    async def test_distinct_windows_must_agree_on_topic_with_only_a_visual_cue(self):
        from vector_puck.spoken_stop import OverlapCheck, OverlapChecks
        self.old_reply = "One two three four."
        await self.original_turn()
        await self.acoustically_cue()
        checks = OverlapChecks()

        def job(end):
            return OverlapCheck(self.s.epoch, self.s.interrupt_serial, time.monotonic(), end,
                                frame(91), "", False, 2, True, lease=self.s.visual_focus_until)

        first = job(16000)
        self.assertFalse(await checks.apply(self.s, first, "What is the traffic?"))
        self.assertFalse(await checks.apply(self.s, job(32000), "One, two. What is the Triforce?"))
        self.assertFalse(await checks.apply(self.s, job(32000), "What is the Triforce?"))
        self.assertTrue(await checks.apply(self.s, job(48000), "What is the Triforce in Zelda?"))
        self.assertTrue(self.s.focus_turn.read_only)

    async def test_reference_resets_on_mute_and_does_not_clean_call_audio(self):
        reference = Mock()
        self.s.playback_reference = reference
        await self.original_turn()
        capture = Mock()
        self.s.room_runtime = SimpleNamespace(active=True, signaling=None, capture=capture, cancel=Mock())
        self.s.activity = "call"
        self.mic(frame(114))
        await until(lambda: capture.called)
        self.assertEqual(capture.call_args.args[1], frame(114))
        reference.clean_mic.assert_not_called()
        self.emit("vector-mute", {"muted": True})
        await until(lambda: self.s.hardware_muted)
        self.assertIsNone(self.s.reference_epoch)
        self.assertEqual(reference.method_calls[-1][0], "reset")

    def test_spoiler_only_request_keeps_game_mode_and_excludes_room_tools(self):
        from vector_puck.assistant import request_body
        body = request_body(Config(), "What is the Triforce in Zelda?", [], spoiler_mode="avoid")
        self.assertIn("spoiler-free", body["instructions"])
        self.assertEqual([tool["type"] for tool in body["tools"]], ["web_search"])
        self.assertEqual(body["input"], [{"role": "user", "content": "What is the Triforce in Zelda?"}])


@unittest.skipUnless(os.environ.get("VECTOR_TEST_ASR_MODELS"), "requires local pinned speech models and FFmpeg flite")
class AcousticOverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_spoken_triforce_over_continuing_count(self):
        """Real pinned Vosk/Whisper; synthetic voices and speaker, no device or paid APIs."""
        import numpy as np
        from vector_puck.resident import respond
        from vector_puck.speech import Speech

        def synth(text, voice):
            pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                           f"flite=text='{text}':voice={voice}", "-ar", "16000",
                                           "-f", "s16le", "-"])
            return np.frombuffer(pcm, dtype="<i2")

        root = Path(os.environ["VECTOR_TEST_ASR_MODELS"])
        config = replace(Config(), working_sound=False, cpu_threads=4,
                         vosk_model=str(root / "vosk-model-small-en-us-0.15"),
                         whisper_model=str(root / "faster-whisper-base.en"))
        speech = await asyncio.to_thread(Speech, config)
        count = ("One, two, three, four, five, six, seven, eight, nine, ten, eleven, twelve, "
                 "thirteen, fourteen, fifteen, sixteen, seventeen, eighteen, nineteen, twenty.")
        far = np.tile(synth(count, "awb"), 2)
        wake = synth("Hey Vector", "rms")
        question = synth("What is the Triforce in Zelda?", "rms")
        start = 16000 + len(wake) + 5600
        near = np.zeros(start + len(question) + 56000, dtype="<i2")
        near[16000:16000 + len(wake)] = wake
        near[start:start + len(question)] = question
        mix = near + far[:len(near)].astype(float) * 0.11
        mic = np.clip(mix, -32768, 32767).astype("<i2").tobytes()
        near_mic = near.tobytes()
        speaker = np.repeat(far, 3)[::2].astype("<i2").tobytes()
        dialogue = SimpleNamespace(key="synthetic", quota=Mock(), clear=Mock(),
                                   essentials=SimpleNamespace(room_preferences=lambda: {"spoilers": "avoid"}),
                                   respond=AsyncMock(return_value=(count, speaker)))
        wire = Wire()
        sat = Satellite(asyncio.StreamReader(), wire, config, dialogue, asyncio.Event())
        sat.gate.muted = False
        sat.focus_capable = sat.question_capable = sat.busy = True
        sat.interrupt_audio = asyncio.Queue(maxsize=100)

        def emit(kind, data, pcm=b""):
            header = {"type": kind, "data": data, "payload_length": len(pcm)}
            sat.reader.feed_data(json.dumps(header).encode() + b"\n" + pcm)

        with (patch("vector_puck.resident.answer", new_callable=AsyncMock, return_value="Answer.") as answer,
              patch("vector_puck.resident.cedar", new_callable=AsyncMock, return_value=bytes(4800))):
            tasks = [asyncio.create_task(coro) for coro in
                     (interruptions(sat, speech), sat.pump(), listen(sat, speech))]
            old = sat.active = asyncio.create_task(respond(sat, "count to twenty", sat.epoch))
            try:
                await until(lambda: bool(sat.reply_text))
                emit("run-pipeline", {"start_stage": "wake", "vector_barge_in": True})
                deadline = time.monotonic()
                for pos in range(0, len(mic), 1600):
                    # The simulated native peer drops its count only at handoff.
                    source = near_mic if wire.count("vector-question") else mic
                    packet = source[pos:pos + 1600]
                    emit("audio-chunk", MIC_FORMAT, packet)
                    deadline += len(packet) / 32000
                    await asyncio.sleep(max(0, deadline - time.monotonic()))
                async with asyncio.timeout(5):
                    while wire.count("audio-stop") < 2:
                        await asyncio.sleep(0.02)
                self.assertEqual(wire.count("vector-focus"), 1)
                self.assertEqual(wire.count("vector-question"), 1)
                self.assertEqual(wire.count("vector-stop"), 0)
                answer.assert_awaited_once()
                self.assertEqual(answer.await_args.args[2].lower().strip(" ?."), "what is the triforce in zelda")
                self.assertEqual(answer.await_args.args[3], [])
                self.assertEqual(answer.await_args.kwargs, {"spoiler_mode": "avoid"})
                dialogue.respond.assert_awaited_once()
            finally:
                sat.stop.set()
                for task in [old, *tasks]:
                    task.cancel()
                await asyncio.gather(old, *tasks, return_exceptions=True)
