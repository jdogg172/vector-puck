"""Announcements and accepted calls, using private same-host datagram audio."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import socket
import stat
import struct
import time
import math
from array import array

from .state import Cancelled as ControlCancelled
from .transport import check_audio
from .household import Household
from .resident import status

from .update_indicator import choose_indicator

RATE = 16000
HEADER = struct.Struct("!16sQd")


def call_ring_audio(outgoing=False):
    """Original two-burst telephone chime, independent of cloud speech."""
    rate = 24000
    amplitude = 2400 if outgoing else 6000
    samples = array("h")
    for _ in range(2):
        for i in range(int(rate * 0.32)):
            t = i / rate
            envelope = min(t / 0.012, 1, max(0, (0.32 - t) / 0.025))
            samples.append(
                round(
                    amplitude
                    * envelope
                    * (math.sin(2 * math.pi * 440 * t) + math.sin(2 * math.pi * 660 * t))
                )
            )
        samples.extend([0] * int(rate * 0.16))
    import sys

    if sys.byteorder != "little":
        samples.byteswap()
    return rate, samples.tobytes()


class CallAudio:
    """No files contain PCM. UNIX socket permissions scope access to the residents."""

    def __init__(self, household):
        self.household = household
        self.path = household.root / ("audio." + household.room + ".sock")
        if len(str(self.path).encode()) > 100:
            raise ValueError("Audio socket path is too long")
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise ValueError("Unexpected audio socket path")
            self.path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        self.sock.bind(str(self.path))
        self.path.chmod(0o600)
        self.call = None
        self.sequence = 0
        self.received = -1

    def set_call(self, call):
        self.call, self.sequence, self.received = call, 0, -1
        while True:
            try:
                self.sock.recv(16384)
            except BlockingIOError:
                break

    def peer(self):
        c = self.call
        return c["callee"] if c["caller"] == self.household.room else c["caller"]

    def capture(self, data, pcm):
        if self.call is None:
            return
        check_audio(data, pcm)
        if not pcm or len(pcm) > 9600:
            return
        self.sequence += 1
        packet = HEADER.pack(bytes.fromhex(self.call["id"]), self.sequence, time.monotonic()) + pcm
        try:
            self.sock.sendto(packet, str(self.household.root / ("audio." + self.peer() + ".sock")))
        except (BlockingIOError, FileNotFoundError, ConnectionRefusedError):
            pass  # Never accumulate stale room audio.

    def decode(self, packet, address):
        if self.call is None or len(packet) <= HEADER.size or len(packet) > HEADER.size + 9600:
            return None
        if address != str(self.household.root / ("audio." + self.peer() + ".sock")):
            return None
        ident, seq, stamp = HEADER.unpack_from(packet)
        pcm = packet[HEADER.size :]
        if (
            ident.hex() != self.call["id"]
            or seq <= self.received
            or len(pcm) % 2
            or not 0 <= time.monotonic() - stamp < 1
        ):
            return None
        self.received = seq
        return pcm

    def close(self):
        self.sock.close()
        self.path.unlink(missing_ok=True)


class RoomRuntime:
    def __init__(self, household: Household, satellite, speaker, *, alarms=None, facts=None):
        self.household, self.satellite, self.speaker = household, satellite, speaker
        self.audio = CallAudio(household)
        self.active = None
        self.rang = set()
        self.last_ring = {}
        self.signaling = None
        self.signaling_ready = asyncio.Event()
        self.ring_clip = call_ring_audio()
        self.idle_since = None
        self.transport_task = None
        self.alarms, self.facts = alarms, facts
        self.alarm_ids = set()
        self.weather_kind = "none"
        self.last_indicator = None

    def cancel(self):
        # The microphone forwarding gate closes synchronously before disk I/O.
        self.audio.call = None
        self.active = None
        self.household.end_call()
        self.household.release()

    def capture(self, data, pcm):
        if self.active and self.satellite.allowed_base(self.satellite.epoch):
            self.audio.capture(data, pcm)

    def alarm_state(self, ringing):
        if not ringing and self.alarms is not None:
            for ident in self.alarm_ids:
                self.alarms.dismiss_ringing(ident)
            self.alarm_ids.clear()

    def control_call(self, action, epoch):
        """Answer belongs to the called room, independent of conversation leases."""
        s = self.satellite
        expected = self.signaling
        current = self.household.call()
        if (
            not expected
            or not s.allowed_base(epoch)
            or not current
            or current["id"] != expected["id"]
            or current["status"] != "ringing"
        ):
            return False

        def guard():
            if not s.allowed_base(epoch):
                raise ControlCancelled("Call control cancelled")

        if action == "answer" and current["callee"] == self.household.room:
            self.household.answer_call(guard)
            current = self.household.call()
            accepted = bool(
                current and current["id"] == expected["id"] and current["status"] == "active"
            )
            if accepted:
                status("household_call_accepted")
            return accepted
        if action == "end":
            self.household.end_call(guard)
            return True
        return False

    async def ring_call(self, call):
        """Continuous local signalling with live, explicit call controls on both ends.

        Keep the AFE echo-reference stream open across silent gaps. Finishing an
        accepted ring uses audio-stop, never vector-stop (which emits cancel).
        """
        s = self.satellite
        if (
            not s.allowed_base(s.epoch)
            or s.busy
            or s.speaking
            or s.recording
            or s.followup.active()
            or s.followup.ready
            or self.active
        ):
            return False
        s.activity = "call_wait"
        s.busy = True
        s.followup.clear()
        self.household.release()
        s.epoch += 1
        epoch = s.epoch
        self.signaling = call
        self.signaling_ready.clear()
        outgoing = call["caller"] == self.household.room
        rate, tone = call_ring_audio(outgoing=outgoing)
        pcm = tone
        if not outgoing:
            prompt = self.household.root / "call-prompts" / (call["caller"] + ".pcm")
            try:
                if not prompt.is_symlink() and 0 < prompt.stat().st_size <= rate * 2 * 8:
                    speech = prompt.read_bytes()
                    if len(speech) % 2 == 0:
                        pcm += bytes(rate // 3 * 2) + speech
            except OSError:
                pass
        first_cycle = pcm + bytes(max(0, rate * 2 * 6 - len(pcm)))
        cycle = tone + bytes(rate * 2 * 6 - len(tone))
        s.alert_epoch = epoch
        s.alert_played.clear()
        played = False

        def pending():
            current = self.household.call()
            return bool(
                s.allowed_base(epoch)
                and current
                and current["id"] == call["id"]
                and current["status"] == "ringing"
            )

        try:
            await s.send("detection", {"name": s.config.wake_phrase})
            await s.send("transcript", {"text": ""})
            if getattr(s, "barge_capable", False):
                await s.send("vector-listen")
            await s.send("audio-start", {"rate": rate, "width": 2, "channels": 1})
            started = time.monotonic()
            offset = 0
            status("household_call_ringback" if outgoing else "household_call_signaling")
            while pending():
                clip = first_cycle if offset < len(first_cycle) else cycle
                pos = (
                    offset
                    if offset < len(first_cycle)
                    else (offset - len(first_cycle)) % len(cycle)
                )
                chunk = clip[pos : pos + 4800]
                delay = started + offset / (rate * 2) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if not pending():
                    break
                await s.send("audio-chunk", {"rate": rate, "width": 2, "channels": 1}, chunk)
                offset += len(chunk)
            if s.output_open:
                await s.send("audio-stop")
            # Drain at most one paced packet and observe played before the bridge
            # starts. A vector-stop here would cancel an already accepted call.
            deadline = time.monotonic() + 2
            while (
                s.allowed_base(epoch)
                and not (s.alert_played.is_set() and self.signaling_ready.is_set())
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.02)
            played = s.alert_played.is_set()
            if s.allowed_base(epoch) and not (played and self.signaling_ready.is_set()):
                raise OSError("Call signalling playback did not finish")
            status("household_call_signaling_finished")
            return played
        finally:
            if s.output_open:
                await s.send("audio-stop")
            self.signaling = None
            s.alert_epoch = None
            s.busy = False
            s.activity = "idle"

    async def sync_alarms(self):
        s = self.satellite
        if self.alarms is None or not s.native_alarm:
            return
        ringing = self.alarms.tick()
        wanted = {a["id"] for a in ringing}
        for ident in self.alarm_ids - wanted:
            self.alarm_ids.discard(ident)
            await s.send("vector-alarm", {"action": "stop", "id": ident})
        for alarm in ringing:
            if alarm["id"] not in self.alarm_ids:
                self.alarm_ids.add(alarm["id"])
                await s.send(
                    "vector-alarm", {"action": "start", "id": alarm["id"], "name": alarm["name"]}
                )

    async def wake(self, quality):
        s = self.satellite
        before = s.epoch
        try:
            won = await asyncio.to_thread(self.household.nominate, quality)
        except (OSError, ValueError):
            return False
        if not s.allowed_base(before):
            self.household.release()
            return False
        if won:
            s.activity = "request"
        return won

    async def say(self, text, *, message=None):
        """Read a queued notification only while idle and physically unmuted."""
        from .cloud_voice import cedar
        from .resident import paced

        s = self.satellite
        if (
            not s.allowed_base(s.epoch)
            or s.busy
            or s.speaking
            or s.recording
            or s.followup.active()
            or s.followup.ready
            or self.active
        ):
            if message is not None:
                self.household.finish_message(message, False)
            return False
        s.activity, s.busy = "announcement", True
        s.epoch += 1
        epoch = s.epoch
        s.alert_epoch = epoch
        s.alert_played.clear()
        delivered = False
        try:

            def guard():
                if not s.allowed(epoch):
                    raise ControlCancelled("Announcement cancelled")

            self.speaker.quota.reserve()
            pcm = await cedar(s.config, self.speaker.key, text, guard)
            guard()
            await s.send("detection", {"name": s.config.wake_phrase})
            await s.send("transcript", {"text": ""})
            if s.config.experimental_spoken_stop:
                await s.send("vector-listen")
            await s.send("audio-start", {"rate": 24000, "width": 2, "channels": 1})
            await paced(s, pcm, epoch)
            await s.send("audio-stop")
            deadline = time.monotonic() + 5
            while s.allowed(epoch) and not s.alert_played.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            delivered = s.alert_played.is_set() and s.allowed(epoch)
            if not delivered and s.connected and s.epoch != epoch:
                delivered = not s.hardware_muted  # Action/stop dismisses; mute defers.
        except ControlCancelled:
            pass
        except Exception:
            status("household_announcement_failed")
        finally:
            if s.output_open:
                await s.send("audio-stop")
            s.alert_epoch = None
            s.busy = False
            s.activity = "idle"
            if message is not None:
                self.household.finish_message(message, delivered)
        return delivered

    async def bridge(self, call):
        s = self.satellite
        self.household.release()
        s.activity = "call"
        s.followup.clear()
        s.epoch += 1
        epoch = s.epoch
        self.active = call
        self.audio.set_call(call)
        try:
            await s.send("detection", {"name": "hey_vector"})
            await s.send("transcript", {"text": ""})
            await s.send("vector-listen")
            await s.send("audio-start", {"rate": RATE, "width": 2, "channels": 1})
            status("household_call_connected")
            loop = asyncio.get_running_loop()
            while self.active and s.allowed(epoch):
                current = self.household.call()
                if current is None or current["id"] != call["id"] or current["status"] != "active":
                    break
                try:
                    packet, address = await asyncio.wait_for(
                        loop.sock_recvfrom(self.audio.sock, 16384), 0.2
                    )
                    pcm = self.audio.decode(packet, address)
                except asyncio.TimeoutError:
                    pcm = bytes(3200)  # Bound idle output, retaining AFE's echo reference.
                if pcm and self.active and s.allowed(epoch):
                    await s.send("audio-chunk", {"rate": RATE, "width": 2, "channels": 1}, pcm)
        finally:
            self.audio.call = None
            self.active = None
            self.household.end_call()
            if s.connected:
                await s.send("vector-stop")
                if s.output_open:
                    await s.send("audio-stop")
            s.speaking = False
            s.activity = "idle"
            s.epoch += 1
            status("household_call_ended")

    async def maintain(self):
        """Keep leases and availability fresh even during a long spoken message."""
        s = self.satellite
        while s.connected and not s.stop.is_set():
            muted = not s.allowed_base(s.epoch)
            occupied = (
                s.busy or s.speaking or s.recording or s.followup.active() or s.followup.ready
            )
            try:
                self.household.heartbeat(muted=muted, busy=bool((occupied and s.activity != "ambient") or self.active))
                self.household.export_public()
                await self.sync_alarms()
                if s.native_indicator:
                    call = self.household.call()
                    incoming = bool(call and call['status'] in {'ringing', 'active'})
                    ambient = getattr(s, "ambient", None)
                    messages = bool(self.household.pending_messages() or (ambient and ambient.essentials.notices.pending()))
                    updates = getattr(s, "updates", None)
                    update = updates.indicator() if updates and getattr(s, "native_update_indicator", False) else None
                    indicator = choose_indicator(weather=self.weather_kind, call=incoming,
                        alarm=bool(self.alarms and self.alarms.ringing()), messages=messages, update=update)
                    key = indicator['kind']
                    if key != self.last_indicator:
                        await s.send('vector-indicator', indicator)
                        self.last_indicator = key
                if self.household.owns_turn() and not occupied:
                    self.idle_since = self.idle_since or time.monotonic()
                    if time.monotonic() - self.idle_since > 0.8:
                        self.household.release()
                        s.activity = "idle"
                else:
                    self.idle_since = None
                    if not occupied and s.activity == "request" and not self.household.owns_turn():
                        s.activity = "idle"
            except Exception:
                self.household.valid_until = 0
                if self.active:
                    self.audio.call = None
                    self.active = None
                    s.epoch += 1
                    await s.send("vector-stop")
                status("household_coordinator_failed")
            await asyncio.sleep(0.5)

    async def run(self):
        s = self.satellite
        try:
            while s.connected and not s.stop.is_set():
                muted = not s.allowed_base(s.epoch)
                occupied = (
                    s.busy or s.speaking or s.recording or s.followup.active() or s.followup.ready
                )
                if muted:
                    if self.active:
                        self.cancel()
                elif not occupied and not self.active:
                    call = self.household.call()
                    if call and call["status"] == "active":
                        self.transport_task = asyncio.create_task(self.bridge(call))
                        await asyncio.sleep(0)
                    elif call and call["status"] == "ringing":
                        await self.ring_call(call)
                    else:
                        task = s.hub.kitchen.claim_due()
                        if task is not None:
                            delivered = await self.say(s.hub.kitchen.announcement(task))
                            s.hub.kitchen.finish_delivery(task, delivered)
                        message = self.household.claim_message()
                        if message is not None:
                            sender = self.household.rooms[message["sender"]]
                            await self.say(
                                "Announcement from " + sender + ": " + message["text"],
                                message=message,
                            )
                if self.transport_task and self.transport_task.done():
                    await self.transport_task
                    self.transport_task = None
                await asyncio.sleep(0.25)
        finally:
            self.audio.call = None
            with suppress(Exception):
                self.household.end_call()
                self.household.release()
                self.household.heartbeat(muted=True, busy=False)
            if self.transport_task:
                self.transport_task.cancel()
                await asyncio.gather(self.transport_task, return_exceptions=True)
            self.audio.close()
