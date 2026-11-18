"""Offline state, routing, restart, privacy and audio-focus acceptance checks."""

import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from vector_puck.ambient import RATE, SOUNDS, SleepState, loop_pcm
from vector_puck.ambient_runtime import AmbientRuntime
from vector_puck.cookbook import Cookbook
from vector_puck.essentials import Essentials
from vector_puck.kitchen import Kitchen
from vector_puck.notices import Notices
from vector_puck.units import convert


RECIPE = dict(
    title="Skillet pancakes",
    servings=4,
    ingredients=[
        dict(name="flour", quantity=1.5, unit="cups"),
        dict(name="milk", quantity=1, unit="cups"),
        dict(name="salt", quantity=None, unit=""),
    ],
    steps=[
        "Combine the dry ingredients.",
        "Stir in milk just until combined.",
        "Cook small pancakes in a lightly greased skillet until cooked through.",
    ],
    notes="AI draft. Check ingredients against your dietary needs.",
)


class EssentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000000.0
        self.kitchen = Kitchen(self.root / "shopping.sqlite3")
        self.features = Essentials(
            self.root / "kitchen",
            root=self.root / "shared",
            room="kitchen",
            kitchen=self.kitchen,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temp.cleanup()

    def handle(self, name, utterance, **args):
        return self.features.handle(name, args, utterance=utterance)

    def recipe(self, action, utterance, title=None, servings=None, recipe=None):
        return self.handle(
            "cooking_recipe",
            utterance,
            action=action,
            title=title,
            servings=servings,
            recipe=recipe,
        )

    def test_recipe_versions_scaling_shared_book_separate_progress_restart(self):
        self.assertIn("AI recipe draft", self.recipe("save", "save this recipe", recipe=RECIPE))
        self.assertIn(
            "step 1", self.recipe("start", "start cooking skillet pancakes", title=RECIPE["title"])
        )
        self.assertIn("step 2", self.recipe("next", "next step"))
        updated = deepcopy(RECIPE)
        updated["steps"][1] = "Changed instructions for a new version."
        self.recipe("save", "save this recipe", recipe=updated)
        restored = Cookbook(self.root / "shared/cookbook.sqlite3", "kitchen")
        self.assertIn(
            "Stir in milk", restored.step(), "An active cook silently changed recipe versions"
        )
        self.assertIn("not been scaled", restored.scale(8))
        self.assertEqual(restored.ingredients(), ["3 cups flour", "2 cups milk", "to taste salt"])
        other = Cookbook(self.root / "shared/cookbook.sqlite3", "bedroom")
        self.assertIn("skillet pancakes", other.titles())
        self.assertIsNone(other.current())
        self.assertIn("step 1", other.start(RECIPE["title"]))
        self.assertIn("step 2", restored.step())

    def test_recipe_ingredients_reach_existing_shared_list(self):
        self.recipe("save", "create and save a recipe", recipe=RECIPE)
        self.recipe("start", "start cooking pancakes", title=RECIPE["title"])
        self.recipe("scale", "scale this recipe to eight servings", servings=8)
        self.assertIn(
            "Added",
            self.recipe("add_ingredients", "add the recipe ingredients to our shopping list"),
        )
        self.assertEqual(
            [i["text"] for i in self.kitchen.items()],
            ["3 cups flour", "2 cups milk", "to taste salt"],
        )

    def test_bad_recipe_or_cancelled_write_does_not_save(self):
        for patch_value in [
            {"servings": float("nan")},
            {"servings": True},
            {"steps": []},
            {"ingredients": []},
            {"extra": "field"},
        ]:
            with self.assertRaises((ValueError, ArithmeticError)):
                self.features.recipes.save({**RECIPE, **patch_value})

        def cancelled():
            raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            self.features.recipes.save(RECIPE, cancelled)
        self.assertEqual(self.features.recipes.titles(), [])

    def test_question_negation_and_hypothetical_cannot_change_state(self):
        for utterance in [
            "don't play thunderstorm",
            "what happens if I play rain",
            "how do I save this recipe",
            "pretend to start rain",
        ]:
            reply = self.handle(
                "sleep_sound", utterance, action="start", sound="rain", seconds=36000, level=30
            )
            self.assertIn("Nothing was changed", reply)
            self.assertIsNone(self.features.sleep.get())

    def test_sleep_requires_transport_is_bounded_and_keeps_original_deadline(self):
        args = dict(action="start", sound="thunderstorm", seconds=36000, level=30)
        self.assertIn(
            "No sound was started", self.handle("sleep_sound", "play thunderstorm", **args)
        )
        self.features.playback_ready = True
        self.assertIn("10 hours", self.handle("sleep_sound", "play thunderstorm", **args))
        self.now += 90
        restored = SleepState(self.root / "kitchen/sleep.sqlite3", clock=lambda: self.now)
        self.assertEqual(restored.get()["ends"], 1036000)
        restored.change("pause")
        self.assertTrue(restored.get()["paused"])
        restored.change("resume")
        self.assertEqual(restored.get()["ends"], 1036000)
        for seconds in [True, 1, 50000]:
            with self.assertRaises(ValueError):
                restored.start("rain", seconds)
        self.now += 36000
        self.assertIsNone(restored.get())

    def test_fast_paths_do_not_need_a_model_and_do_not_match_negations(self):
        for phrase, tool in [
            ("play thunderstorm for ten hours", "sleep_sound"),
            ("next step", "cooking_recipe"),
            ("convert 350 fahrenheit to celsius", "unit_conversion"),
            ("why are you lit up", "notification_status"),
        ]:
            self.assertEqual(self.features.fast_path(phrase)[0], tool)
        self.assertIsNone(self.features.fast_path("don't play rain"))

    def test_conversions_are_local_exact_and_density_is_not_invented(self):
        self.assertEqual(
            convert(350, "fahrenheit", "celsius")["result"],
            "176.6666666666666666666666666666666666667",
        )
        self.assertEqual(convert(1, "cups", "tablespoons")["result"], "16")
        with self.assertRaises(ValueError):
            convert(1, "cups", "grams")
        self.assertIn(
            "16",
            self.handle(
                "unit_conversion",
                "convert 1 cup to tablespoons",
                amount=1,
                from_unit="cup",
                to_unit="tablespoons",
            ),
        )

    def test_delivery_content_rejected_before_storage_and_status_dedup_expires(self):
        notices = self.features.notices
        event = dict(id="opaque-event-123", status="out_for_delivery", expires=self.now + 3600)
        for extra in [{"product": "surprise"}, {"subject": "private"}, {"raw_email": "private"}]:
            with self.assertRaises(ValueError):
                notices.ingest_delivery({**event, **extra})
        notices.ingest_delivery(event)
        notices.ingest_delivery(event)
        self.assertEqual(len(notices.pending()), 1)
        notices.dismiss()
        notices.ingest_delivery(event)
        self.assertEqual(notices.pending(), [])
        notices.ingest_delivery({**event, "status": "delivered"})
        self.assertEqual(notices.pending(), ["delivered"])
        restored = Notices(self.root / "shared/notices.sqlite3", clock=lambda: self.now)
        self.assertIn("a package was delivered", restored.read())
        self.now += 3600
        self.assertEqual(restored.pending(), [])
        self.assertNotIn(
            "opaque-event",
            (self.root / "shared/notices.sqlite3").read_bytes().decode(errors="ignore"),
        )

    def test_private_database_permissions(self):
        for db in self.root.rglob("*.sqlite3"):
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)


