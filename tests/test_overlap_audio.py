"""Offline tests. Run directly with --benchmark for synthetic counted-speech results."""

from __future__ import annotations

import argparse
from functools import lru_cache
import json
import shutil
import subprocess
import sys
import time
import unittest

import numpy as np

from vector_puck.overlap_audio import PlaybackReference


MIC = 16_000


def pcm(signal):
    return np.clip(np.rint(signal), -32768, 32767).astype("<i2")


def rms(signal):
    return np.sqrt(np.mean(signal.astype(np.float64) ** 2))


def db_ratio(first, second):
    return float(20 * np.log10(max(rms(first), 1e-9) / max(rms(second), 1e-9)))


def sample(signal, source_rate, rate):
    """Offline independent oracle on a continuous sample timeline."""
    index = np.arange(round(len(signal) * rate / source_rate)) * (source_rate / rate)
    return np.interp(index, np.arange(len(signal)), signal)


def shifted(signal, delay):
    result = np.zeros_like(signal)
    if delay < len(signal):
        result[delay:] = signal[: len(signal) - delay]
    return result


def speech_like(seed, rate=48000, seconds=4):
    """Broadband changing pitch, unvoiced consonants and syllable envelopes."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(rate * seconds)) / rate
    pitch = 110 + seed * 4 + 40 * np.sin(7 * t + seed) + 15 * np.sin(17 * t)
    phase = np.cumsum(pitch) * (2 * np.pi / rate)
    voice = sum(np.sin(i * phase) / i for i in range(1, 14))
    consonant = np.convolve(rng.normal(size=len(t)), np.ones(7) / 7, mode="same")
    envelope = 0.5 + 0.5 * np.sin(t * (21 + seed / 4))
    return pcm(4000 * (envelope * voice + consonant))


@lru_cache(maxsize=1)
def counted_speech():
    """Two local Flite voices. All audio stays in memory, including TTS output."""

    def say(text, voice):
        p = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"flite=text='{text}':voice={voice}",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        return np.frombuffer(p.stdout, dtype="<i2").astype(np.float64)

    far = say(
        "One two three four five six seven eight nine ten. "
        "Eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty.",
        "slt",
    )
    near = say("Hey Vector. What time is it in Tokyo?", "rms")
    far *= 3600 / rms(far)
    near *= 2100 / rms(near)
    talker = np.zeros_like(far)
    start = round(1.3 * 48000)
    talker[start : start + len(near)] = near
    return pcm(far), pcm(talker)


def render(far, near, delay_ms, *, rate=24000, gain=0.55, reflection=0.0, noise=0, drift_ppm=0):
    # Simulated microphone clock and acoustic path are independent of the filter.
    t = np.arange(round(len(far) * MIC / 48000))
    speaker = pcm(sample(far, 48000, rate))
    positions = (t - delay_ms * MIC / 1000) * (rate / MIC) * (1 + drift_ppm / 1e6)
    direct = np.interp(positions, np.arange(len(speaker)), speaker, left=0, right=0)
    echo = gain * direct + reflection * shifted(direct, 233)
    target = pcm(sample(near, 48000, MIC))
    background = np.random.default_rng(51).normal(0, noise, size=len(echo))
    return pcm(echo + target + background), target


def stream(far, mic, *, rate=24000, mic_packet=320, lead=0.1):
    wire = pcm(sample(far, 48000, rate))
    now = 0.0
    api = PlaybackReference(clock=lambda: now)
    api.start()
    cursor = 0
    output = bytearray()
    cost_ms = []
    cpu_s = 0.0
    for at in range(0, len(mic), mic_packet):
        now = at / MIC
        start_cpu, start_wall = time.process_time_ns(), time.perf_counter_ns()
        # Pace 100 ms writes; the native playback buffer can lead the mic.
        while cursor < min(len(wire), (now + lead) * rate):
            end = min(len(wire), cursor + rate // 10)
            api.push_playback(wire[cursor:end].tobytes(), rate=rate)
            cursor = end
        output.extend(api.clean_mic(mic[at : at + mic_packet].tobytes()))
        cost_ms.append((time.perf_counter_ns() - start_wall) / 1e6)
        cpu_s += (time.process_time_ns() - start_cpu) / 1e9
    return np.frombuffer(output, dtype="<i2"), np.array(cost_ms), cpu_s


def count_case(rate=24000, delay_ms=137.4, *, reflection=0.12, drift_ppm=0, voices=None):
    far, near = counted_speech() if voices is None else voices
    mic, target = render(
        far, near, delay_ms, rate=rate, reflection=reflection, noise=22, drift_ppm=drift_ppm
    )
    out, times, cpu = stream(far, mic, rate=rate)
    # Score entire spoken question, including its natural pauses; exclude
    # the easier count-only lead-in and tail from the overlap measurement.
    active = np.flatnonzero(np.abs(target) > 100)
    span = slice(active[0], active[-1] + 1)
    echo_only = np.ones(len(out), dtype=bool)
    echo_only[span] = False
    error_in = mic.astype(float) - target
    error_out = out.astype(float) - target
    stats = dict(
        playback_rate=rate,
        delay_ms=delay_ms,
        reflection_gain=reflection,
        drift_ppm=drift_ppm,
        audio_s=round(len(mic) / MIC, 3),
        count_only_reduction_db=round(db_ratio(error_in[echo_only], error_out[echo_only]), 2),
        question_error_reduction_db=round(db_ratio(error_in[span], error_out[span]), 2),
        question_input_snr_db=round(db_ratio(target[span], error_in[span]), 2),
        question_output_snr_db=round(db_ratio(target[span], error_out[span]), 2),
        question_projection_gain=round(
            float(target[span].astype(float) @ out[span].astype(float))
            / float(np.sum(target[span].astype(float) ** 2)),
            3,
        ),
        wall_ms=round(float(times.sum()), 2),
        cpu_ms_per_audio_second=round(cpu * 1000 / (len(mic) / MIC), 2),
        packet_p95_ms=round(float(np.percentile(times, 95)), 3),
        packet_max_ms=round(float(times.max()), 3),
    )
    return stats, (mic, out, target, span)


class OverlapTests(unittest.TestCase):
    def test_disabled_mute_end_and_rooms(self):
        far = speech_like(1)
        ref = pcm(sample(far[:48000], 48000, 16000)).tobytes()
        a, other = PlaybackReference(), PlaybackReference()
        other.start()
        a.push_playback(ref, rate=16000)
        self.assertIs(a.clean_mic(ref), ref)
        a.start()
        a.push_playback(ref, rate=16000)
        self.assertNotEqual(a.clean_mic(ref), ref)
        self.assertIs(other.clean_mic(ref), ref)
        a.reset()
        a.push_playback(ref, rate=16000)
        self.assertIs(a.clean_mic(ref), ref)
        a.start()
        self.assertIs(a.clean_mic(ref), ref)

    def test_different_rates_delays_polarity_and_packet_sizes(self):
        far = speech_like(1)
        silence = np.zeros_like(far)
        for rate, delay, gain, packet in [
            (8000, 97, 0.6, 160),
            (16000, 0, 0.2, 320),
            (22050, 97.43, -0.7, 512),
            (24000, 412.5, 0.6, 256),
            (44100, 810.12, 0.4, 240),
            (48000, 1771, 0.8, 320),
        ]:
            with self.subTest(rate=rate, delay=delay):
                mic, _ = render(far, silence, delay, rate=rate, gain=gain)
                out, _, _ = stream(far, mic, rate=rate, mic_packet=packet)
                self.assertGreater(db_ratio(mic, out), 6.0)

    def test_independent_talker_silence_and_ambiguous_tone(self):
        far = speech_like(1)
        near = pcm(sample(speech_like(6), 48000, MIC))
        for mic in (near, np.zeros_like(near), np.full_like(near, 250)):
            out, _, _ = stream(far, mic)
            np.testing.assert_array_equal(out, mic)
        tone = pcm(5000 * np.sin(2 * np.pi * np.arange(144000) * 250 / 48000))
        mic = pcm(sample(tone, 48000, MIC))
        out, _, _ = stream(tone, mic, lead=0.3)
        np.testing.assert_array_equal(out, mic)

    def test_simultaneous_talker_and_reacquisition_after_delay_change(self):
        far = speech_like(1)
        near = pcm(speech_like(6) * 0.5)
        near[:48000] = 0
        # Moving the echo path during continuous double talk should cause
        # abstention. A later pause in the near voice permits a fresh lock.
        near[round(2.65 * 48000) : round(3.15 * 48000)] = 0
        mic, target = render(far, near, 233, reflection=0.12, noise=5)
        moved, _ = render(far, near, 411, reflection=0.12, noise=5)
        mic[2 * MIC :] = moved[2 * MIC :]
        out, _, _ = stream(far, mic)
        np.testing.assert_array_equal(
            out[2 * MIC : round(2.65 * MIC)], mic[2 * MIC : round(2.65 * MIC)]
        )
        for segment in (slice(MIC, 2 * MIC), slice(round(3.15 * MIC), 4 * MIC)):
            raw_error = mic[segment].astype(float) - target[segment]
            filtered_error = out[segment].astype(float) - target[segment]
            self.assertGreater(db_ratio(raw_error, filtered_error), 2.0)

    def test_quiet_or_clipped_frame_and_stale_reference(self):
        clock = [0.0]
        api = PlaybackReference(clock=lambda: clock[0])
        far = pcm(sample(speech_like(1), 48000, MIC))[:MIC]
        api.start()
        api.push_playback(far.tobytes(), rate=MIC)
        self.assertNotEqual(api.clean_mic(far[:3200].tobytes()), far[:3200].tobytes())
        for frame in (
            np.zeros(320, dtype="<i2"),
            np.ones(320, dtype="<i2") * 2,
            np.full(320, 32767, dtype="<i2"),
        ):
            self.assertEqual(api.clean_mic(frame.tobytes()), frame.tobytes())
        clock[0] = 3.0
        data = far[:3200].tobytes()
        self.assertIs(api.clean_mic(data), data)
        api.push_playback(data, rate=MIC)
        self.assertIs(api.clean_mic(data), data)

    def test_fragmented_resampling_and_retention(self):
        for rate in (8000, 16000, 22050, 24000, 44100, 48000):
            with self.subTest(rate=rate):
                data = speech_like(1, rate=rate)
                api = PlaybackReference(clock=lambda: 0.0)
                api.start()
                for i in range(0, len(data), 571):
                    api.push_playback(data[i : i + 571].tobytes(), rate=rate)
                expected = sample(data, rate, MIC)
                np.testing.assert_allclose(
                    api._ref, expected[api._end - len(api._ref) : api._end], rtol=0.0, atol=0.51
                )
                self.assertLessEqual(len(api._ref), round(2.5 * MIC))
                self.assertLessEqual(abs(api._end - len(expected)), 2)
                api.clean_mic(data[:1600].tobytes())
                self.assertLess(
                    sum(a.nbytes for a in vars(api).values() if isinstance(a, np.ndarray)), 96000
                )

    def test_bad_format_abstains_and_clears_old_alignment(self):
        good = speech_like(2)[:1600].tobytes()
        a = PlaybackReference()
        for rate, audio in [
            (None, good),
            (True, good),
            (96000, good),
            (24000, b"x"),
            (8000, good * 10),
        ]:
            a.start()
            a.push_playback(good, rate=16000)
            a.push_playback(audio, rate=rate)
            self.assertIs(a.clean_mic(good), good)
        for rate, audio in [(22050, good), (True, good), (16000, b"x")]:
            a.start()
            a.push_playback(good, rate=16000)
            self.assertIs(a.clean_mic(audio, rate=rate), audio)
            self.assertIs(a.clean_mic(good), good)

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "counted-speech fixture requires local FFmpeg with Flite"
    )
    def test_counted_voice_with_different_near_talker(self):
        filters = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        if " flite " not in filters:
            self.skipTest("FFmpeg was built without the optional Flite fixture generator")
        stats, _ = count_case()
        self.assertGreater(stats["count_only_reduction_db"], 4.0, stats)
        self.assertGreater(stats["question_error_reduction_db"], 2.0, stats)
        self.assertTrue(0.9 <= stats["question_projection_gain"] <= 1.1, stats)
        far, near = counted_speech()
        clean = pcm(sample(near, 48000, MIC))
        out, _, _ = stream(far, clean)
        np.testing.assert_array_equal(out, clean)
        # The acoustic echo can also disappear while the writer keeps sending.
        mic, _ = render(far, np.zeros_like(near), 137.4)
        cut = 2 * MIC
        mic[cut:] = clean[cut:]
        out, _, _ = stream(far, mic)
        np.testing.assert_array_equal(out[cut:], clean[cut:])

    def test_rate_change_and_long_gap_discard_old_reference(self):
        now = [0.0]
        api = PlaybackReference(clock=lambda: now[0])
        data = speech_like(1)[:8000].tobytes()
        api.start()
        api.push_playback(data, rate=16000)
        self.assertNotEqual(api.clean_mic(data), data)
        api.push_playback(bytes(2400), rate=24000)
        self.assertEqual(api.clean_mic(data), data)
        now[0] = 3.1
        api.push_playback(bytes(2400), rate=24000)
        self.assertLess(len(api._ref), 1000)
        self.assertEqual(api.clean_mic(data), data)


def recognize_case(model, audio):
    """Optional read-only offline Vosk check on the known question interval."""
    from vosk import KaldiRecognizer

    mic, clean, near, span = audio
    clip = slice(max(0, span.start - 2000), span.stop + 2000)
    results = {}
    for label, signal in (("near_only", near), ("mixture", mic), ("subtracted", clean)):
        recognizer = KaldiRecognizer(model, MIC)
        recognizer.AcceptWaveform(signal[clip].tobytes())
        text = json.loads(recognizer.FinalResult())["text"]
        # This constrained decoder is only a local wake candidate check.
        wake = KaldiRecognizer(model, MIC, json.dumps(["hey vector", "hey victor", "[unk]"]))
        hits = set()
        chunk = signal[clip]
        for start in range(0, len(chunk), 320):
            ended = wake.AcceptWaveform(chunk[start : start + 320].tobytes())
            heard = json.loads(wake.Result() if ended else wake.PartialResult())
            hits.add(heard.get("text", heard.get("partial", "")))
        hits.add(json.loads(wake.FinalResult())["text"])
        results[label] = dict(
            text=text, wake_candidate=any("hey vector" in h or "hey victor" in h for h in hits)
        )
    return results


def benchmark(model=None, voices=None):
    for rate, delay, reflection, drift in [
        (16000, 39, 0.0, 0),
        (24000, 137.4, 0.12, 0),
        (44100, 621.5, 0.12, 150),
        (48000, 385, 0.38, -250),
    ]:
        stats, audio = count_case(
            rate, delay, reflection=reflection, drift_ppm=drift, voices=voices
        )
        if model is not None:
            stats["vosk"] = recognize_case(model, audio)
        print(json.dumps(stats), flush=True)


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        parser = argparse.ArgumentParser(description="Local Flite counted-speech benchmark")
        parser.add_argument("--benchmark", action="store_true")
        parser.add_argument(
            "--vosk-model", help="Optional installed model directory for offline A/B transcripts"
        )
        args = parser.parse_args()
        model = None
        if args.vosk_model:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)
            model = Model(args.vosk_model)
        benchmark(model)
    else:
        unittest.main()
