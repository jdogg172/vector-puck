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

from .cloud_voice import Dialogue, read_key
from .config import Config
from .speech import Speech, done, question
from .spoken_stop import interruptions
from .state import Cancelled, FollowUp, Gate
from .transport import REPLY_FORMAT, check_audio, read_event, send_event
from .tune import working_audio


def status(event):
    # Enumerated events only: no exception bodies, transcripts, addresses, audio or keys.
    print(json.dumps({"event": event}), flush=True)


class Satellite:
    def __init__(self, reader, writer, config, dialogue, stop):
        self.reader, self.writer, self.config, self.dialogue, self.stop = (
            reader, writer, config, dialogue, stop)
        self.gate = Gate()
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

    @property
    def epoch(self):
        return self.gate.epoch

    @epoch.setter
    def epoch(self, value):
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

    async def send(self, kind, data=None, payload=b""):
        async with self.write_lock:
            await send_event(self.writer, kind, data, payload)
        if kind == "audio-start":
            self.output_open = self.speaking = True
            self.speech_started = time.monotonic()
        elif kind == "audio-stop":
            self.output_open = False

    def invalidate(self):
        self.gate.cancel()
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
                    self.last_audio = self.last_event
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
                    self.gate.muted = data["muted"]
                    self.invalidate()
                    self.barge_stream = False
                    await self.send("pause-satellite" if self.gate.muted else "run-satellite")
                elif kind == "vector-cancel":
                    self.invalidate()
                    self.barge_stream = False
                elif kind == "run-pipeline":
                    self.last_audio = self.last_event
                    self.barge_stream = data.get("vector_barge_in") is True
                    if self.barge_stream:
                        self.interrupt_serial += 1
                        self.interrupt_checks = {"audio_seconds": 0.0, "local_checks": 0,
                                                 "confirmed": 0, "max_rms": 0.0}
                        while not self.interrupt_audio.empty():
                            self.interrupt_audio.get_nowait()
                        continue  # Never interpret a stop-only stream as a conversation turn.
                    if self.busy:
                        self.invalidate()
                elif kind == "played":
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
                elif kind == "vector-alarm-state" and self.room_runtime:
                    if type(data.get("ringing")) is not bool:
                        raise ValueError("Invalid alarm state")
                    self.room_runtime.alarm_state(data["ringing"])
                    continue
                if kind == "audio-chunk" and self.barge_stream:
                    if self.config.experimental_spoken_stop and not self.gate.muted:
                        if self.interrupt_audio.full():
                            self.interrupt_audio.get_nowait()
                        self.interrupt_audio.put_nowait((self.gate.epoch, self.interrupt_serial,
                                                        data, event[2]))
                    continue
                if kind == "audio-chunk" and (self.busy or self.gate.muted):
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
        self.invalidate()
        while not self.events.empty():
            self.events.get_nowait()
        self.enqueue(("disconnected", {}, b""))
        self.writer.close()

    async def monitor(self, state):
        while not self.stop.is_set() and self.gate.connected:
            if any(task.done() for task in self.household_tasks):
                self.disconnect()
                return
            self.dialogue.expire()
            temp = state / "health.tmp"
            temp.write_text(json.dumps({
                "updated": time.time(), "connected": self.gate.connected,
                "muted": self.gate.muted, "busy": self.busy, "speaking": self.speaking,
                "followup_open": self.followup.active(),
                "spoken_stop": self.config.experimental_spoken_stop,
                "interrupt_worker_running": self.interrupt_worker_running,
                "interrupt_listening": self.barge_stream,
                "interrupt_checks": self.interrupt_checks.copy(),
            }))
            temp.replace(state / "health.json")
            now = time.monotonic()
            stale_audio = (not self.gate.muted and not self.busy and not self.speaking
                           and now - self.last_audio > 20)
            stuck_playback = self.speaking and now - self.speech_started > 125
            if now - self.last_event > 20 or stale_audio or stuck_playback:
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
    for offset in range(0, len(pcm), 4800):
        satellite.guard(epoch)
        if done_event and done_event.is_set():
            return
        chunk = pcm[offset:offset + 4800]
        await satellite.send("audio-chunk", REPLY_FORMAT, chunk)
        if done_event:
            try:
                await asyncio.wait_for(done_event.wait(), len(chunk) / 48000)
                return
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(len(chunk) / 48000)


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


