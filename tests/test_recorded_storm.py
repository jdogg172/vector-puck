import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from vector_puck import ambient
from vector_puck.essentials import Essentials


class RecordedStormTests(unittest.TestCase):
    def test_recording_selected_and_every_sample_survives_multiple_packet_wraps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'thunderstorm.wav'
            # Intentionally NOT a multiple of the 100 ms packet length.
            pcm = np.arange(ambient.RATE + 17, dtype='<i2').tobytes()
            with wave.open(str(path), 'wb') as handle:
                handle.setparams((1, 2, ambient.RATE, 0, 'NONE', 'not compressed'))
                handle.writeframes(pcm)
            with patch.object(ambient, 'loop_pcm', side_effect=AssertionError('Unexpected fallback')):
                clip = ambient.playback_loop('thunderstorm', recording=path)
            offset = 0
            packets = []
            size = ambient.RATE // 10 * 2
            for _ in range(33):
                chunk, offset = ambient.loop_packet(clip, offset, size)
                self.assertEqual(len(chunk), size)
                packets.append(chunk)
            self.assertEqual(b''.join(packets), (pcm * 4)[:size * 33])

    def test_missing_or_invalid_storm_preserves_other_sleep_options(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ambient, 'loop_pcm', return_value=b'fallback') as synth:
            path = Path(tmp) / 'storm.wav'
            self.assertEqual(ambient.playback_loop('thunderstorm', recording=path), b'fallback')
            path.write_bytes(b'truncated')
            with self.assertLogs(ambient.__name__, level='WARNING'):
                self.assertEqual(ambient.playback_loop('thunderstorm', recording=path), b'fallback')
            self.assertEqual(ambient.playback_loop('rain', recording=path), b'fallback')
            self.assertEqual([args.args[0] for args in synth.call_args_list], ['thunderstorm', 'thunderstorm', 'rain'])

    def test_family_phrasing_preserves_requested_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            features = Essentials(Path(tmp))
            for utterance, seconds in [('play storm sounds', 36000), ('play thunderstorms for one minute', 60),
                                       ('play storm sounds for ten hours', 36000)]:
                tool, args = features.fast_path(utterance)
                self.assertEqual((tool, args['sound'], args['seconds']), ('sleep_sound', 'thunderstorm', seconds))
            self.assertIsNone(features.fast_path('do not play storm sounds'))
