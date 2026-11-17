from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from vector_puck.alarms import Alarms, next_due as base_next_due, parse


from zoneinfo import ZoneInfo


def next_due(*args):
    return base_next_due(*args, zone=ZoneInfo("America/Chicago"))


class AlarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "alarm.db"
        self.now = [datetime.fromisoformat("2026-11-17T06:59:00-06:00").timestamp()]
        self.a = Alarms(self.path, clock=lambda: self.now[0], timezone="America/Chicago")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, action, **kw):
        return {**dict(action=action, name=None, at=None, days=[], date=None, minutes=None), **kw}

    def test_school_alarm_repeats_and_snoozes_more_than_once(self):
        self.a.handle(self.args("set", name="school", at="07:00", days=[0, 1, 2, 3, 4]))
        self.now[0] += 61
        self.assertEqual(len(self.a.tick()), 1)
        for _ in range(3):
            self.assertIn("9 minutes", self.a.handle(self.args("snooze")))
            self.assertEqual(self.a.ringing(), [])
            self.now[0] += 541
            self.assertEqual(len(self.a.tick()), 1)
        self.a.handle(self.args("dismiss"))
        self.assertEqual(self.a.ringing(), [])
        self.assertIn("Wednesday", self.a.handle(self.args("status")))

    def test_one_off_fishing_alarm_and_ringing_state_survive_restart(self):
        self.a.handle(self.args("set", name="fishing", at="07:00", date="2026-11-17"))
        self.now[0] += 61
        self.a.tick()
        other = Alarms(self.path, clock=lambda: self.now[0], timezone="America/Chicago")
        self.assertEqual(other.ringing()[0]["name"], "fishing")
        other.dismiss_ringing()
        self.assertEqual(self.a.ringing(), [])
        self.assertIn("no enabled", self.a.handle(self.args("status")))

    def test_cancel_does_not_cancel_ambiguous_multiple_alarms(self):
        for name, at in [("school", "07:00"), ("fishing", "08:00")]:
            self.a.handle(self.args("set", name=name, at=at))
        self.assertIn("Which alarm", self.a.handle(self.args("cancel")))
        self.assertIn("Cancelled", self.a.handle(self.args("cancel", name="school")))
        self.assertIn("fishing", self.a.handle(self.args("status")))

    def test_cancel_guard_prevents_new_alarm(self):
        def guard():
            raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            self.a.handle(self.args("set", at="07:00"), guard)
        self.assertIn("no enabled", self.a.handle(self.args("status")))

    def test_bad_times_dates_and_snooze_rejected(self):
        for at in ["7", "7am", "25:00", "07:99", None]:
            with self.assertRaises((ValueError, TypeError)):
                next_due(at, [], None, self.now[0])
        with self.assertRaises(ValueError):
            next_due("07:00", [True], None, self.now[0])
        with self.assertRaises(ValueError):
            next_due("07:00", [1], "2026-11-17", self.now[0])

    def test_spring_forward_nonexistent_time_rejected(self):
        now = datetime.fromisoformat("2027-03-13T12:00:00-06:00").timestamp()
        with self.assertRaises(ValueError):
            next_due("02:30", [], "2027-03-14", now)

    def test_snooze_and_off_fast_paths(self):
        self.assertEqual(parse("Hey Vector snooze")["action"], "snooze")
        self.assertEqual(parse("snooze for 5 minutes")["minutes"], 5)
        self.assertIsNone(parse("off", False))
        self.assertEqual(parse("off", True)["action"], "dismiss")

    def test_relative_alarm_preserves_exact_delay_after_restart_and_snooze(self):
        self.now[0] += 37
        reply = self.a.handle(self.args("set", name="test", minutes=2))
        self.assertIn("in 2 minutes", reply)
        self.now[0] += 119
        again = Alarms(self.path, clock=lambda: self.now[0], timezone="America/Chicago")
        self.assertEqual(again.tick(), [])
        self.now[0] += 1
        self.assertEqual(len(again.tick()), 1)
        self.assertIn("1 minutes", again.handle(self.args("snooze", minutes=1)))
        self.now[0] += 60
        self.assertEqual(len(again.tick()), 1)
        again.handle(self.args("dismiss"))
        self.assertEqual(again.ringing(), [])

    def test_relative_alarm_can_cross_midnight(self):
        self.now[0] = datetime.fromisoformat("2026-11-17T23:59:30-06:00").timestamp()
        self.a.handle(self.args("set", minutes=2))
        self.now[0] += 120
        row = self.a.tick()[0]
        self.assertEqual(row["on_date"], "2026-11-18")
        self.assertEqual(row["at"], "00:01")

    def test_relative_alarm_cannot_mix_schedules_or_invalid_delays(self):
        for values in [
            dict(minutes=2, at="07:00"),
            dict(minutes=2, days=[0]),
            dict(minutes=2, date="2026-11-18"),
            dict(minutes=True),
            dict(minutes=0),
            dict(minutes=1441),
        ]:
            with self.assertRaises(ValueError):
                self.a.handle(self.args("set", **values))
        self.assertIn("no enabled", self.a.handle(self.args("status")))

    def test_old_native_dismissal_cannot_silence_next_occurrence(self):
        self.a.handle(self.args("set", name="school", at="07:00", days=[0, 1, 2, 3, 4]))
        self.now[0] += 61
        first = self.a.tick()[0]["id"]
        self.a.dismiss_ringing(first)
        self.now[0] += 86400
        second = self.a.tick()[0]["id"]
        self.assertNotEqual(first, second)
        self.a.dismiss_ringing(first)
        self.assertEqual(self.a.ringing()[0]["id"], second)
        self.a.handle(self.args("snooze", minutes=1))
        self.now[0] += 61
        third = self.a.tick()[0]["id"]
        self.assertNotEqual(second, third)
        self.a.dismiss_ringing(second)
        self.assertEqual(self.a.ringing()[0]["id"], third)
        self.a.handle(self.args("set", name="school", minutes=2))
        self.now[0] += 121
        fourth = self.a.tick()[0]["id"]
        self.assertNotEqual(third, fourth)
        self.a.dismiss_ringing(third)
        self.assertEqual(self.a.ringing()[0]["id"], fourth)


if __name__ == "__main__":
    unittest.main()