class AmbientAudioTests(unittest.TestCase):
    def test_original_loops_have_stable_level_and_no_boundary_jump(self):
        for sound in SOUNDS:
            pcm = np.frombuffer(loop_pcm(sound), dtype="<i2").astype(float)
            self.assertEqual(len(pcm), RATE * 60)
            self.assertGreater(np.sqrt(np.mean(pcm**2)), 500)
            self.assertLessEqual(max(abs(pcm)), 12000)
            # A periodic buffer must not introduce a uniquely large seam.
            self.assertLess(abs(pcm[-1] - pcm[0]), max(100, np.quantile(abs(np.diff(pcm)), 0.9999)))
            rms = [np.sqrt(np.mean(part**2)) for part in np.array_split(pcm, 60)]
            self.assertGreater(min(rms), 400, "A loop contains a silent/faded section")


class AmbientFocusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = Essentials(Path(self.temp.name))
        self.events = []
        self.s = SimpleNamespace(
            connected=True,
            stop=asyncio.Event(),
            epoch=0,
            followup=SimpleNamespace(active=lambda: False, ready=False, clear=lambda: None),
            room_runtime=None,
            busy=False,
            recording=False,
            speaking=False,
            output_open=False,
            hardware_muted=False,
            activity="idle",
            allowed_base=lambda epoch: not self.s.hardware_muted,
        )

        async def send(kind, data=None, payload=b""):
            if kind == "audio-start":
                self.s.speaking = self.s.output_open = True
            self.events.append((kind, payload))

        self.s.send = send
        self.r = AmbientRuntime(self.f, self.s)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_stream_yields_on_foreground_and_mute_pauses_durable_state(self):
        self.f.sleep.start("rain", 60)
        with patch("vector_puck.ambient_runtime.playback_loop", return_value=bytes(32000)):
            task = asyncio.create_task(self.r.run())
            try:
                await asyncio.sleep(0.35)
                self.assertTrue(any(k == "audio-chunk" for k, _ in self.events))
                self.s.busy = True
                self.r.focus_taken()
                count = len(self.events)
                await asyncio.sleep(0.25)
                self.assertEqual(
                    len(self.events), count, "Stale ambient PCM followed foreground focus"
                )
                self.assertFalse(
                    self.f.sleep.get()["paused"], "A question permanently stopped sleep audio"
                )
                self.r.focus_taken(permanent=True)
                self.assertTrue(self.f.sleep.get()["paused"])
                self.s.hardware_muted = True
                self.s.busy = False
                await asyncio.sleep(0.25)
                self.assertEqual(len(self.events), count)
            finally:
                self.s.stop.set()
                await task

    async def test_muted_or_foreground_speaker_never_starts_audio(self):
        self.f.sleep.start("rain", 60)
        self.s.hardware_muted = True
        task = asyncio.create_task(self.r.run())
        await asyncio.sleep(0.25)
        self.s.stop.set()
        await task
        self.assertEqual(self.events, [])
        self.assertFalse(self.f.playback_ready)


class EssentialsRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_recipe_proposal_is_saved_and_negated_proposal_is_rejected(self):
        import json
        from test_tool_routing import Client, call
        from vector_puck.assistant import answer, request_body
        from vector_puck.config import Config

        with tempfile.TemporaryDirectory() as temp:
            f = Essentials(Path(temp))
            arguments = dict(action="save", title=None, servings=None, recipe=RECIPE)
            client = Client([call("cooking_recipe", json.dumps(arguments))])
            reply = await answer(
                Config(),
                "synthetic",
                "create and save a pancake recipe",
                [],
                lambda: None,
                client,
                essentials=f,
            )
            self.assertIn("Saved", reply)
            self.assertEqual(f.recipes.titles(), ["skillet pancakes"])
            blocked = Client([call("cooking_recipe", json.dumps(arguments))])
            self.assertIn(
                "Nothing was changed",
                await answer(
                    Config(),
                    "synthetic",
                    "don't save a pancake recipe",
                    [],
                    lambda: None,
                    blocked,
                    essentials=f,
                ),
            )
            self.assertEqual(f.recipes.start("skillet pancakes").count("step 1"), 1)
            schema = request_body(Config(), "hello", [], essentials=f)
            self.assertFalse(schema["parallel_tool_calls"])
            self.assertIn("cooking_recipe", {t["name"] for t in schema["tools"]})
            self.assertNotIn(temp, json.dumps(schema))
