"""Local addressed-stop recognition, isolated from conversation and home tools."""
from __future__ import annotations

from array import array
import asyncio
from collections import deque
import json
import math
import re
import time

from .transport import check_audio
from .call_signaling import call_control


def addresses(wake_phrase: str) -> list[str]:
    name = wake_phrase.split()[-1]
    names = [wake_phrase, name]
    if name == "vector":
        names.extend([wake_phrase.rsplit("vector", 1)[0] + "victor", "victor"])
    return list(dict.fromkeys(names))


def stop_phrases(wake_phrase: str) -> list[str]:
    return [f"{name} {command}" for name in addresses(wake_phrase)
            for command in ("stop", "cancel", "be quiet", "hang up", "end call",
                            "answer call", "answer the call", "pick up", "decline call")]


def interrupt_requested(text: str, wake_phrase: str) -> bool:
    if not isinstance(text, str) or len(text) > 2000:
        return False
    address = "|".join(r"[ ,.!?]+".join(map(re.escape, value.split()))
                       for value in addresses(wake_phrase))
    return bool(re.search(
        r"\b(?:" + address + r")[ ,.!?]+(?:(?:can|could|would) you )?(?:please )?"
        r"(?:stop|cancel|quiet|be quiet|enough|that's enough|shut up|hold on|wait|pause|hang up|end (?:the )?call)\b",
        text, re.I))


def audio_level(pcm: bytes) -> float:
    samples = array("h", pcm)
    return math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768


def wake_audio(pcm: bytes) -> bytes:
    """Bounded local gain only. Quiet noise is never amplified into a command."""
    if not pcm or len(pcm) % 2:
        raise ValueError("Invalid stop audio")
    samples = array("h", pcm)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    peak = max(abs(value) for value in samples)
    if rms < 12 or not peak:
        return pcm
    gain = max(1.0, min(6.0, 1200 / rms, 30000 / peak))
    return array("h", (max(-32768, min(32767, int(value * gain)))
                       for value in samples)).tobytes()


async def interruptions(satellite, speech):
    satellite.interrupt_worker_running = True
    try:
        await interrupt_loop(satellite, speech)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A dead stop worker must not leave an apparently healthy listener behind.
        satellite.disconnect()
    finally:
        satellite.interrupt_worker_running = False


async def interrupt_loop(satellite, speech):
    detector = speech.interrupt_detector()
    ring, size, stable, previous_stream = deque(), 0, 0, None
    next_check = 0.0
    phrase = satellite.config.wake_phrase
    while (satellite.gate.connected and not satellite.stop.is_set()
           and not asyncio.current_task().cancelling()):
        try:
            async with asyncio.timeout(0.25):
                epoch, serial, data, payload = await satellite.interrupt_audio.get()
        except asyncio.TimeoutError:
            continue
        if (not satellite.barge_stream or serial != satellite.interrupt_serial
                or not satellite.gate.allowed(epoch)):
            ring.clear()
            size = stable = 0
            detector.Reset()
            continue
        check_audio(data, payload)
        if (epoch, serial) != previous_stream:
            ring.clear()
            size = stable = 0
            detector.Reset()
            previous_stream = (epoch, serial)
            next_check = 0.0
        ring.append(payload)
        size += len(payload)
        stats = satellite.interrupt_checks
        stats["audio_seconds"] = round(stats["audio_seconds"] + len(payload) / 32000, 2)
        while size > 112000 and len(ring) > 1:
            size -= len(ring.popleft())
        ended = detector.AcceptWaveform(wake_audio(payload))
        result = json.loads(detector.Result() if ended else detector.PartialResult())
        words = result.get("text" if ended else "partial", "")
        stable = stable + 1 if (interrupt_requested(words, phrase) or
            (satellite.room_runtime and satellite.room_runtime.signaling and
             call_control(words, phrase))) else 0
        level = audio_level(payload)
        stats["max_rms"] = round(max(level, stats["max_rms"]), 6)
        # Small-recognizer misses do not block independent local Whisper checks.
        due = size >= 51200 and level >= 0.0006
        if time.monotonic() < next_check or (stable < 2 and not due):
            continue
        next_check = time.monotonic() + 1.5
        stats["local_checks"] += 1
        heard = await asyncio.to_thread(speech.transcribe, wake_audio(b"".join(ring)))
        current = (satellite.barge_stream and serial == satellite.interrupt_serial
                   and satellite.gate.allowed(epoch))
        action = call_control(heard, phrase)
        handled = bool(current and action and satellite.room_runtime
                       and satellite.room_runtime.control_call(action, epoch))
        if handled and action == "answer":
            stats["confirmed"] += 1
            ring.clear()
            size = 0
        elif current and (interrupt_requested(heard, phrase) or handled):
            stats["confirmed"] += 1
            satellite.invalidate()
            satellite.barge_stream = False
            await satellite.send("vector-stop")
        stable = 0
        detector.Reset()
