"""Local recognition for addressed stops and optional playback focus handoff."""
from __future__ import annotations

from array import array
import asyncio
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import json
import math
import re
import time

from .speech import Speech
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


# A recovery may remove a recognizable piece of the count, never arbitrary
# preceding speech (in particular a quotation, a negation or a command).
_INFO = re.compile(r"\b(?:what|who|where|when|why|which|how|tell me about|explain)\b", re.I)
_COUNT = frozenset(_UNITS + _TENS + ["hundred", "thousand", "and"])


def information_fragment(text: str, phrase: str, reply: str) -> str:
    if phrase.split()[-1] == "vector":
        # This Whisper spelling never enters the command or full-wake gate.
        text = re.sub(r"\bvektor\b", "vector", text, flags=re.I)
    addressed = addressed_prompt(text, phrase, reply)
    text = text.strip() if addressed is None else addressed
    match = _INFO.search(text)
    if match is None:
        return ""
    prefix = text[:match.start()]
    if words(prefix) and not (set(playback_words(prefix)) <= _COUNT and playback_echo(prefix, reply)):
        return ""
    fragment = text[match.start():].strip()
    if playback_echo(fragment, reply) or addressed_prompt(fragment, phrase) is not None:
        return ""
    return fragment


def recovered_question(text: str, phrase: str, reply: str, acoustic_head: str) -> str | None:
    fragment = information_fragment(text, phrase, reply)
    if not fragment and acoustic_head:
        # Join only words the two recognizers actually heard, with a shared
        # boundary. A topic by itself is never a substitute for a question.
        head = list(re.finditer(r"[a-z0-9]+", acoustic_head, re.I))
        tail = words(text)
        if len(tail) >= 2:
            for count in range(min(len(head), len(tail)), 0, -1):
                if words(acoustic_head)[-count:] == tail[:count]:
                    joined = acoustic_head[:head[-count].start()] + text.strip()
                    fragment = information_fragment(joined, phrase, reply)
                    break
    if (fragment.rstrip().endswith(("-", "…", "...")) or not question_started(fragment)
            or playback_echo(fragment, reply)):
        return None
    return fragment


def same_information(first: str, final: str, *, partial=False) -> bool:
    def normalized(text):
        return words(re.sub(r"\b(what|who|where|when|how)['’]s\b", r"\1 is", text, flags=re.I))

    seed = normalized(first)
    while seed and seed[-1] in {"in", "this", "that", "the", "a", "of", "at", "on"}:
        seed.pop()
    if not question_started(" ".join(seed)):
        return False
    # Allow a word boundary to change ("tri force") but require agreement
    # through the topic, not just a shared "what is the ...".
    prefix = "".join(seed)
    tokens = normalized(final)
    if any("".join(tokens[:end]) == prefix for end in range(1, len(tokens) + 1)):
        return True
    # A growing window can complete the last name differently. Require a
    # complete shared topic before that name and acoustic agreement on its onset.
    return (partial and len(seed) == len(tokens) and seed[:-1] == tokens[:-1]
            and len(seed[-1]) >= 3 and tokens[-1].startswith(seed[-1][:3])
            and question_started(" ".join(seed[:-1])))


