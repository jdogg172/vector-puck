"""Original quiet plucked working tune; no samples, recordings, or paid generation."""
from array import array
import math
import sys

RATE = 24000


def working_audio() -> bytes:
    notes = (392.0, 493.883, 440.0, 329.628, 523.251, 392.0, 293.665, 440.0)
    result = array("h")
    for hz in notes:
        for i in range(round(RATE * 0.4)):
            t = i / RATE
            envelope = min(t / 0.012, 1.0) * math.exp(-t * 11)
            envelope *= max(0, min((0.3 - t) / 0.04, 1))
            tone = math.sin(2 * math.pi * hz * t) + 0.22 * math.sin(4 * math.pi * hz * t)
            result.append(round(1800 * tone * envelope))
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()
