"""Update lifecycle, restart, safety priority and fail-closed timeout regression."""
import json
from pathlib import Path
import tempfile
import unittest

from vector_puck.update_indicator import UpdateIndicator, choose_indicator


class UpdateIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = UpdateIndicator(self.root)

    def test_begin_survives_process_restart_and_expires(self):
        ident = self.state.begin(90, now=1000)
        fresh = UpdateIndicator(self.root)
        self.assertEqual(fresh.read(now=1001)['id'], ident)
        self.assertEqual(fresh.indicator(now=1010), {'kind': 'update', 'duration_ms': 80000})
        self.assertEqual(fresh.indicator(now=1090), {'kind': 'none'})
        self.assertEqual(fresh.indicator(now=999), {'kind': 'none'})
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)

    def test_only_matching_active_update_can_complete(self):
        ident = self.state.begin(20, now=1000)
        with self.assertRaises(ValueError):
            self.state.complete('x'*32, now=1001)
        self.assertEqual(self.state.indicator(now=1001)['kind'], 'update')
        self.state.complete(ident, now=1002)
        self.assertEqual(self.state.indicator(now=1003), {'kind': 'updated', 'duration_ms': 7000})
        self.assertEqual(self.state.indicator(now=1010), {'kind': 'none'})
        with self.assertRaises(ValueError):
            self.state.complete(ident, now=1011)

    def test_urgency_suppresses_update(self):
        rgb = {'kind': 'update', 'duration_ms': 5000}
        self.assertEqual(choose_indicator(update=rgb), rgb)
        for weather in ['warning', 'watch', 'advisory']:
            self.assertEqual(choose_indicator(weather=weather, call=True, messages=True, update=rgb), {'kind': weather})
        self.assertEqual(choose_indicator(call=True, update=rgb), {'kind': 'none'})
        self.assertEqual(choose_indicator(alarm=True, update=rgb), {'kind': 'none'})
        self.assertEqual(choose_indicator(messages=True, update=rgb), {'kind': 'message', 'duration_ms': 60000})

    def test_invalid_or_symlink_state_never_lights_ring(self):
        self.state.path.symlink_to(self.root / 'missing')
        self.assertEqual(self.state.indicator(), {'kind': 'none'})
        self.state.path.unlink()
        for value in [[], {'phase': 'updating'}, dict(id='x'*32, phase='updating', started=100, until=float('inf')),
                      dict(id='x'*32, phase='complete', started=100, until=900)]:
            self.state.path.write_text(json.dumps(value))
            self.assertEqual(self.state.indicator(now=101), {'kind': 'none'})
        for seconds in [True, 0, 901, 1.5]:
            with self.assertRaises(ValueError):
                self.state.begin(seconds)
        self.state.clear()
        self.state.clear()


if __name__ == '__main__':
    unittest.main()