async def respond(satellite, query, epoch):
    finished = asyncio.Event()
    cue_task = asyncio.create_task(working(satellite, epoch, finished)) if satellite.config.working_sound else None
    completed = False
    try:
        if satellite.config.experimental_spoken_stop:
            await satellite.send("vector-listen")
        result, pcm = await satellite.dialogue.respond(query, lambda: satellite.guard(epoch))
        finished.set()
        if cue_task:
            await cue_task  # No tune bytes may race the final reply.
        satellite.guard(epoch)
        await satellite.send("audio-start", REPLY_FORMAT)
        await paced(satellite, pcm, epoch)
        satellite.guard(epoch)
        if satellite.hub and satellite.hub.household.call():
            # Call setup/answer must yield to the bridge, not an eight-second follow-up.
            satellite.followup.clear()
        else:
            satellite.followup.expect(epoch)
        satellite.pending_history = (epoch, query, result)
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
    post = 0
    following = following_started = False
    awaiting_pipeline = True
    epoch = satellite.gate.epoch
    while not satellite.stop.is_set() and satellite.gate.connected:
        if satellite.room_runtime and satellite.room_runtime.active:
            await asyncio.sleep(.1)
            continue
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
        if kind in {"vector-mute", "vector-cancel", "run-pipeline"}:
            speech.reset()
            ring.clear()
            size = post = 0
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
        check_audio(data, payload)
        if recorded is None:
            ring.append(payload)
            size += len(payload)
            while size > 16000 * 2 * 3.5 and len(ring) > 1:
                size -= len(ring.popleft())
            if not speech.wake_candidate(payload):
                continue
            candidate = await asyncio.to_thread(speech.transcribe, b"".join(ring))
            if not satellite.gate.allowed(epoch):
                continue
            if question(candidate, satellite.config.wake_phrase) is None:
                speech.reset()
                ring.clear()
                size = 0
                continue
            if satellite.room_runtime:
                from .spoken_stop import audio_level
                quality = audio_level(b"".join(ring)[-32000:])
                if not await satellite.room_runtime.wake(quality):
                    speech.reset()
                    ring.clear()
                    size = 0
                    continue
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
        if not speech.endpoint_ready(payload, post / 32000):
            continue
        satellite.busy = True
        awaiting_pipeline = True
        await satellite.send("transcript", {"text": ""})
        try:
            text = await asyncio.to_thread(speech.transcribe, b"".join(recorded)[-32000 * 34:])
            satellite.guard(epoch)
            query = text.strip() if following else question(text, satellite.config.wake_phrase)
            if not query or len(query) > satellite.config.max_request_chars:
                satellite.followup.clear()
                await satellite.finish()
                continue
            direct_alarm = (satellite.hub and satellite.hub.fast_path(query))
            if done(query) and not direct_alarm:
                satellite.followup.clear()
                satellite.dialogue.clear()
                await satellite.finish()
                continue
            if not following:
                satellite.followup.begin()
            satellite.active = asyncio.create_task(respond(satellite, query, epoch))
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
            satellite.recording = False
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
        satellite.native_alarm = details.get("vector_alarm") is True
        satellite.native_indicator = details.get("vector_indicator") is True
        if satellite.hub:
            from .household_runtime import RoomRuntime
            satellite.hub.alarm_ready = satellite.native_alarm
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
        tasks.extend([asyncio.create_task(satellite.pump()),
                      asyncio.create_task(satellite.monitor(state))])
        if not satellite.gate.muted:
            await satellite.send("run-satellite")
        status("connected")
        await listen(satellite, speech)
    finally:
        satellite.gate.connected = False
        satellite.invalidate()
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
