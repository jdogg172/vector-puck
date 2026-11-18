"""Local recognition for addressed stops and optional playback focus handoff."""
from __future__ import annotations

from array import array
import asyncio
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
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
    names = addresses(wake_phrase)
    full_names = [name for name in names if " " in name]
    return full_names + [f"{name} {command}" for name in names
            for command in ("stop", "cancel", "be quiet", "hang up", "end call",
                            "answer call", "answer the call", "pick up", "decline call")]


def stop_command(text: str, wake_phrase: str) -> str | None:
    """Whisper must hear one complete addressed command."""
    if not isinstance(text, str) or len(text) > 2000:
        return None
    address = "|".join(re.escape(name) for name in addresses(wake_phrase))
    match = re.fullmatch(
        r"(?:" + address + r") (?:(?:can|could|would) you )?(?:please )?"
        r"(stop(?: talking| speaking| counting)?|cancel|quiet|be quiet|enough|that s enough|"
        r"shut up|hold on|wait|pause|hang up|end (?:the )?call)(?: now)?(?: please)?",
        " ".join(re.findall(r"[a-z0-9]+", text.casefold())),
    )
    return match[1] if match else None


def interrupt_requested(text: str, wake_phrase: str) -> bool:
    return stop_command(text, wake_phrase) is not None


def addressed_prompt(text: str, phrase: str, reply: str = "") -> str | None:
    """Ignore a known playback prefix and repeated wakes, retaining the actual question."""
    tokens = phrase.split()
    wake_words = words(phrase)
    name = r"(?:vector|victor)" if tokens[-1] == "vector" else re.escape(tokens[-1])
    address = r"[^\w]+".join([*(re.escape(token) for token in tokens[:-1]), name])
    tail = None
    end = 0
    for match in re.finditer(r"\b" + address + r"\b", text, re.I):
        before = text[end:match.start()]
        prefix = words(before)
        while prefix and prefix[-1] in wake_words:
            prefix.pop()
        if prefix and (tail is not None or not playback_echo(" ".join(prefix), reply)):
            break
        end = match.end()
        tail = text[end:].lstrip(" ,.!?:;—–-\t\r\n")
    if tail is not None and all(token in wake_words for token in words(tail)):
        return ""
    return tail


def words(text: str) -> list[str]:
    return ["vector" if token == "victor" else token
            for token in re.findall(r"[a-z0-9]+", text.casefold())]


