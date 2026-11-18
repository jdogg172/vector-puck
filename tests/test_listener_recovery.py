"""Content-free listener watchdog and repeat-wake regressions."""
import unittest
from vector_puck.listener_state import ListenerState


class ProgressTest(unittest.TestCase):
    def test_flowing_audio_does_not_hide_a_missing_pipeline_or_stale_epoch(self):
        for awaiting, epoch in [(True, 0), (False, 1)]:
            now = [10.0]
            state = ListenerState(lambda: now[0])
            state.progress("wake", 0, awaiting)
            self.assertFalse(state.stalled(epoch, idle=True, transport_fresh=True))
            now[0] += 3.1
            self.assertTrue(state.stalled(epoch, idle=True, transport_fresh=True))

    def test_mute_calls_and_real_work_are_not_treated_as_stalled_wake_listeners(self):
        now = [10.0]
        state = ListenerState(lambda: now[0])
        for idle, fresh in [(False, True), (True, False)]:
            state.stalled(1, idle=idle, transport_fresh=fresh)
            now[0] += 60
            self.assertFalse(state.stalled(1, idle=idle, transport_fresh=fresh))

    def test_idle_listener_cannot_remain_gated_by_a_lost_household_turn(self):
        now = [10.0]
        state = ListenerState(lambda: now[0])
        state.progress("wake", 0)
        self.assertFalse(state.stalled(0, idle=True, transport_fresh=True, owns_turn=False))
        now[0] += 3.1
        self.assertTrue(state.stalled(0, idle=True, transport_fresh=True, owns_turn=False))
