"""Optional local echo subtraction for recognition, one instance per room.

Call start for each Cedar playback, push only PCM actually sent, and clean each
fresh, chronological microphone packet once. Feed the returned copy only to
local recognition. Call reset on mute, end, cancellation, a stream gap, or
disconnect; start again explicitly. Serialize calls in the room's audio loop.
Retains at most 2.5 s of sent playback and 96 ms of mic context. The native
echo path may already suppress playback; if no stable linear match survives,
leave that mic signal alone. Keep the resident's mute/epoch checks on results.
No I/O, learned voice profiles, persistent state, or capture-device access.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np


_RATE = 16_000
_HISTORY = 40_000  # 2.5 seconds of playback on the microphone sample clock
_WINDOW = 1_536  # 96 ms of microphone context, including the current frame
_MIN_MATCH = 960  # never acquire on a lone short vowel or click
_STEP = 8  # search at 2 kHz, then refine at the 16 kHz microphone rate
_FRAME = 320  # gate each 20 ms independently, including changes to double talk
_EMPTY = np.empty(0, dtype=np.float32)
_EMPTY_REF = np.empty(0, dtype="<i2")


def _fit(mic: np.ndarray, ref: np.ndarray) -> tuple[float, float]:
    """Zero-mean correlation and signed least-squares echo gain."""
    x = ref - ref.mean()
    y = mic - mic.mean()
    xx, yy = float(x @ x), float(y @ y)
    if xx < len(x) * 16**2 or yy < len(y) * 10**2:
        return 0.0, 0.0
    xy = float(x @ y)
    return xy / (xx * yy) ** 0.5, xy / xx


class PlaybackReference:
    """Inert until started. Input/output PCM is mono signed little-endian 16-bit."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        """Forget all audio, timing and gain; subsequent packets pass through."""
        self._active = False
        self._ref, self._mic = _EMPTY_REF, _EMPTY
        self._end = self._input_rate = self._phase = 0
        self._previous = None
        self._cursor = None
        self._gain = 0.0
        self._confirmed = self._missed = 0
        self._last_push = self._last_mic = None
        self._search_in = 0

    def start(self) -> None:
        self.reset()
        self._active = True

    def push_playback(self, pcm: bytes, *, rate: int) -> None:
        """Append one sent speaker packet (8–48 kHz). Invalid streams disarm."""
        if not self._active:
            return
        if (
            type(rate) is not int
            or not 8000 <= rate <= 48000
            or not isinstance(pcm, bytes)
            or len(pcm) % 2
            or len(pcm) > rate * 2
        ):
            self.reset()
            return
        if not pcm:
            return
        now = self._clock()
        if (self._input_rate and self._input_rate != rate) or (
            self._last_push is not None and now - self._last_push >= 2.5
        ):
            self.start()  # no matching across a sample clock change or playback gap
        self._input_rate, self._last_push = rate, now
        src = np.frombuffer(pcm, dtype="<i2")
        if rate != _RATE:
            # Linear interpolation with integer phase across arbitrary packet cuts.
            # Keep one source sample for interpolation across the next boundary.
            src = src.astype(np.float32)
            if self._previous is not None:
                src = np.concatenate(([self._previous], src), dtype=np.float32)
            boundary = (len(src) - 1) * _RATE
            positions = np.arange(self._phase, boundary, rate, dtype=np.int64)
            self._phase += len(positions) * rate - boundary
            self._previous = src[-1]
            idx, frac = np.divmod(positions, _RATE)
            src = np.rint(
                src[idx] + (src[idx + 1] - src[idx]) * (frac.astype(np.float32) / _RATE)
            ).astype("<i2")
        if not len(src):
            return
        self._end += len(src)
        self._ref = np.concatenate((self._ref[max(0, len(self._ref) + len(src) - _HISTORY) :], src))

    def _acquire(self, mic: np.ndarray) -> int | None:
        n = len(mic) // _STEP
        y = mic[: n * _STEP].reshape(n, _STEP).mean(axis=1)
        y -= y.mean()
        energy = float(y @ y)
        if energy < n * 10**2:
            return None
        coarse = (
            self._ref[: len(self._ref) // _STEP * _STEP]
            .reshape(-1, _STEP)
            .mean(axis=1, dtype=np.float32)
        )
        # Search scratch only. Float64 sums avoid cancellation in quiet windows.
        power = np.empty((2, len(coarse) + 1), dtype=np.float64)
        power[:, 0] = 0
        np.cumsum(coarse, dtype=np.float64, out=power[0, 1:])
        np.cumsum(coarse * coarse, dtype=np.float64, out=power[1, 1:])
        sums, squares = power[:, n:] - power[:, :-n]
        variance = squares - sums * sums / n
        dots = np.correlate(coarse, y)
        scores = np.abs(dots) / np.sqrt(energy * np.maximum(variance, n * 16**2))
        peak = int(scores.argmax())
        best = scores[peak]
        if best < 0.68 or len(scores) < 16:
            return None
        # Repeated syllables and pure tones may fit several unrelated offsets.
        scores[max(0, peak - 6) : peak + 7] = 0
        if best - scores.max() < 0.02:
            return None
        return self._refine(mic, peak * _STEP, _STEP)

    def _refine(self, mic: np.ndarray, start: int, radius: int) -> int | None:
        lo, hi = max(0, start - radius), min(len(self._ref) - len(mic), start + radius)
        if hi < lo:
            return None
        # The small refinement band has nearly constant window energy. Check
        # the winning offset with a full normalized fit before subtracting.
        dots = np.correlate(self._ref[lo : hi + len(mic)], mic - mic.mean())
        return lo + int(np.argmax(np.abs(dots)))

    def _frame(self, pcm: bytes) -> bytes:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        n = len(samples)
        context = np.concatenate((self._mic[-(_WINDOW - n) :], samples))
        self._mic = context
        self._search_in = max(0, self._search_in - n)
        if len(context) < _MIN_MATCH or len(self._ref) < len(context):
            return pcm
        origin = self._end - len(self._ref)
        offset = None
        tracking = self._cursor is not None
        if tracking:
            offset = self._refine(context, self._cursor - origin - (len(context) - n), 3)
        elif self._search_in <= 0:
            self._search_in = _RATE // 20  # at most 20 broad searches per second
            offset = self._acquire(context)
        if offset is None:
            self._cursor = None
            return pcm
        ref = self._ref[offset : offset + len(context)].astype(np.float32)
        corr, gain = _fit(context, ref)
        if not tracking:
            if abs(corr) < 0.82 or not 0.025 <= abs(gain) <= 2:
                return pcm
            self._cursor = origin + offset + len(context)
            self._gain = gain
            # Require another 60 ms of matching audio before using this lag.
            self._confirmed = self._missed = 0
            return pcm
        locked = self._confirmed >= _MIN_MATCH
        if (
            corr * np.sign(self._gain) < (0.53 if locked else 0.78)
            or not 0.5 <= gain / self._gain <= 1.8
        ):
            self._missed += n
            # Coast on the sample clock during silence or strong double talk.
            # No gain adaptation, lag correction or subtraction on these frames.
            self._cursor = self._cursor + n if locked and self._missed <= _RATE // 2 else None
            return pcm
        self._cursor = origin + offset + len(context)
        self._missed = 0
        self._confirmed = min(_MIN_MATCH, self._confirmed + n)
        if self._confirmed < _MIN_MATCH:
            return pcm
        if abs(corr) >= 0.85:
            self._gain += 0.15 * (gain - self._gain)
        local_corr, local_gain = _fit(samples, ref[-n:])
        if local_corr * np.sign(self._gain) < 0.57 or np.max(np.abs(samples)) >= 32760:
            return pcm
        # Subtract only the aligned waveform. Cap its gain with both the
        # longer match and this frame; never gate/mute the entire mic signal.
        scale = np.copysign(min(abs(self._gain), abs(local_gain)), self._gain) * 0.92
        echo = ref[-n:]
        result = samples - scale * (echo - echo.mean())
        return np.clip(np.rint(result), -32768, 32767).astype("<i2").tobytes()

    def clean_mic(self, pcm: bytes, *, rate: int = _RATE) -> bytes:
        """Return recognition-only PCM, unchanged when unsupported or uncertain.

        Takes newly arrived packets (up to one second), never overlapping ASR
        windows. History is bounded independently of session length.
        """
        if not self._active or self._last_push is None:
            return pcm
        now = self._clock()
        if now - self._last_push >= 2.5:
            self.reset()
            return pcm
        if (
            type(rate) is not int
            or rate != _RATE
            or not isinstance(pcm, bytes)
            or len(pcm) % 2
            or len(pcm) > 2 * _RATE
        ):
            self.reset()
            return pcm
        if not pcm:
            return pcm
        if self._last_mic is not None and now - self._last_mic > 0.25:
            self._mic, self._cursor, self._search_in = _EMPTY, None, 0
        self._last_mic = now
        if len(pcm) <= 2 * _FRAME:
            return self._frame(pcm)
        return b"".join(
            self._frame(pcm[i : i + 2 * _FRAME]) for i in range(0, len(pcm), 2 * _FRAME)
        )