_UNITS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def playback_words(text: str) -> list[str]:
    # Cedar's reply may contain digits while local ASR spells the count out.
    result = []
    for token in words(text):
        if token == "100":
            result.extend(("one", "hundred"))
        elif token.isdigit() and len(token) <= 2:
            number = int(token)
            if number < 20:
                result.append(_UNITS[number])
            else:
                result.append(_TENS[number // 10 - 2])
                if number % 10:
                    result.append(_UNITS[number % 10])
        else:
            result.append(token)
    return result


def playback_echo(heard: str, reply: str) -> bool:
    # Compare complete words, including quotes of the wake phrase or stop command.
    sample, spoken = playback_words(heard), playback_words(reply)
    if not sample or not spoken:
        return False
    if f" {' '.join(sample)} " in f" {' '.join(spoken)} ":
        return True
    if len(sample) < 5:
        return False
    # Whisper can drop or substitute a word. Require a nearly contiguous
    # quote so ordinary shared words elsewhere in an answer do not match.
    matches = SequenceMatcher(None, sample, spoken, autojunk=False).get_matching_blocks()[:-1]
    return (sum(block.size for block in matches) >= len(sample) - 1
            and matches[-1].b + matches[-1].size - matches[0].b <= len(sample) + 1)


def question_started(prompt: str) -> bool:
    tokens = words(prompt)
    if len(tokens) < 2 or tokens[-1] in {
        "a", "an", "the", "s", "my", "your", "to", "of", "for", "and",
        "about", "please", "uh", "um",
    }:
        return False
    starters = {
        "what", "when", "where", "which", "who", "whose", "why", "how",
        "can", "could", "will", "would", "should", "is", "are", "am", "was",
        "were", "do", "does", "did", "has", "have", "any", "explain", "tell",
    }
    scaffold = starters | {"me", "you", "i", "please", "a", "an", "the", "s"}
    # Local ASR often adds a question mark to an uncertain count or wake.
    while tokens and tokens[0] in {"and", "so", "please"}:
        tokens = tokens[1:]
    return bool(tokens and tokens[0] in starters and any(word not in scaffold for word in tokens[1:]))


@dataclass(frozen=True)
class FocusTurn:
    """Only the local interrupt worker can enqueue this listener event."""
    epoch: int
    prompt: str
    addressed: bool
    previous_query: str
    reply: str

    def query(self, text: str, phrase: str) -> str | None:
        prompt = addressed_prompt(text, phrase, self.reply)
        if prompt is None:
            if self.addressed:
                return None
            prompt = text.strip()
        prefix = words(self.prompt)
        tokens = words(prompt)
        if (tokens[:len(prefix)] != prefix or tokens == words(self.previous_query)
                or not question_started(prompt) or playback_echo(prompt, self.reply)):
            return None
        return prompt


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


def detect_audio(detector, payload: bytes) -> tuple[bool, str, float]:
    ended = detector.AcceptWaveform(wake_audio(payload))
    result = json.loads(detector.Result() if ended else detector.PartialResult())
    return ended, result.get("text" if ended else "partial", ""), audio_level(payload)


def transcribe_audio(speech, pcm: bytes) -> str:
    return speech.transcribe(wake_audio(pcm))


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
    detector = await asyncio.to_thread(speech.interrupt_detector)
    ring, size, stable, previous_stream = deque(), 0, 0, None
    next_check = next_candidate = 0.0
    last_voiced = last_received = 0.0
    phrase = satellite.config.wake_phrase
    while (satellite.gate.connected and not satellite.stop.is_set()
           and not asyncio.current_task().cancelling()):
        try:
            async with asyncio.timeout(0.25):
                epoch, serial, data, payload = await satellite.interrupt_audio.get()
        except asyncio.TimeoutError:
            continue
        received = data.get("_vector_received_at", time.monotonic())
        if not satellite.interrupt_current(epoch, serial, received):
            ring.clear()
            size = stable = 0
            detector.Reset()
            continue
        check_audio(data, payload)
        stream = (epoch, serial)
        if stream != previous_stream or received - last_received > 2:
            if stream == previous_stream:
                satellite.focus_until = satellite.visual_focus_until = 0.0
            ring.clear()
            size = stable = 0
            detector.Reset()
            previous_stream = stream
            next_check = next_candidate = last_voiced = 0.0
        last_received = received
        ring.append(payload)
        size += len(payload)
        stats = satellite.interrupt_checks
        stats["audio_seconds"] = round(stats["audio_seconds"] + len(payload) / 32000, 2)
        while size > 112000 and len(ring) > 1:
            size -= len(ring.popleft())
        # The playback writer is paced on the event loop. Both recognizers
        # and gain processing can take longer than a speaker packet.
        ended, candidate, level = await asyncio.to_thread(detect_audio, detector, payload)
        if not satellite.interrupt_current(epoch, serial, received):
            continue
        addressed = addressed_prompt(candidate, phrase) if satellite.focus_capable else None
        proposed = (addressed is not None or interrupt_requested(candidate, phrase)
                    or (satellite.room_runtime and satellite.room_runtime.signaling
                        and call_control(candidate, phrase)))
        stable = stable + 1 if proposed else 0
        stats["max_rms"] = round(max(level, stats["max_rms"]), 6)
        if level >= 0.0006:
            last_voiced = received
        recent_voice = last_voiced > 0 and received - last_voiced < 1.6
        final_wake = ended and addressed == "" and recent_voice and bool(satellite.reply_text)
        if final_wake:
            await satellite.candidate_focus(epoch, serial, received)

        now = time.monotonic()
        due = size >= 51200 and level >= 0.0006
        if satellite.focus_capable and satellite.reply_text:
            minimum = 12800 if satellite.focus_until > now else 25600
            due = size >= minimum and recent_voice
        # A fresh detector proposal need not wait for the periodic fallback's
        # 1.5-second cooldown. Bound candidate checks independently.
        ready = stable >= 2 or final_wake
        if not ((ready and now >= next_candidate) or (due and now >= next_check)):
            continue
        next_check = now + 1.5
        if ready:
            next_candidate = now + 0.5
        stats["local_checks"] += 1
        pcm = b"".join(ring)
        heard = await asyncio.to_thread(transcribe_audio, speech, pcm)
        if not satellite.interrupt_current(epoch, serial, received):
            continue
        if satellite.focus_capable and playback_echo(heard, satellite.reply_text):
            # This background check can land halfway through a real wake.
            # Keep the streaming detector and its bounded audio context intact.
            continue
        command = stop_command(heard, phrase)
        # An ambiguous stop may acknowledge a full address actually heard
        # by Whisper. It cannot cancel or authorize a new question.
        ambiguous_stop = bool(command and satellite.focus_capable and satellite.reply_text
                              and (final_wake or (playback_echo(command, satellite.reply_text)
                                                  and not interrupt_requested(candidate, phrase))))
        action = None if ambiguous_stop else call_control(heard, phrase)
        handled = bool(action and satellite.room_runtime
                       and satellite.room_runtime.control_call(action, epoch))
        consumed = False
        if handled and action == "answer":
            stats["confirmed"] += 1
            consumed = True
        elif (command and not ambiguous_stop) or handled:
            stats["confirmed"] += 1
            await satellite.stop_reply()
        elif satellite.focus_capable and not action:
            consumed = await satellite.focus_input(epoch, serial, received, heard, pcm,
                                                   wake_only=ambiguous_stop)
        if consumed:
            ring.clear()
            size = stable = 0
            detector.Reset()
            # Wait for fresh voiced audio before checking a short follow-on.
            last_voiced = 0.0
            next_check = next_candidate = time.monotonic() + 0.25
