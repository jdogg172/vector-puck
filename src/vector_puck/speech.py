"""Only local Vosk wake/endpoint and offline Whisper ASR; no network fallback."""
from __future__ import annotations

import json
import re
import threading


def question(text: str, wake_phrase: str) -> str | None:
    match = re.match(r"^\s*" + r"[ ,.!?]+".join(map(re.escape, wake_phrase.split()))
                     + r"\b[ ,.!?]*(.*)$", text, re.I | re.S)
    return match[1].strip() if match else None


def done(text):
    return " ".join(re.findall(r"[a-z]+", text.lower())) in {
        "stop", "cancel", "thanks", "thank you", "that s all", "never mind",
        "end conversation", "stop listening", "clear conversation", "forget conversation",
    }


class Speech:
    def __init__(self, config):
        import numpy as np
        from faster_whisper import WhisperModel
        from vosk import KaldiRecognizer, Model, SetLogLevel
        self.np, self.recognizer = np, KaldiRecognizer
        SetLogLevel(-1)
        self.vosk = Model(config.vosk_model)
        self.whisper = WhisperModel(config.whisper_model, device="cpu", compute_type="int8",
                                    cpu_threads=config.cpu_threads, num_workers=1,
                                    local_files_only=True)
        self.config = config
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.wake = self.recognizer(self.vosk, 16000, json.dumps([self.config.wake_phrase, "[unk]"]))
        self.endpoint = self.recognizer(self.vosk, 16000)
        self.stable = 0
        self.endpoint_text = ""

    def wake_candidate(self, pcm):
        ended = self.wake.AcceptWaveform(pcm)
        words = json.loads(self.wake.Result() if ended else self.wake.PartialResult())
        words = words.get("text" if ended else "partial", "")
        self.stable = self.stable + 1 if self.config.wake_phrase in words else 0
        return self.stable >= 2

    def endpoint_ready(self, pcm, seconds):
        ended = self.endpoint.AcceptWaveform(pcm)
        if ended:
            self.endpoint_text = (self.endpoint_text + " " + json.loads(
                self.endpoint.Result()).get("text", ""))[-1000:]
        return endpoint_ready(self.endpoint_text, seconds, ended, self.config.wake_phrase)

    def speech_present(self, pcm):
        ended = self.endpoint.AcceptWaveform(pcm)
        result = json.loads(self.endpoint.Result() if ended else self.endpoint.PartialResult())
        samples = self.np.frombuffer(pcm, dtype="<i2").astype(self.np.float32)
        level = float(self.np.sqrt(self.np.mean(samples ** 2)))
        return level >= 20 and bool(result.get("text" if ended else "partial", "").strip())

    def transcribe(self, pcm):
        if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 34:
            raise ValueError("Invalid local ASR input")
        with self.lock:
            audio = self.np.frombuffer(pcm, dtype="<i2").astype(self.np.float32) / 32768
            segments, _ = self.whisper.transcribe(audio, language="en", beam_size=3,
                                                 condition_on_previous_text=False, vad_filter=False)
            return " ".join(segment.text.strip() for segment in segments).strip()[:2000]


def endpoint_ready(words: str, seconds: float, ended: bool, wake_phrase: str) -> bool:
    """A pause after the wake sound must not terminate a wake-only request."""
    if seconds >= 30:
        return True
    if seconds < 1.2 or not ended:
        return False
    ignored = set(wake_phrase.split()) | {"unk"}
    return any(word not in ignored for word in re.findall(r"[a-z]+", words.lower()))
