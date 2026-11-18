#!/usr/bin/env python3
"""Convert a local field recording to a circular 16 kHz PCM WAV for Vector."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np

RATE = 16000
CROSSFADE = 8 * RATE


def prepare(source: Path, output: Path, start_seconds: int = 0) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Decode once, at install time. The player only needs the prepared WAV.
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        raw = Path(folder) / 'source.pcm'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source.resolve()),
                        '-map', '0:a:0', '-t', '1800', '-ac', '1', '-ar', str(RATE),
                        '-f', 's16le', str(raw)], check=True)
        samples = np.memmap(raw, dtype='<i2', mode='r')
        if len(samples) < RATE * 40:
            raise ValueError('Use at least 40 seconds of recorded ambience')
        # Drop intro/outro fades. Mix the end and beginning at constant power.
        samples = samples[RATE*2:-RATE*2]
        angle = np.linspace(0, np.pi / 2, CROSSFADE, dtype=np.float32)
        seam = samples[-CROSSFADE:] * np.cos(angle) + samples[:CROSSFADE] * np.sin(angle)
        loop = np.concatenate((seam, samples[CROSSFADE:-CROSSFADE]))
        # Keep extended rain audible and contain sharp thunder peaks for sleep.
        rms = float(np.sqrt(np.mean(np.square(loop, dtype=np.float64))))
        if rms < 5:
            raise ValueError('Recording has insufficient audio level')
        gain = min(6, 1800 / rms)
        peak = 10000.0
        pcm = np.rint(peak * np.tanh(loop * (gain / peak))).astype('<i2')
        if not 0 <= start_seconds * RATE < len(pcm):
            raise ValueError('Start position outside the prepared recording')
        # Rotate, preserving every sample around both joins. This lets the
        # first minute include a natural roll without manufacturing extra thunder.
        split = start_seconds * RATE
        dest = Path(folder) / 'thunderstorm.wav'
        with wave.open(str(dest), 'wb') as w:
            w.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            w.writeframesraw(memoryview(pcm[split:]).cast('B'))
            w.writeframes(memoryview(pcm[:split]).cast('B'))
        os.chmod(dest, 0o644)
        os.replace(dest, output)
        print(f'{len(pcm)/RATE:.2f}s loop, 8s crossfade, peak {max(abs(pcm.min()), pcm.max())} / 32768')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--start-seconds', type=int, default=0)
    args = parser.parse_args()
    prepare(args.source, args.output, args.start_seconds)