class QuestionCapture:
    """One short, contiguous recording per candidate, independent of the stop ring."""

    RATE = 32000
    MAX_BYTES = 10 * RATE
    PREROLL = RATE  # A Vosk partial can arrive after the first question word.

    def __init__(self, detector, received: float):
        self.detector = detector
        self.started = received
        self.audio = bytearray()
        self.onset = self.silence = self.new_voice = 0
        self.head = ""
        self.head_start = None
        self.cued = self.anchored = False
        self.regular_probes = self.tail_probes = 0
        self.head_probed = False
        self.next_probe = 0

    def feed(self, pcm: bytes, raw: bytes, phrase: str, reply: str, *, seed=False):
        start = len(self.audio)
        self.audio.extend(pcm)
        if not seed and audio_level(pcm) >= 0.0006:
            if self.silence >= self.RATE * 0.35:
                self.onset = max(0, start - self.RATE // 5)
                self.head = ""
                self.head_start = None
            self.new_voice += len(pcm)
            self.silence = 0
        elif not seed:
            self.silence += len(pcm)
        ended = False
        if self.detector is not None:
            ended, heard = decode_words(self.detector, wake_audio(raw))
            # An unrestricted acoustic decoder can carry earlier counting or
            # a garbled name in its partial. Its question head is corroboration
            # only; Whisper must still decode the actual informational question.
            starts = list(_INFO.finditer(heard))
            head = information_fragment(heard[starts[-1].start():], phrase, reply) if starts else ""
            if head:
                if self.head_start is None:
                    self.head_start = max(self.onset, start - (3 * self.PREROLL if ended else self.PREROLL))
                self.head = head
        return ended

    def prime(self, pcm: bytes, raw: bytes, following: int, phrase: str, reply: str) -> bool:
        boundary = len(pcm) - following
        ended = bool(boundary) and self.feed(pcm[:boundary], raw[:boundary], phrase, reply, seed=True)
        # A periodic Whisper check can authenticate a wake without any Vosk
        # proposal. Replay speech that arrived while that check was running.
        if following:
            self.anchored = True
            for start in range(boundary, len(pcm), self.RATE // 10):
                stop = start + self.RATE // 10
                ended = self.feed(pcm[start:stop], raw[start:stop], phrase, reply)
        return ended

    def probe(self, ended: bool) -> tuple[bytes, bool] | None:
        if self.new_voice < self.RATE // 4:
            return None
        end = len(self.audio)
        terminal = ended or self.silence >= self.RATE * 0.4
        if terminal and self.tail_probes < 2:
            self.tail_probes += 1
            self.new_voice = 0  # Sustained silence gets exactly one final check.
        elif terminal:
            return None
        elif self.head and not self.head_probed and self.new_voice >= self.RATE // 2:
            self.head_probed = True
        elif self.regular_probes < 2 and end >= self.next_probe and self.new_voice >= self.RATE:
            self.regular_probes += 1
        else:
            return None
        self.next_probe = end + self.RATE
        start = self.head_start if self.head_start is not None else self.onset
        # Keep the question onset even when the rolling stop recognizer has
        # advanced beyond it. Strip excess trailing silence from late checks.
        stop = end - max(0, self.silence - self.RATE // 3)
        return bytes(self.audio[start:stop]), terminal


@dataclass(frozen=True)
class FocusTurn:
    """Only the local interrupt worker can enqueue this listener event."""
    epoch: int
    prompt: str
    addressed: bool
    previous_query: str
    reply: str
    read_only: bool = False
    acoustic_head: str = ""
    ended: bool = False

    def verify_information(self, text: str, phrase: str) -> str | None:
        prompt = recovered_question(text, phrase, self.reply, self.acoustic_head or self.prompt)
        if not prompt or words(prompt) == words(self.previous_query):
            return None
        return prompt if same_information(self.prompt, prompt) else None

    def query(self, text: str, phrase: str) -> str | None:
        if self.read_only:
            return self.verify_information(text, phrase)
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


def decode_words(detector, normalized: bytes) -> tuple[bool, str]:
    ended = detector.AcceptWaveform(normalized)
    result = json.loads(detector.Result() if ended else detector.PartialResult())
    return ended, result.get("text" if ended else "partial", "")


def detect_audio(detector, payload: bytes) -> tuple[bool, str, float]:
    ended, text = decode_words(detector, wake_audio(payload))
    return ended, text, audio_level(payload)


def transcribe_audio(speech, pcm: bytes) -> str:
    normalized = wake_audio(pcm)
    if not isinstance(speech, Speech):
        return speech.transcribe(normalized)
    # No temperature retries or long continuations on short, noisy overlap
    # clips. Normal, fully addressed conversation retains its own ASR policy.
    with speech.lock:
        audio = speech.np.frombuffer(normalized, dtype="<i2").astype(speech.np.float32) / 32768
        segments, _ = speech.whisper.transcribe(
            audio, language="en", beam_size=1, temperature=0, max_new_tokens=48,
            condition_on_previous_text=False, vad_filter=False)
        return " ".join(segment.text.strip() for segment in segments).strip()[:2000]


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


@dataclass
class OverlapCheck:
    epoch: int
    serial: int
    received: float
    end: int
    pcm: bytes
    candidate: str
    final_wake: bool
    priority: int
    recovered: bool = False
    head: str = ""
    terminal: bool = False
    lease: float = 0.0
    following_audio: bytearray = field(default_factory=bytearray)
    contiguous: bool = True


class OverlapChecks:
    """One local inference and one replaceable proposal, never a backlog of ASR jobs."""
    def __init__(self):
        self.running = self.pending = self.consumed = self.witness = None
        self.ready = asyncio.Event()

    def discard(self):
        self.pending = self.consumed = self.witness = None
        self.ready.clear()
        if self.running:
            self.running.contiguous = False
            self.running.following_audio.clear()

    def offer(self, job):
        pending = self.pending
        if (pending is None or (pending.epoch, pending.serial) != (job.epoch, job.serial)
                or not pending.contiguous or job.received - pending.received > 2
                or job.priority >= pending.priority):
            self.pending = job
            self.ready.set()

    def append(self, epoch, serial, pcm):
        for job in (self.running, self.pending):
            if job and not job.terminal and job.contiguous and (job.epoch, job.serial) == (epoch, serial):
                if len(job.following_audio) + len(pcm) > (128000 if job.recovered else 64000):
                    job.contiguous = False
                else:
                    job.following_audio.extend(pcm)

    @staticmethod
    def current(satellite, job):
        if job.recovered and (job.lease != satellite.visual_focus_until or time.monotonic() >= job.lease):
            return False
        return job.contiguous and satellite.interrupt_current(
            job.epoch, job.serial, job.received, max_age=4 if job.recovered else 2)

    async def verify(self, satellite, speech):
        while True:
            await self.ready.wait()
            job = self.running = self.pending
            self.pending = None
            self.ready.clear()
            if job is None:  # The stream was invalidated after waking this task.
                continue
            if self.current(satellite, job):
                satellite.interrupt_checks["local_checks"] += 1
                heard = await asyncio.to_thread(transcribe_audio, speech, job.pcm)
                if self.current(satellite, job):
                    if await self.apply(satellite, job, heard):
                        self.consumed = (job.epoch, job.serial, job.end, time.monotonic())
                        if self.pending and not self.pending.recovered and self.pending.end <= job.end:
                            self.pending = None
                            self.ready.clear()
            self.running = None

    async def apply(self, satellite, job, heard):
        phrase, reply = satellite.config.wake_phrase, satellite.reply_text
        if satellite.focus_capable and playback_echo(heard, reply):
            return False
        pcm = job.pcm
        if job.recovered:
            return await self.information(satellite, job, heard)

        # Only the complete, unsliced rolling check may execute an explicit
        # stop/call command. Isolated recordings are permanently read-only.
        command = stop_command(heard, phrase)
        ambiguous = bool(command and satellite.focus_capable and reply and
                         (job.final_wake or (playback_echo(command, reply)
                                             and not interrupt_requested(job.candidate, phrase))))
        action = None if ambiguous else call_control(heard, phrase)
        handled = bool(action and satellite.room_runtime
                       and satellite.room_runtime.control_call(action, job.epoch))
        if handled or (command and not ambiguous):
            satellite.interrupt_checks["confirmed"] += 1
            if not (handled and action == "answer"):
                await satellite.stop_reply()
            return True
        if satellite.focus_capable and not action:
            consumed = await satellite.focus_input(job.epoch, job.serial, job.received, heard, pcm,
                                                   wake_only=ambiguous, check=job)
            if not consumed and not ambiguous and job.lease:
                return await self.information(satellite, job, heard)
            return consumed
        return False


    async def information(self, satellite, job, heard):
        lease = (job.epoch, job.serial, job.lease)
        if not job.lease or satellite.visual_focus_until != job.lease or time.monotonic() >= job.lease:
            return False
        prompt = recovered_question(heard, satellite.config.wake_phrase, satellite.reply_text, job.head)
        if not prompt:
            return False
        # During speech, corroborate the topic via free Vosk or a different
        # Whisper window. A wake alone, including its own ASR packet, cannot qualify.
        previous = self.witness
        agrees = (previous and previous[0] == lease and job.end - previous[1] >= 8000
                  and same_information(previous[2], prompt, partial=True))
        if not job.terminal and not agrees and not same_information(job.head, prompt):
            self.witness = (lease, job.end, prompt)
            return False
        return await satellite.focus_input(job.epoch, job.serial, job.received, prompt, job.pcm,
                                           recovered=True, acoustic_head=job.head, ended=job.terminal,
                                           check=job)


async def interrupt_loop(satellite, speech):
    checks = OverlapChecks()
    async with asyncio.TaskGroup() as group:
        verifier = group.create_task(checks.verify(satellite, speech))
        try:
            await monitor_overlap(satellite, speech, checks)
        finally:
            verifier.cancel()


async def monitor_overlap(satellite, speech, checks):
    detector = await asyncio.to_thread(speech.interrupt_detector)
    ring, raw_ring = bytearray(), bytearray()
    position = stop_start = stable = 0
    previous_stream = None
    capture = None
    capture_blocked = 0.0
    next_check = next_candidate = 0.0
    last_voiced = last_received = 0.0
    phrase = satellite.config.wake_phrase
    while (satellite.gate.connected and not satellite.stop.is_set()
           and not asyncio.current_task().cancelling()):
        try:
            async with asyncio.timeout(0.25):
                epoch, serial, data, payload = await satellite.interrupt_audio.get()
        except asyncio.TimeoutError:
            if not satellite.barge_stream or satellite.hardware_muted:
                capture = None
                ring.clear()
                raw_ring.clear()
                checks.discard()
            continue
        received = data.get("_vector_received_at", time.monotonic())
        if not satellite.interrupt_current(epoch, serial, received):
            checks.discard()
            previous_stream = None
            capture = None
            ring.clear()
            raw_ring.clear()
            position = stop_start = stable = 0
            detector.Reset()
            continue
        check_audio(data, payload)
        raw = payload
        payload = data.get("_vector_recognition_pcm", raw)
        stream = (epoch, serial)
        if stream != previous_stream or received - last_received > 2:
            if stream == previous_stream:
                satellite.focus_until = satellite.visual_focus_until = 0.0
            ring.clear()
            raw_ring.clear()
            position = stop_start = stable = 0
            detector.Reset()
            previous_stream = stream
            checks.discard()
            capture = None
            capture_blocked = 0.0
            next_check = next_candidate = last_voiced = 0.0
        last_received = received
        confirmed_end = None
        consumed = checks.consumed
        if consumed and consumed[:2] == stream:
            keep = max(0, position - consumed[2])
            stop_start = consumed[2]
            if satellite.focus_until > time.monotonic():
                confirmed_end = stop_start
            if not keep:
                detector.Reset()
                stable = 0
            next_check = next_candidate = consumed[3] + 0.25
            checks.consumed = None
        checks.append(epoch, serial, payload)
        position += len(payload)
        ring.extend(payload)
        raw_ring.extend(raw)
        del ring[:max(0, len(ring) - 112000)]
        del raw_ring[:max(0, len(raw_ring) - 112000)]
        size = min(len(ring), position - stop_start)
        stats = satellite.interrupt_checks
        stats["audio_seconds"] = round(stats["audio_seconds"] + len(payload) / 32000, 2)
        # The playback writer is paced on the event loop. Both recognizers
        # and gain processing can take longer than a speaker packet.
        ended, candidate, level = await asyncio.to_thread(detect_audio, detector, raw)
        if not satellite.interrupt_current(epoch, serial, received):
            continue
        # In overlap the constrained recognizer commonly emits "[unk] hey
        # vector". Unknown edge tokens are acceptable only for a visual cue.
        hint = re.sub(r"^(?:\s*\[unk\]\s*)+|(?:\s*\[unk\]\s*)+$", "", candidate).strip()
        addressed = addressed_prompt(hint, phrase) if satellite.focus_capable else None
        proposed = (addressed is not None or interrupt_requested(candidate, phrase)
                    or (satellite.room_runtime and satellite.room_runtime.signaling
                        and call_control(candidate, phrase)))
        stable = stable + 1 if proposed else 0
        stats["max_rms"] = round(max(level, stats["max_rms"]), 6)
        if level >= 0.0006:
            last_voiced = received
        recent_voice = last_voiced > 0 and received - last_voiced < 1.6
        final_wake = ended and addressed == "" and recent_voice and bool(satellite.reply_text)
        # A stable full partial can precede the first question word. Waiting
        # for the stop grammar's final result can lose an entire same-breath turn.
        if final_wake or (addressed == "" and stable >= 3 and recent_voice and satellite.reply_text):
            await satellite.candidate_focus(epoch, serial, received)

        now = time.monotonic()
        attention = satellite.visual_focus_until > now
        if capture and (len(capture.audio) + len(payload) > capture.MAX_BYTES
                        or now - capture.started > 10 or (capture.cued and not attention)
                        or (not attention and now - capture.started > 2)):
            capture = None
            capture_blocked = satellite.visual_focus_until if attention else now + 0.25
        probe = None
        if satellite.question_capable and satellite.reply_text and now >= capture_blocked:
            seed = capture is None
            early = hint == phrase.split()[0] or addressed is not None
            if seed and (attention or early):
                # Separate from both the command grammar and the normal listener.
                factory = getattr(speech, "recognizer", None)
                free = await asyncio.to_thread(factory, speech.vosk, 16000) if factory else None
                capture = QuestionCapture(free, received)
            if capture is not None:
                if seed:
                    following = min(position - confirmed_end, len(ring)) if confirmed_end is not None else 0
                    length = capture.PREROLL + following
                    boundary = await asyncio.to_thread(capture.prime, bytes(ring[-length:]),
                                                       bytes(raw_ring[-length:]), following,
                                                       phrase, satellite.reply_text)
                else:
                    boundary = await asyncio.to_thread(capture.feed, payload, raw, phrase, satellite.reply_text)
                if not capture.anchored and (addressed == "" or attention):
                    capture.anchored = True
                    capture.new_voice = 0
                capture.cued |= attention
                if attention and not (interrupt_requested(candidate, phrase) or call_control(candidate, phrase)):
                    probe = capture.probe(boundary)
        if probe:
            clip, terminal = probe
            checks.offer(OverlapCheck(epoch, serial, received, position, clip, candidate, final_wake,
                                      3 if terminal else 2, True, capture.head, terminal,
                                      satellite.visual_focus_until))
            continue
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
        exact = interrupt_requested(candidate, phrase) or call_control(candidate, phrase)
        eligible = capture and capture.anchored and attention and capture.new_voice >= capture.RATE // 4
        head = capture.head if eligible else ""
        priority = 4 if exact else 1 if ready else 0
        checks.offer(OverlapCheck(epoch, serial, received, position, bytes(ring[-size:]), candidate,
                                  final_wake, priority, head=head,
                                  lease=satellite.visual_focus_until if eligible else 0))
