"""Original local ambient loops and durable sleep deadlines; no cloud audio."""

from __future__ import annotations

from contextlib import closing
import json
import math
import logging
import os
from pathlib import Path
import sqlite3
import time
import wave

SOUNDS = ("rain", "thunderstorm", "white noise", "brown noise")
RATE = 16000
LOOP_SECONDS = 60


def loop_pcm(sound):
    """Periodic filtered noise, deterministic per sound, with no crossfade hole.

    The spectral construction is periodic: wrapping the buffer is one ordinary
    adjacent sample transition. Thunder is low rumble, not loud crack effects.
    """
    if sound not in SOUNDS:
        raise ValueError("Unsupported ambient sound")
    import numpy as np

    rng = np.random.default_rng(SOUNDS.index(sound) + 712031)
    count = RATE * LOOP_SECONDS
    frequency = np.fft.rfftfreq(count, 1 / RATE)
    phases = rng.uniform(0, 2 * math.pi, len(frequency))
    slope = {"white noise": 0.0, "brown noise": 1.0, "rain": 0.45, "thunderstorm": 0.5}[sound]
    spectrum = np.exp(1j * phases) / np.maximum(frequency, 40) ** slope
    spectrum[0] = 0
    spectrum[-1] = 0
    signal = np.fft.irfft(spectrum, n=count)
    signal /= max(float(np.sqrt(np.mean(signal**2))), 1e-10)
    if sound == "thunderstorm":
        t = np.arange(count) / RATE
        rumble = np.zeros(count)
        for centre in (14, 43):
            distance = np.minimum(abs(t - centre), LOOP_SECONDS - abs(t - centre))
            envelope = np.exp(-(distance**2) / 18)
            rumble += envelope * (np.sin(2 * math.pi * 47 * t) + 0.5 * np.sin(2 * math.pi * 71 * t))
        signal = 0.8 * signal + rumble
    # Fixed, conservative digital level; no device/global volume writes.
    signal *= 0.09 / max(1.0, float(np.max(abs(signal))) / 3.0)
    return np.clip(signal * 32767, -12000, 12000).astype("<i2").tobytes()


RECORDING = Path("/models/ambient/thunderstorm.wav")


def playback_loop(sound, *, recording=RECORDING):
    """Load the installed storm once per resident, or use its offline fallback."""
    if sound == "thunderstorm":
        try:
            with wave.open(str(recording), "rb") as wav:
                frames = wav.getnframes()
                if (wav.getparams()[:3] != (1, 2, RATE) or wav.getcomptype() != "NONE"
                        or not RATE <= frames <= RATE * 1800):
                    raise ValueError("Unsupported storm WAV format or duration")
                data = wav.readframes(frames)
                if len(data) != frames * 2:
                    raise ValueError("Truncated storm WAV")
                return data
        except FileNotFoundError:
            pass  # Standalone installs can use the original synthesized loop.
        except (wave.Error, EOFError, ValueError, OSError) as exc:
            logging.getLogger(__name__).warning("Local storm unavailable (%s)", type(exc).__name__)
    return loop_pcm(sound)


def loop_packet(clip, offset, size):
    """Fill one packet even when a recording ends in the middle of it."""
    end = offset + size
    packet = clip[offset:end]
    if end > len(clip):
        packet += clip[:end - len(clip)]
    return packet, end % len(clip)


class SleepState:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sleep (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT)"
            )

    def get(self):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT value FROM sleep WHERE id=1").fetchone()
        value = None if row is None else json.loads(row[0])
        return value if value and value["ends"] > self.clock() else None

    def _write(self, value, guard):
        with closing(sqlite3.connect(self.path)) as db, db:
            guard()
            db.execute("INSERT OR REPLACE INTO sleep VALUES(1,?)", (json.dumps(value),))

    def start(self, sound, seconds=36000, level=30, guard=lambda: None):
        if sound not in SOUNDS or type(seconds) is not int or not 60 <= seconds <= 43200:
            raise ValueError("Use a supported sound for one minute to twelve hours")
        if type(level) is not int or not 5 <= level <= 60:
            raise ValueError("Sleep level outside bounds")
        self._write(
            dict(sound=sound, ends=self.clock() + seconds, level=level, paused=False), guard
        )

    def change(self, action, guard=lambda: None):
        if action not in {"stop", "pause", "resume"}:
            raise ValueError("Unsupported sleep control")
        value = self.get()
        if action == "stop":
            self._write(None, guard)
        elif value:
            value["paused"] = action == "pause"
            self._write(value, guard)
        return value
