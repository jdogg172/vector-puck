from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from vector_puck.kitchen import Kitchen, parse, reminder_time


def task(action="start", kind="timer", name="pasta", seconds=600, when=None, message=None):
    return dict(action=action, kind=kind, name=name, seconds=seconds, when=when, message=message)


class KitchenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1794844800.0
        self.store = Kitchen(self.root / "kitchen.sqlite3", clock=lambda: self.now)

    def call(self, **kw):
        return self.store.handle("kitchen_task", task(**kw))

    def test_durable_deadlines_and_list_after_reopen(self):
        self.call()
        self.store.handle(
            "shopping_list", dict(action="add", items=["Milk", "2 dozen eggs"], offset=0)
        )
        self.now += 40
        reopened = Kitchen(self.store.path, clock=lambda: self.now)
        self.assertIn(
            "9 minutes and 20 seconds",
            reopened.handle("kitchen_task", task(action="status", seconds=None)),
        )
        self.assertEqual([r["text"] for r in reopened.items()], ["Milk", "2 dozen eggs"])
        self.assertEqual(self.store.path.stat().st_mode & 511, 384)

    def test_multiple_names_ambiguity_extension_and_cancellation(self):
        self.call()
        self.call(name="oven", seconds=1200)
        self.assertIn("already", self.call())
        self.assertIn("Which timer", self.call(action="cancel", name=None, seconds=None))
        self.assertEqual(len(self.store.tasks()), 2)
        self.assertIn("12 minutes", self.call(action="extend", seconds=120))
        self.call(action="cancel", seconds=None)
        self.assertEqual([t["label"] for t in self.store.tasks()], ["oven"])

    def test_invalid_values_have_no_effect(self):
        for change in [
            dict(seconds=True),
            dict(seconds=float("nan")),
            dict(seconds=0),
            dict(seconds=86401),
            dict(name="x\ncommand"),
            dict(when="tomorrow"),
            dict(message="unwanted"),
        ]:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                self.call(**change)
        self.assertEqual(self.store.tasks(), [])
        with self.assertRaises(ValueError):
            self.store.handle("kitchen_task", {**task(), "shell": "reboot"})

    def test_relative_and_absolute_reminder_with_timezone_validation(self):
        self.call(kind="reminder", name="laundry", message="move the laundry", seconds=90)
        self.now += 91
        claimed = self.store.claim_due()
        self.assertEqual(claimed["message"], "move the laundry")
        future = (
            datetime.fromtimestamp(self.now + 7200)
            .astimezone(__import__("vector_puck.kitchen", fromlist=["ZONE"]).ZONE)
            .isoformat()
        )
        self.call(
            kind="reminder", name="bread", message="check the bread", seconds=None, when=future
        )
        for invalid in [
            "2026-11-16T18:00:00",
            "2026-03-08T02:30:00-06:00",
            "2026-07-01T12:00:00-06:00",
        ]:
            with self.assertRaises(ValueError):
                reminder_time(invalid, self.now)

    def test_delivery_lease_and_ack_survive_reopen_and_cancel(self):
        self.call(seconds=1)
        self.now += 2
        claimed = self.store.claim_due()
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.store.claim_due())
        restarted = Kitchen(self.store.path, clock=lambda: self.now)
        self.assertIsNone(restarted.claim_due())
        self.now += 91
        retry = restarted.claim_due()
        self.assertNotEqual(claimed["lease"], retry["lease"])
        self.store.finish_delivery(claimed, True)
        self.assertEqual(len(self.store.tasks()), 1)
        restarted.finish_delivery(retry, False)
        self.now += 6
        final = self.store.claim_due()
        self.store.finish_delivery(final, True)
        self.assertEqual(self.store.tasks(), [])
        self.assertIn("has finished", self.call(action="status", seconds=None))

    def test_shared_list_dedup_checkoff_restore_and_atomic_missing(self):

        def shopping(action, values):
            return self.store.handle("shopping_list", dict(action=action, items=values, offset=0))

        shopping("add", ["Milk", "eggs"])
        shopping("add", ["milk"])
        self.assertEqual(len(self.store.items()), 2)
        shopping("complete", ["milk"])
        self.assertNotIn("milk", shopping("read", []))
        shopping("add", ["milk"])
        self.assertIn("Nothing was changed", shopping("remove", ["milk", "unknown"]))
        self.assertEqual(len(self.store.items()), 2)
        shopping("remove", ["milk"])
        self.assertEqual([r["text"] for r in self.store.items()], ["eggs"])

    def test_concurrent_changes_are_not_lost(self):

        def add(n):
            peer = Kitchen(self.store.path, clock=lambda: self.now)
            peer.handle("shopping_list", dict(action="add", items=[f"item {n}"], offset=0))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(16)))
        self.assertEqual(len(self.store.items()), 16)

    def test_offline_natural_command_examples(self):
        for text, expected in [
            ("set a ten minute pasta timer", ("start", "pasta", 600)),
            ("start a pasta timer for 10 minutes", ("start", "pasta", 600)),
            ("set a timer for five minutes called oven", ("start", "oven", 300)),
            ("give it two more minutes", ("extend", None, 120)),
            ("add two minutes to the pasta timer", ("extend", "pasta", 120)),
            ("how much time is left on the pasta timer", ("status", "pasta", None)),
            ("cancel the pasta timer", ("cancel", "pasta", None)),
        ]:
            with self.subTest(text=text):
                result = parse(text)
                self.assertIsNotNone(result)
                self.assertEqual(
                    tuple((result[1][k] for k in ("action", "name", "seconds"))), expected
                )
        self.assertEqual(parse("remind me in 20 minutes to check the oven")[1]["seconds"], 1200)
        self.assertEqual(
            parse("add milk and eggs to our shopping list")[1]["items"], ["milk", "eggs"]
        )
        for text in [
            "don't start a pasta timer for ten minutes",
            "set thermostat to 75",
            "show the dashboard",
            "give it two dozen minutes",
        ]:
            self.assertIsNone(parse(text))
