import unittest
from vector_puck.game_guide import game_turn, official_source, opening, research_request


class GuideTest(unittest.TestCase):
    def test_guides_and_controls(self):
        self.assertIn("Pokédex online", opening("Open the Pokédex!"))
        self.assertIn("Hyrule", opening("Open the Hyrule field guide"))
        self.assertIsNone(game_turn("Set an alarm called Pokémon", []))
        self.assertIsNone(game_turn("Is it still available?", []))
        prior = [("user","Why is the TV off?"),("assistant","Private reply"),
                 ("user","Open the Pokédex"),("assistant","Pokédex online.")]
        context = game_turn("Is it still available?", prior)
        self.assertEqual(context, prior[-2:])
        body = research_request("test", "Is it still available?", context, "avoid", "Speak naturally.")
        self.assertEqual(body["tool_choice"], "required")
        self.assertNotIn("Private reply", str(body))
        self.assertIn("spoiler-free", body["instructions"])
        self.assertTrue(official_source("https://support.pokemon.com/distributions"))
        self.assertFalse(official_source("https://pokemon.com.another.invalid/"))
