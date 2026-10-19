"""Tests for mcp_server/dmdb.py. Run: python -m unittest discover tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
import dmdb  # noqa: E402
from dmdb import CardDB, DMError  # noqa: E402
from server import Server  # noqa: E402


def deck(*pairs):
    return [{"card": c, "count": n} for c, n in pairs]


class DMTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = CardDB(decks_dir=cls.tmp.name)
        # A legal 40-card mono-fire list built from real cards: 10 distinct names x 4, no Shield Triggers.
        pool = cls.db.search(civilizations=["Fire"], civ_match="exact", card_types=["Creature"],
                             cost_max=4, limit=100)["cards"]
        names = [c["name"] for c in pool if "shield_trigger" not in c.get("keywords", [])][:10]
        cls.legal = deck(*[(n, 4) for n in names])

    @classmethod
    def tearDownClass(cls):
        cls.db.db.close()
        cls.tmp.cleanup()


class TestLookup(DMTestCase):
    def test_resolve_by_id_name_and_short_id(self):
        self.assertEqual(self.db.resolve("0079")["name"], "Bolshack Dragon")
        self.assertEqual(self.db.resolve("79")["name"], "Bolshack Dragon")
        self.assertEqual(self.db.resolve("bolshack dragon")["id"], "0079")

    def test_unknown_card_suggests(self):
        with self.assertRaises(DMError) as cm:
            self.db.resolve("Bolshak Dragon")
        self.assertIn("Bolshack Dragon", str(cm.exception))

    def test_reprints_resolve_to_first_printing(self):
        self.assertEqual(self.db.resolve("Death Smoke")["id"], "0063")


class TestSearch(DMTestCase):
    def test_filters_combine(self):
        r = self.db.search(civilizations=["Fire"], keywords=["speed_attack"], cost_max=3, limit=100)
        self.assertGreater(r["total_matches"], 0)
        for c in r["cards"]:
            self.assertIn("Fire", c["civilizations"])
            self.assertIn("speed_attack", c["keywords"])
            self.assertLessEqual(c["cost"], 3)

    def test_exact_civ_match_is_mono(self):
        for c in self.db.search(civilizations=["Water"], civ_match="exact", limit=100)["cards"]:
            self.assertEqual(c["civilizations"], ["Water"])

    def test_race_partial_match(self):
        r = self.db.search(races=["Dragon"], limit=100)
        self.assertTrue(all(any("dragon" in x.lower() for x in c["races"]) for c in r["cards"]))

    def test_reprints_hidden_by_default(self):
        names = [c["name"] for c in self.db.search(query="Death Smoke")["cards"]]
        self.assertEqual(names, ["Death Smoke"])
        self.assertEqual(self.db.search(query="Death Smoke", include_reprints=True)["total_matches"], 3)

    def test_reminder_text_stripped(self):
        c = self.db.search(query="Bolshack Dragon")["cards"][0]
        self.assertNotIn("(This creature breaks", c["text"])

    def test_bad_codes_rejected(self):
        with self.assertRaises(DMError):
            self.db.search(keywords=["nope"])
        with self.assertRaises(DMError):
            self.db.search(civilizations=["Purple"])


class TestEvolution(DMTestCase):
    def test_all_evolutions_parse_and_have_bases(self):
        evos = [c for c in self.db.unique_cards() if c["evolution"]]
        self.assertGreater(len(evos), 250)
        for c in evos:
            self.assertFalse(c["evolution"].get("unparsed"), c["name"])
            self.assertTrue(any(dmdb.is_valid_base(b, c["evolution"])
                                for b in self.db.unique_cards() if b is not c), c["name"])

    def test_specific_requirements(self):
        lupia = self.db.resolve("Valkyrie Lupia, Sky Lord Dragonmech")["evolution"]
        self.assertEqual(set(lupia["races"]), {"Armored Dragon", "Fire Bird"})
        self.assertNotIn("civilizations", lupia)  # 'Fire Bird' must not be read as Fire civ
        bolgal = self.db.resolve("Bolgalshack, Super Divine Dragoon")["evolution"]
        self.assertIn("Dragon", bolgal["race_contains"])
        self.assertTrue(dmdb.is_valid_base(self.db.resolve("Bolshack Dragon"), bolgal))
        self.assertFalse(dmdb.is_valid_base(self.db.resolve("Aqua Hulcus"), bolgal))

    def test_synergies(self):
        s = self.db.find_synergies("Armored Blaster Valdios")
        self.assertGreater(s["evolution_bases"]["total"], 0)
        self.assertTrue(all("Human" in c["races"] for c in s["evolution_bases"]["cards"]))


class TestDeckRules(DMTestCase):
    def test_legal_deck(self):
        r = self.db.validate_deck(self.legal)
        self.assertTrue(r["valid"], r["errors"])
        self.assertEqual(r["total_cards"], 40)

    def test_wrong_size(self):
        r = self.db.validate_deck(self.legal[:-1])
        self.assertFalse(r["valid"])
        self.assertIn("36 cards", r["errors"][0])

    def test_too_many_copies_merged_by_name_across_reprints(self):
        cards = self.legal[:-1] + deck(("0063", 2), ("0423", 2), ("Death Smoke", 1))
        r = self.db.validate_deck(cards)
        self.assertFalse(r["valid"])
        self.assertTrue(any("5 copies of 'Death Smoke'" in e for e in r["errors"]))

    def test_unknown_and_bad_counts(self):
        r = self.db.validate_deck(deck(("Not A Card", 4), ("Bolshack Dragon", 0)))
        self.assertFalse(r["valid"])
        self.assertTrue(any("Unknown card" in e for e in r["errors"]))
        self.assertTrue(any("at least 1" in e for e in r["errors"]))

    def test_empty_deck(self):
        with self.assertRaises(DMError):
            self.db.validate_deck([])


class TestAnalysis(DMTestCase):
    def test_stats(self):
        a = self.db.analyze_deck(self.legal)
        self.assertTrue(a["validation"]["valid"])
        self.assertEqual(sum(a["mana_curve"].values()), 40)
        self.assertEqual(a["civilizations"], {"Fire": 40})
        self.assertEqual(a["card_types"], {"Creature": 40})

    def test_warns_on_missing_triggers(self):
        a = self.db.analyze_deck(self.legal)
        self.assertTrue(any("Shield Trigger" in w for w in a["warnings"]))

    def test_evolution_without_bases_warns(self):
        cards = self.legal[:-1] + deck(("Armored Blaster Valdios", 4))
        a = self.db.analyze_deck(cards)
        evo = [e for e in a["evolutions"] if e["card"] == "Armored Blaster Valdios"][0]
        humans = sum(n for c, n in self.db.resolve_deck(self.legal[:-1])[0] if "Human" in c["races"])
        self.assertEqual(evo["bases_in_deck"], humans)
        if humans < 8:
            self.assertTrue(any("Armored Blaster Valdios" in w for w in a["warnings"]))


class TestStorage(DMTestCase):
    def test_save_load_list_delete(self):
        r = self.db.save_deck("Test Fire Rush!", self.legal, strategy="Go fast.")
        self.assertEqual(r["slug"], "test-fire-rush")
        d = self.db.get_deck("test-fire-rush")
        self.assertEqual(d["total_cards"], 40)
        self.assertEqual(d["strategy"], "Go fast.")
        self.assertTrue(d["cards"][0]["image_path"].startswith("data/images/"))
        self.assertIn("test-fire-rush", [x["slug"] for x in self.db.list_decks()])
        with self.assertRaises(DMError):
            self.db.save_deck("Test Fire Rush!", self.legal)  # exists, no overwrite
        self.db.save_deck("Test Fire Rush!", self.legal, overwrite=True)
        self.assertEqual(self.db.delete_deck("test-fire-rush"), {"deleted": "test-fire-rush"})
        with self.assertRaises(DMError):
            self.db.get_deck("test-fire-rush")

    def test_invalid_deck_needs_allow_invalid(self):
        with self.assertRaises(DMError):
            self.db.save_deck("draft", self.legal[:2])
        r = self.db.save_deck("draft", self.legal[:2], allow_invalid=True)
        self.assertFalse(r["valid"])
        self.db.delete_deck("draft")

    def test_slug_blocks_path_traversal(self):
        r = self.db.save_deck("../../evil", self.legal)
        self.assertEqual(r["slug"], "evil")
        self.assertTrue((Path(self.tmp.name) / "evil.json").exists())
        self.db.delete_deck("evil")


class TestProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()

    def call(self, method, params=None, msg_id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})

    def test_initialize_negotiates_version(self):
        r = self.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        r = self.call("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-11-25")

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tool_errors_are_reported_not_raised(self):
        r = self.call("tools/call", {"name": "get_deck", "arguments": {"slug": "does-not-exist"}})["result"]
        self.assertTrue(r["isError"])
        r = self.call("tools/call", {"name": "search_cards", "arguments": {"bogus_arg": 1}})["result"]
        self.assertTrue(r["isError"])

    def test_tool_result_is_json_text(self):
        r = self.call("tools/call", {"name": "list_values", "arguments": {"kind": "civilizations"}})["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(len(json.loads(r["content"][0]["text"])), 5)

    def test_unknown_method_and_tool(self):
        self.assertEqual(self.call("nope")["error"]["code"], -32601)
        self.assertEqual(self.call("tools/call", {"name": "nope"})["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
