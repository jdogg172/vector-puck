"""One puck, one bounded request, immediate physical mute/Action cancellation."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import suppress
import json
from pathlib import Path
import signal
import time

from .assistant import answer
from .cloud_voice import Dialogue, cedar, read_key
from .config import Config
from .overlap_audio import PlaybackReference
from .speech import Speech, done, question
from .spoken_stop import FocusTurn, audio_level, transcribe_audio, addressed_prompt, interruptions, playback_echo, question_started, words
from .state import Cancelled, FollowUp, Gate
from .transport import MIC_FORMAT, REPLY_FORMAT, check_audio, read_event, send_event
from .tune import working_audio
from .listener_state import ListenerState


def status(event):
    # Enumerated events only: no exception bodies, transcripts, addresses, audio or keys.
    print(json.dumps({"event": event}), flush=True)


class Satellite:
    def __init__(self, reader, writer, config, dialogue, stop):
        self.reader, self.writer, self.config, self.dialogue, self.stop = (
            reader, writer, config, dialogue, stop)
        self.gate = Gate()
        self.playback_reference = PlaybackReference()
        self.reference_epoch = None
        self.followup = FollowUp(config.followup_seconds, config.followup_turns, config.session_seconds)
        self.events = asyncio.Queue(maxsize=128)
        self.interrupt_audio = asyncio.Queue(maxsize=30)
        self.interrupt_worker_running = False
        self.interrupt_serial = 0
        self.interrupt_checks = {"audio_seconds": 0.0, "local_checks": 0,
                                 "confirmed": 0, "max_rms": 0.0}
        self.write_lock = asyncio.Lock()
        self.active = None
        self.busy = self.speaking = self.output_open = self.barge_stream = False
        self.pending_history = None
        self.focus_capable = self.question_capable = False
        self.focus_until = self.visual_focus_until = 0.0
        self.focus_turn = None
        self.reply_text = self.reply_query = ""
        self.alarm_ringing = False
        self.last_event = self.last_audio = time.monotonic()
        self.speech_started = 0.0
        self.hub = getattr(dialogue, "household", None)
        self.room_runtime = None
        self.recording = False
        self.activity = "idle"
        self.alert_epoch = None
        self.alert_played = asyncio.Event()
        self.native_alarm = self.native_indicator = self.barge_capable = False
        self.household_tasks = []
        self.listener = ListenerState()
        self.ambient = None
        self.native_update_indicator = False
        self.native_weather_indicator = False
        self.updates = None
        self.native_version = None

    @property
    def epoch(self):
        return self.gate.epoch

    @epoch.setter
    def epoch(self, value):
        self.clear_focus()
        self.gate.epoch = value

    @property
    def connected(self):
        return self.gate.connected

    @property
    def hardware_muted(self):
        return self.gate.muted

    def allowed_base(self, epoch):
        return self.gate.allowed(epoch)

    def allowed(self, epoch):
        return (self.allowed_base(epoch) and
                (not self.hub or self.activity != "request" or self.hub.household.owns_turn()))

    def guard(self, epoch):
        if not self.allowed(epoch):
            raise Cancelled("Request cancelled")

    def reset_reference(self) -> None:
        self.playback_reference.reset()
        self.reference_epoch = None

    def clear_focus(self) -> None:
        self.reset_reference()
        self.focus_until = self.visual_focus_until = 0.0
        if self.focus_turn is not None:
            self.recording = False
        self.focus_turn = None
        self.reply_text = self.reply_query = ""

    def interrupt_current(self, epoch: int, serial: int, received: float, *, max_age=2) -> bool:
        return (self.barge_stream and self.interrupt_serial == serial
                and self.gate.allowed(epoch) and time.monotonic() - received <= max_age)

    def focus_available(self, epoch: int) -> bool:
        room = self.room_runtime
        return (self.gate.allowed(epoch) and self.activity in {"idle", "request"}
                and self.alert_epoch is None and not self.alarm_ringing
                and not (self.ambient and self.ambient.active)
                and (not self.hub or (self.activity == "request"
                                      and self.hub.household.owns_turn()))
                and not (room and (room.active or room.signaling or room.household.call())))

    async def light_focus(self) -> None:
        """Write lock held. A duplicate cannot renew the peer's visual lease."""
        if time.monotonic() >= self.visual_focus_until:
            self.focus_until = 0.0
            self.visual_focus_until = time.monotonic() + 8
            await send_event(self.writer, "vector-focus")
            status("focus_acquired")

    async def candidate_focus(self, epoch: int, serial: int, received: float) -> None:
        # A final local wake can light the ring while Whisper runs. Only
        # confirmed addressed speech can open the window for a later question.
        async with self.write_lock:
            if (self.interrupt_current(epoch, serial, received) and self.speaking
                    and self.reply_text and self.focus_available(epoch)
                    and not playback_echo(self.config.wake_phrase, self.reply_text)):
                await self.light_focus()

    async def stop_reply(self) -> None:
        self.invalidate()
        self.barge_stream = False
        await self.send("vector-stop")
        status("reply_interrupted")

    async def focus_input(self, epoch: int, serial: int, received: float,
                          heard: str, pcm: bytes, *, wake_only: bool = False,
                          recovered: bool = False, acoustic_head: str = "", ended: bool = False, check=None) -> bool:
        """Return true when this recognition window has been consumed."""
        phrase = self.config.wake_phrase
        tail = addressed_prompt(heard, phrase, self.reply_text)
        async with self.write_lock:
            if (not self.interrupt_current(epoch, serial, received, max_age=4 if recovered else 2)
                    or (check and (not check.contiguous or
                        (recovered and check.lease != self.visual_focus_until)))):
                return False
            if not self.speaking or not self.reply_text or not self.focus_available(epoch):
                self.focus_until = self.visual_focus_until = 0.0
                return False
            focused = time.monotonic() < self.focus_until
            if recovered:
                if (not self.question_capable or time.monotonic() >= self.visual_focus_until
                        or (self.focus_until and not focused)):
                    return False
            elif tail is None and not focused:
                return False
            prompt = heard.strip() if tail is None else tail
            if wake_only or playback_echo(prompt, self.reply_text):
                # The human wake and Cedar's next word can share one ASR result.
                if tail is None or playback_echo(phrase, self.reply_text):
                    return True
                prompt = ""
            if words(prompt) == words(self.reply_query):
                return True
            if not self.question_capable or not question_started(prompt):
                if tail is not None:
                    await self.light_focus()
                    self.focus_until = self.visual_focus_until
                # Retain an incomplete question in the rolling buffer.
                return not prompt or not self.question_capable

            # The existing room owns this new turn. Mute/Action invalidate the
            # entire session, but a handoff preserves the lease and played history.
            turn = FocusTurn(epoch + 1, prompt, tail is not None, self.reply_query, self.reply_text,
                             read_only=recovered, acoustic_head=acoustic_head, ended=ended)
            self.gate.cancel()
            self.clear_focus()
            self.focus_turn = turn
            self.followup.clear()
            self.pending_history = None
            if self.active and not self.active.done():
                self.active.cancel()
            self.barge_stream = self.speaking = self.busy = False
            self.recording = True
            while not self.events.empty():
                self.events.get_nowait()
            if check and not ended:
                pcm += check.following_audio
            accepted = time.monotonic() if recovered else received
            self.enqueue((turn, dict(MIC_FORMAT, _vector_received_at=accepted), pcm))
            # Preserve question audio received while Whisper was running. All
            # subsequent microphone frames go directly to the normal listener.
            while not self.interrupt_audio.empty():
                previous, stream, data, payload = self.interrupt_audio.get_nowait()
                if previous == epoch and stream == serial:
                    self.enqueue(("audio-chunk", dict(data, _vector_epoch=turn.epoch), payload))
            await send_event(self.writer, "vector-question")
            status("question_handoff_read_only" if recovered else "question_handoff")
            if self.output_open and self.gate.allowed(turn.epoch):
                self.output_open = False
                await send_event(self.writer, "audio-stop")
            return True

    async def send(self, kind, data=None, payload=b""):
        if self.ambient and kind in {"detection", "vector-alarm", "vector-stop", "pause-satellite"}:
            self.ambient.focus_taken(permanent=self.connected and kind in {"vector-stop", "pause-satellite"})
        async with self.write_lock:
            if kind in {"detection", "vector-alarm", "vector-stop", "pause-satellite", "audio-start"}:
                self.clear_focus()
            if kind == "vector-alarm" and data:
                self.alarm_ringing = data.get("action") == "start"
            if kind == "audio-start":
                if self.focus_capable:
                    self.interrupt_serial += 1
                self.output_open = self.speaking = True
                self.speech_started = time.monotonic()
            elif kind == "audio-stop":
                self.output_open = False
            await send_event(self.writer, kind, data, payload)
            if kind == "audio-chunk" and self.reference_epoch == self.epoch:
                self.playback_reference.push_playback(payload, rate=data["rate"])


    def invalidate(self, *, pause_sleep=True):
        if self.ambient:
            self.ambient.focus_taken(permanent=pause_sleep)
        self.gate.cancel()
        self.clear_focus()
        self.recording = False
        self.activity = "idle"
        if self.room_runtime:
            self.room_runtime.cancel()
        self.followup.clear()
        self.pending_history = None
        self.dialogue.clear()
        if self.active and not self.active.done():
            self.active.cancel()

    def enqueue(self, event):
        if self.events.full():
            raise ValueError("Satellite event overflow")
        self.events.put_nowait(event)

    async def pump(self):
        try:
            while not self.stop.is_set():
                event = await asyncio.wait_for(read_event(self.reader), 20)
                kind, data, _ = event
                self.last_event = time.monotonic()
                if kind == "audio-chunk":
                    if self.barge_stream and self.last_event - self.last_audio > 2:
                        self.reset_reference()
                    self.last_audio = self.last_event
                    data = dict(data, _vector_received_at=self.last_event,
                                _vector_epoch=self.gate.epoch)
                    event = (kind, data, event[2])
                    if self.room_runtime and self.room_runtime.active and not self.gate.muted:
                        self.room_runtime.capture(data, event[2])
                if kind == "ping":
                    await self.send("pong")
                    continue
                if kind == "pong":
                    continue
                if kind == "vector-mute":
                    if type(data.get("muted")) is not bool:
                        raise ValueError("Invalid mute report")
                    self.output_open = self.speaking = False
                    self.gate.muted = data["muted"]
                    self.invalidate()
                    self.barge_stream = False
                    await self.send("pause-satellite" if self.gate.muted else "run-satellite")
                elif kind == "vector-cancel":
                    self.output_open = self.speaking = False
                    self.invalidate()
                    self.barge_stream = False
                elif kind == "run-pipeline":
                    if self.barge_stream or data.get("vector_barge_in") is not True:
                        self.reset_reference()
                    self.focus_until = self.visual_focus_until = 0.0
                    if self.focus_turn is not None:
                        self.focus_turn = None
                        self.gate.cancel()
                        self.recording = False
                    self.last_audio = self.last_event
                    self.barge_stream = data.get("vector_barge_in") is True and not (self.ambient and self.ambient.active)
                    if self.barge_stream:
                        self.interrupt_serial += 1
                        self.interrupt_checks = {"audio_seconds": 0.0, "local_checks": 0,
                                                 "confirmed": 0, "max_rms": 0.0}
                        while not self.interrupt_audio.empty():
                            self.interrupt_audio.get_nowait()
                        continue  # Only locally confirmed speech may transfer this stream.
                    if (self.focus_capable and self.config.experimental_spoken_stop
                            and self.activity in {"idle", "request"} and self.alert_epoch is None
                            and (self.reply_text or (self.busy and self.active and not self.active.done()))):
                        # An unmarked restart during prearm/playback is neither
                        # permission to use overlap audio nor a physical cancel.
                        continue
                    if (self.room_runtime and self.room_runtime.signaling
                            and self.alert_played.is_set()):
                        self.room_runtime.signaling_ready.set()
                    elif self.busy:
                        self.invalidate()
                elif kind == "audio-stop" and self.barge_stream:
                    self.reset_reference()
                    self.barge_stream = False
                    self.focus_until = self.visual_focus_until = 0.0
                    continue
                elif kind == "audio-stop" and self.focus_turn and not self.busy:
                    # Microphone ended before the local endpoint completed.
                    self.gate.cancel()
                    self.clear_focus()
                    await self.finish()
                    continue
                elif kind == "played":
                    if self.focus_capable and (not self.speaking or self.output_open):
                        continue  # Late acknowledgement of the displaced output.
                    self.clear_focus()
                    self.speaking = self.barge_stream = False
                    self.last_audio = self.last_event
                    if self.alert_epoch == self.epoch:
                        self.alert_played.set()
                        continue
                    epoch = self.gate.epoch
                    self.followup.played(epoch, self.gate.allowed(epoch))
                    pending = self.pending_history
                    if pending and pending[0] == epoch and self.gate.allowed(epoch):
                        self.dialogue.played(pending[1], pending[2])
                    self.pending_history = None
                    continue
                elif kind == "vector-alarm-state":
                    if type(data.get("ringing")) is not bool:
                        raise ValueError("Invalid alarm state")
                    self.alarm_ringing = data["ringing"]
                    if self.alarm_ringing:
                        self.clear_focus()
                    if self.room_runtime:
                        self.room_runtime.alarm_state(data["ringing"])
                    continue
                if kind == "audio-chunk" and self.barge_stream:
                    if self.config.experimental_spoken_stop and not self.gate.muted:
                        if self.interrupt_audio.full():
                            self.reset_reference()
                            if self.focus_capable:
                                # A gap can remove a negation or part of a question.
                                self.interrupt_serial += 1
                                self.focus_until = self.visual_focus_until = 0.0
                                while not self.interrupt_audio.empty():
                                    self.interrupt_audio.get_nowait()
                            else:
                                self.interrupt_audio.get_nowait()
                        overlap_pcm = event[2]
                        if (self.reference_epoch == self.epoch and self.activity in {"idle", "request"}
                                and not (self.room_runtime and (self.room_runtime.active or self.room_runtime.signaling))):
                            check_audio(data, overlap_pcm)
                            overlap_pcm = self.playback_reference.clean_mic(overlap_pcm, rate=16000)
                        self.interrupt_audio.put_nowait((self.gate.epoch, self.interrupt_serial,
                                                        data, overlap_pcm))
                    continue
                if kind == "audio-chunk" and (self.busy or self.gate.muted
                        or (self.focus_capable and self.speaking and self.reply_text)):
                    continue
                if kind in {"vector-mute", "vector-cancel"}:
                    while not self.events.empty():
                        self.events.get_nowait()
                self.enqueue(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.disconnect()

    def disconnect(self):
        self.gate.connected = False
        self.invalidate(pause_sleep=False)
        while not self.events.empty():
            self.events.get_nowait()
        self.enqueue(("disconnected", {}, b""))
        self.writer.close()

    async def monitor(self, state):
        last_update = None
        while not self.stop.is_set() and self.gate.connected:
            if any(task.done() for task in self.household_tasks):
                self.disconnect()
                return
            self.dialogue.expire()
            if self.room_runtime is None and self.native_update_indicator and self.updates:
                indicator = self.updates.indicator()
                if indicator["kind"] != last_update:
                    await self.send("vector-indicator", indicator)
                    last_update = indicator["kind"]
            temp = state / "health.tmp"
            temp.write_text(json.dumps({
                "updated": time.time(), "connected": self.gate.connected,
                "muted": self.gate.muted, "busy": self.busy, "speaking": self.speaking,
                "followup_open": self.followup.active(),
                "spoken_stop": self.config.experimental_spoken_stop,
                "focus_capable": self.focus_capable,
                "question_capable": self.question_capable,
                "interrupt_worker_running": self.interrupt_worker_running,
                "interrupt_listening": self.barge_stream,
                "interrupt_checks": self.interrupt_checks.copy(),
                "listener": self.listener.snapshot(),
                "native_version": self.native_version,
                "weather_indicator_capable": self.native_weather_indicator, "update_indicator_capable": self.native_update_indicator,
            }))
            temp.replace(state / "health.json")
            now = time.monotonic()
            stale_audio = (not self.gate.muted and not self.busy and not self.speaking
                           and now - self.last_audio > 20)
            stuck_playback = self.speaking and self.activity != "ambient" and now - self.speech_started > 125
            idle_listener = not (self.gate.muted or self.busy or self.speaking or self.recording
                or self.followup.active() or self.followup.ready
                or (self.room_runtime and (self.room_runtime.active or self.room_runtime.signaling)))
            listener_stalled = self.listener.stalled(self.epoch, idle=idle_listener,
                transport_fresh=now - self.last_event < 2 and now - self.last_audio < 2,
                owns_turn=not self.hub or self.activity != "request" or self.hub.household.owns_turn())
            if listener_stalled:
                status("listener_recovering")
            if listener_stalled or now - self.last_event > 20 or stale_audio or stuck_playback:
                self.disconnect()
                return
            if now - self.last_event > 5:
                await self.send("ping")
            await asyncio.sleep(1)

    async def finish(self):
        # Empty transcript prevents household speech from reaching the puck's log.
        await self.send("transcript", {"text": ""})
        await self.send("audio-start", REPLY_FORMAT)
        await self.send("audio-chunk", REPLY_FORMAT, bytes(4800))
        await self.send("audio-stop")


async def paced(satellite, pcm, epoch, done_event=None):
    """At most one 100 ms chunk ahead. Never queue an entire working tune or reply."""
    deadline = time.monotonic()
    for offset in range(0, len(pcm), 4800):
        satellite.guard(epoch)
        if done_event and done_event.is_set():
            return
        chunk = pcm[offset:offset + 4800]
        duration = len(chunk) / 48000
        now = time.monotonic()
        if now - deadline >= duration:
            deadline = now  # A stall must not cause a burst of catch-up audio.
        deadline += duration
        await satellite.send("audio-chunk", REPLY_FORMAT, chunk)
        delay = max(0.0, deadline - time.monotonic())
        if done_event:
            try:
                await asyncio.wait_for(done_event.wait(), delay)
                return
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(delay)


async def working(satellite, epoch, finished):
    try:
        await asyncio.wait_for(finished.wait(), 1.25)
        return
    except asyncio.TimeoutError:
        pass
    satellite.guard(epoch)
    await satellite.send("audio-start", REPLY_FORMAT)
    tune = working_audio()
    deadline = time.monotonic() + 100
    while not finished.is_set() and time.monotonic() < deadline:
        await paced(satellite, tune, epoch, finished)
    # No audio-stop: the final reply completes this same output turn.


async def respond(satellite, query, epoch, *, read_only=False):
    finished = asyncio.Event()
    cue_task = asyncio.create_task(working(satellite, epoch, finished)) if satellite.config.working_sound else None
    completed = False
    try:
        if satellite.config.experimental_spoken_stop:
            await satellite.send("vector-listen")
        def guard():
            satellite.guard(epoch)

        if read_only:
            # This route has no household context, local executors or fast paths.
            guard()
            satellite.dialogue.quota.reserve()
            preferences = getattr(satellite.dialogue.essentials, "room_preferences", None)
            settings = preferences() if callable(preferences) else None
            spoiler = settings.get("spoilers") if isinstance(settings, dict) else None
            spoiler_mode = spoiler if spoiler in ("avoid", "ask", "allow") else None
            result = await answer(satellite.config, satellite.dialogue.key, query, [], guard,
                                  spoiler_mode=spoiler_mode)
            guard()
            pcm = await cedar(satellite.config, satellite.dialogue.key, result, guard)
        else:
            result, pcm = await satellite.dialogue.respond(query, guard)
        finished.set()
        if cue_task:
            await cue_task  # No tune bytes may race the final reply.
        satellite.guard(epoch)
        await satellite.send("audio-start", REPLY_FORMAT)
        satellite.guard(epoch)
        if satellite.config.experimental_spoken_stop:
            satellite.playback_reference.start()
            satellite.reference_epoch = epoch
        if satellite.focus_capable:
            satellite.reply_query, satellite.reply_text = query, result
        await paced(satellite, pcm, epoch)
        satellite.guard(epoch)
        if (read_only or (satellite.hub and satellite.hub.household.call())
                or getattr(getattr(satellite.dialogue, "essentials", None), "last_tool", None) == "sleep_sound"):
            # Call setup/answer must yield to the bridge, not an eight-second follow-up.
            satellite.followup.clear()
        else:
            satellite.followup.expect(epoch)
        satellite.pending_history = None if read_only else (epoch, query, result)
        # busy must drop before the device can send played + the next pipeline.
        satellite.busy = False
        await satellite.send("audio-stop")
        completed = True
        status("reply_sent_waiting_for_played")
    finally:
        finished.set()
        if cue_task:
            cue_task.cancel()
            await asyncio.gather(cue_task, return_exceptions=True)
        satellite.busy = False
        if not completed:
            satellite.followup.clear()
            satellite.pending_history = None
            if satellite.gate.allowed(epoch):
                await satellite.finish()


async def listen(satellite, speech):
    ring, size = deque(), 0
    recorded = None
    post = quiet_bytes = 0
    following = following_started = False
    turn = None
    awaiting_pipeline = True
    epoch = satellite.gate.epoch
    while not satellite.stop.is_set() and satellite.gate.connected:
        if satellite.room_runtime and satellite.room_runtime.active:
            await asyncio.sleep(.1)
            continue
        satellite.listener.progress(
            "waiting_pipeline" if awaiting_pipeline else "followup" if following
            else "recording" if recorded is not None else "wake", epoch, awaiting_pipeline)
        if following and not following_started and not satellite.followup.active():
            following = False
            recorded = None
            satellite.recording = False
            awaiting_pipeline = True
            satellite.followup.clear()
            await satellite.finish()
        try:
            kind, data, payload = await asyncio.wait_for(satellite.events.get(), 0.25)
        except asyncio.TimeoutError:
            continue
        if kind == "disconnected":
            raise EOFError("Satellite disconnected")
        if isinstance(kind, FocusTurn):
            if kind is not satellite.focus_turn:
                continue
            turn, epoch = kind, kind.epoch
            speech.reset()
            ring.clear()
            size = post = quiet_bytes = 0
            following = following_started = awaiting_pipeline = False
            recorded = []
            satellite.recording = True
            kind = "audio-chunk"
        if kind in {"vector-mute", "vector-cancel", "run-pipeline"}:
            turn = None
            speech.reset()
            ring.clear()
            size = post = quiet_bytes = 0
            recorded = None
            following = following_started = False
            awaiting_pipeline = kind != "run-pipeline"
            epoch = satellite.gate.epoch
            if kind == "run-pipeline" and satellite.gate.allowed(epoch):
                if data.get("start_stage") != "wake":
                    raise ValueError("Configure hassmic with -w remote")
                if satellite.followup.open():
                    following = True
                    recorded = []
                    await satellite.send("detection", {"name": satellite.config.wake_phrase})
            continue
        if kind != "audio-chunk" or awaiting_pipeline or not satellite.gate.allowed(epoch):
            continue
        if turn and turn is not satellite.focus_turn:
            turn = None
            awaiting_pipeline = True
            continue
        check_audio(data, payload)
        if (data.get("_vector_epoch", epoch) != epoch
                or time.monotonic() - data.get("_vector_received_at", time.monotonic()) > 2):
            satellite.listener.stale_packets += 1
            if turn:
                turn = satellite.focus_turn = None
                satellite.recording = False
                awaiting_pipeline = True
                await satellite.finish()
            if recorded is None:
                speech.reset()
                ring.clear()
                size = 0
            continue
        satellite.listener.audio_packets += 1
        if recorded is None:
            ring.append(payload)
            size += len(payload)
            while size > 16000 * 2 * 3.5 and len(ring) > 1:
                size -= len(ring.popleft())
            if not speech.wake_candidate(payload):
                continue
            satellite.recording = True
            satellite.listener.progress("confirming_wake", epoch)
            candidate = await asyncio.to_thread(speech.transcribe, b"".join(ring))
            if not satellite.gate.allowed(epoch):
                satellite.recording = False
                continue
            if question(candidate, satellite.config.wake_phrase) is None:
                satellite.recording = False
                satellite.listener.rejected_wakes += 1
                speech.reset()
                ring.clear()
                size = 0
                continue
            if satellite.room_runtime:
                quality = audio_level(b"".join(ring)[-32000:])
                if not await satellite.room_runtime.wake(quality):
                    satellite.recording = False
                    speech.reset()
                    ring.clear()
                    size = 0
                    continue
            satellite.listener.confirmed_wakes += 1
            await satellite.send("detection", {"name": satellite.config.wake_phrase})
            recorded = list(ring)
            satellite.recording = True
            continue
        recorded.append(payload)
        post += len(payload)
        if following and not following_started:
            if not satellite.followup.active() or not speech.speech_present(payload):
                continue
            satellite.followup.consume()
            following_started = True
            satellite.recording = True
        if turn and turn.read_only:
            quiet_bytes = quiet_bytes + len(payload) if audio_level(payload) < 0.0006 else 0
        if not (turn and (turn.ended or quiet_bytes >= 14400)) and not speech.endpoint_ready(payload, post / 32000):
            continue
        satellite.busy = True
        awaiting_pipeline = True
        try:
            query = None
            read_only = bool(turn and turn.read_only)
            # A forced endpoint at the 30-second recording cap may truncate a
            # control or its negation. Require a natural endpoint for handoff.
            if not turn or (post < 30 * 32000 and satellite.focus_available(epoch)):
                await satellite.send("transcript", {"text": ""})
                pcm = b"".join(recorded)[-32000 * 34:]
                if read_only:
                    text = await asyncio.to_thread(transcribe_audio, speech, pcm)
                else:
                    text = await asyncio.to_thread(speech.transcribe, pcm)
                if turn:
                    if turn is not satellite.focus_turn:
                        continue
                    if satellite.focus_available(epoch):
                        query = turn.query(text, satellite.config.wake_phrase)
                        if query is None and not read_only:
                            query = turn.verify_information(text, satellite.config.wake_phrase)
                            read_only = query is not None
                    turn = satellite.focus_turn = None
                else:
                    query = text.strip() if following else question(text, satellite.config.wake_phrase)
            if not query or len(query) > satellite.config.max_request_chars:
                satellite.gate.guard(epoch)
                satellite.followup.clear()
                await satellite.finish()
                continue
            satellite.guard(epoch)
            if not read_only:
                direct_alarm = satellite.hub and satellite.hub.fast_path(query)
                direct_basic = satellite.dialogue.essentials.fast_path(query)
                if done(query) and not direct_alarm and not direct_basic:
                    satellite.followup.clear()
                    satellite.dialogue.clear()
                    await satellite.finish()
                    continue
                if not following:
                    satellite.followup.begin()
            satellite.active = asyncio.create_task(respond(satellite, query, epoch, read_only=read_only))
            try:
                await satellite.active
            except asyncio.CancelledError:
                if satellite.stop.is_set():
                    raise
                status("request_cancelled")
        except Cancelled:
            status("request_cancelled")
        except Exception:
            status("request_failed")
            if satellite.gate.allowed(epoch):
                satellite.followup.clear()
                await satellite.finish()
        finally:
            satellite.busy = False
            recorded = None
            if turn is satellite.focus_turn:
                turn = satellite.focus_turn = None
            satellite.recording = satellite.focus_turn is not None
            speech.reset()


async def session(config, speech, dialogue, stop, state):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(
        config.puck_host, config.puck_port, limit=8192), 5)
    satellite = Satellite(reader, writer, config, dialogue, stop)
    tasks = []
    try:
        await satellite.send("describe")
        kind, info, _ = await asyncio.wait_for(read_event(reader), 5)
        details = info.get("satellite", {})
        if kind != "info" or details.get("vector_controls") != 2:
            raise ValueError("Install the documented controls patch before use")
        if type(details.get("vector_muted")) is not bool:
            raise ValueError("Satellite must report physical mute state")
        satellite.gate.muted = details["vector_muted"]
        satellite.barge_capable = details.get("vector_barge_in") is True
        satellite.focus_capable = details.get("vector_focus") is True
        satellite.question_capable = satellite.focus_capable and details.get("vector_question") is True
        if satellite.focus_capable:
            # Keep about two seconds of 20 ms frames while local ASR runs.
            # The listener queue can accept the seed plus this entire backlog.
            satellite.interrupt_audio = asyncio.Queue(maxsize=100)
        satellite.native_alarm = details.get("vector_alarm") is True
        satellite.native_indicator = details.get("vector_indicator") is True
        from .update_indicator import UpdateIndicator
        satellite.updates = UpdateIndicator(state)
        satellite.native_update_indicator = details.get("vector_update_indicator") is True
        satellite.native_weather_indicator = details.get("vector_weather_indicator") is True
        satellite.native_version = details.get("version")
        if satellite.hub:
            from .household_runtime import RoomRuntime
            satellite.hub.alarm_ready = satellite.native_alarm
            satellite.hub.call_ready = (satellite.barge_capable
                                       and config.experimental_spoken_stop)
            dismissed = details.get("vector_alarm_dismissed")
            if isinstance(dismissed, str) and dismissed:
                satellite.hub.alarms.dismiss_ringing(dismissed)
            satellite.room_runtime = RoomRuntime(satellite.hub.household, satellite, dialogue,
                                                  alarms=satellite.hub.alarms)
            satellite.household_tasks = [asyncio.create_task(satellite.room_runtime.maintain()),
                                         asyncio.create_task(satellite.room_runtime.run())]
            tasks.extend(satellite.household_tasks)
        if config.experimental_spoken_stop:
            tasks.append(asyncio.create_task(interruptions(satellite, speech)))
        from .ambient_runtime import AmbientRuntime
        satellite.ambient = AmbientRuntime(dialogue.essentials, satellite,
                                          kitchen=satellite.hub.kitchen if satellite.hub else None)
        tasks.append(asyncio.create_task(satellite.ambient.run()))
        tasks.extend([asyncio.create_task(satellite.pump()),
                      asyncio.create_task(satellite.monitor(state))])
        if not satellite.gate.muted:
            await satellite.send("run-satellite")
        status("connected")
        await listen(satellite, speech)
    finally:
        satellite.gate.connected = False
        satellite.invalidate(pause_sleep=False)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(Exception):
            await satellite.send("pause-satellite")
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def run(config, state, cache_call_prompts=False):
    config.validate_runtime()
    key = read_key(Path(config.key_file))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    speech = Speech(config)
    home = None
    if config.home_assistant_file:
        from .home_control import HomeControl
        home = HomeControl.load(Path(config.home_assistant_file))
    household = None
    if config.household_file:
        from .household_control import HouseholdControl
        household = HouseholdControl.load(Path(config.household_file), state)
    dialogue = Dialogue(config, key, state, home=home, household=household)
    if cache_call_prompts:
        if not household:
            raise ValueError("Configure household_file first")
        await household.cache_call_prompts(dialogue)
        status("call_prompts_cached_no_device_access")
        return
    from .room_management import make_room_api
    dialogue.room_api = make_room_api(
        state_dir=state, alarms=household.alarms, room_id=household.household.room,
        socket_dir=config.management_socket_dir,
    ) if config.management_socket_dir else None
    if dialogue.room_api:
        dialogue.room_api.start()
        dialogue.essentials.room_preferences = dialogue.room_api.preferences
    try:
        await _run_sessions(config, speech, dialogue, state)
    finally:
        if dialogue.room_api:
            dialogue.room_api.close()


async def _run_sessions(config, speech, dialogue, state):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    runner = asyncio.current_task()
    def shutdown():
        stop.set()
        runner.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
    while not stop.is_set():
        try:
            await session(config, speech, dialogue, stop, state)
        except asyncio.CancelledError:
            break
        except Exception:
            dialogue.clear()
            status("reconnecting")
            try:
                await asyncio.wait_for(stop.wait(), 3)
            except asyncio.TimeoutError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/config/local.json"))
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--cache-call-prompts", action="store_true",
                        help="Generate optional static Cedar caller prompts; API billed, no playback")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        if args.check_config:
            if config.household_file:
                from .household_control import read_config
                read_config(Path(config.household_file))
            if config.home_assistant_file:
                from .home_control import HomeControl
                HomeControl.read_config(Path(config.home_assistant_file))
            status("config_valid")  # No credential or device access.
            return
        asyncio.run(run(config, args.state, args.cache_call_prompts))
    except (Exception, KeyboardInterrupt):
        status("startup_failed_check_config_models_and_key_permissions")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
