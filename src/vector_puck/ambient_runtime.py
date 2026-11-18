"""Finite local PCM playback, yielding to every foreground voice/audio action.

Uses the existing Wyoming speaker and AFE echo-reference path. No firmware,
global volume, microphones, URLs, model calls, or media downloads are controlled.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import time

from .ambient import RATE, loop_packet, playback_loop
from .ring_signals import weather_indicator


class AmbientRuntime:
    def __init__(self, essentials, satellite, *, kitchen=None):
        self.essentials, self.satellite, self.kitchen = essentials, satellite, kitchen
        self.active = False
        self.serial = 0
        self.clips = {}
        self.hold_until = 0.0
        self.essentials.playback_ready = True
        self.essentials.ring_info = self.describe_ring

    def describe_ring(self):
        s = self.satellite
        if s.hardware_muted:
            return "The physical microphone is muted; its red indicator takes priority. The microphone stays off."
        room = s.room_runtime
        weather = weather_indicator(getattr(room, "weather_snapshot", None))["kind"]
        if weather != "none":
            colour = {"warning": "pulsing red", "watch": "pulsing orange", "advisory": "pulsing yellow",
                      "flood-watch": "alternating orange and blue pulses", "flood-warning": "alternating red and blue pulses",
                      "flood-advisory": "pulsing blue"}[weather]
            return f"There is a {weather.replace('-', ' ')}; its signal is {colour}. Ask for the current weather alerts for official instructions. Clearing delivery notices will not clear it."
        if room and room.household.call():
            return "There is a room call. The receiving room can say answer call or decline call."
        if room and room.household.pending_messages():
            return "The white pulse means there are pending room messages."
        if self.essentials.notices.pending():
            return "The white pulse means there are unread delivery notices. Ask me to read notifications, or clear delivery notifications."
        updates = getattr(s, "updates", None)
        update = updates.indicator()["kind"] if updates else "none"
        if update == "update":
            return "The rotating RGB light means an application update is in progress."
        if update == "updated":
            return "The brief green pulse means the updater verified a successful update."
        return "There are no pending weather or message indicators. The ring also shows listening, speaking and alarms as they happen."

    def focus_taken(self, *, permanent=False):
        """Called before foreground playback; no old PCM may follow its start."""
        was_active = self.active
        self.serial += 1
        self.active = False
        self.hold_until = time.monotonic() + 1
        if was_active:
            self.satellite.speaking = False
            self.satellite.output_open = False
            if self.satellite.activity == "ambient":
                self.satellite.activity = "idle"
        if permanent:
            self.essentials.sleep.change("pause")
        return was_active

    def foreground(self):
        s = self.satellite
        followup = s.followup.active() or s.followup.ready
        room = s.room_runtime
        if s.busy or s.recording or followup or s.activity not in {"idle", "ambient"}:
            return True
        if room and (
            room.active
            or room.signaling
            or room.household.call()
            or room.household.pending_messages()
        ):
            return True
        alarms = getattr(room, "alarms", None) if room else None
        if alarms and alarms.ringing():
            return True
        # A due timer/reminder must not wait ten hours for foreground eligibility.
        if self.kitchen and any(
            t.get("due", float("inf")) <= time.time() for t in self.kitchen.tasks()
        ):
            return True
        return False

    async def stop_stream(self):
        if self.active:
            self.active = False
            self.serial += 1
            self.hold_until = time.monotonic() + 1
            if self.satellite.activity == "ambient":
                self.satellite.activity = "idle"
            # Drain only our bounded <=200 ms lead. Never cancel an incoming call.
            # Retain speaking=True until the native played event releases audio.
            await self.satellite.send("audio-stop")

    async def run(self):
        s = self.satellite
        try:
            while s.connected and not s.stop.is_set():
                value = self.essentials.sleep.get()
                allowed = s.allowed_base(s.epoch)
                if (
                    not value
                    or value["paused"]
                    or not allowed
                    or self.foreground()
                    or s.speaking
                    or time.monotonic() < self.hold_until
                ):
                    await asyncio.sleep(0.2)
                    continue
                if value["sound"] not in self.clips:
                    self.clips[value["sound"]] = await asyncio.to_thread(playback_loop, value["sound"])
                # Generation may have taken a second. Recheck after that await.
                if (
                    not s.allowed_base(s.epoch)
                    or self.foreground()
                    or s.speaking
                    or self.essentials.sleep.get() != value
                ):
                    continue
                clip = self.clips[value["sound"]]
                self.serial += 1
                serial = self.serial
                self.active = True
                s.activity = "ambient"
                s.followup.clear()
                # A new wake-stage pipeline also listens while local PCM plays.
                await s.send("vector-listen")
                await s.send("audio-start", {"rate": RATE, "width": 2, "channels": 1})
                next_send = time.monotonic()
                offset, fade = 0, 0
                check_at = 0.0
                while self.active and self.serial == serial and s.connected and not s.stop.is_set():
                    now = time.monotonic()
                    if now >= check_at:
                        check_at = now + 0.4
                        current = self.essentials.sleep.get()
                        if not s.allowed_base(s.epoch) or self.foreground() or current != value:
                            break
                    if self.essentials.sleep.clock() >= value["ends"]:
                        break
                    # One 100 ms packet; the start and final two seconds fade.
                    import numpy as np

                    packet, offset = loop_packet(clip, offset, RATE // 10 * 2)
                    raw = np.frombuffer(packet, dtype="<i2").astype(np.float32)
                    remaining = value["ends"] - self.essentials.sleep.clock()
                    envelope = min(1.0, fade / (RATE * 2), max(0.0, remaining / 2))
                    pcm = np.clip(raw * (value["level"] / 30) * envelope, -32768, 32767).astype("<i2").tobytes()
                    if self.serial != serial or not self.active:
                        break
                    await s.send("audio-chunk", {"rate": RATE, "width": 2, "channels": 1}, pcm)
                    fade += len(pcm) // 2
                    # Keep one packet in reserve for routine scheduling jitter.
                    # After a stall, don't dump a backlog into the speaker queue.
                    next_send = max(next_send + 0.1, time.monotonic())
                    await asyncio.sleep(max(0.0, next_send - 0.1 - time.monotonic()))
                if self.serial == serial:
                    await self.stop_stream()
                await asyncio.sleep(0.2)
        finally:
            self.essentials.playback_ready = False
            # A disconnected speaker discards its PCM. Preserve the original
            # deadline for reconnect/restart; physical mute pauses explicitly.
            with suppress(ConnectionError, OSError):
                await self.stop_stream()
