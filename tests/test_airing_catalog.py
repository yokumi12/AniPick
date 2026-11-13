import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_AIRING_ADDITIONS = {
    "50261", "50764", "50964", "50966", "50988", "51012", "50338"
}


class AiringCatalogTests(unittest.TestCase):
    def test_only_selected_airing_additions_are_localized_and_genre_tagged(self):
        catalog = json.loads(
            (PROJECT_ROOT / "data" / "anime.json").read_text(encoding="utf-8")
        )
        overrides = json.loads(
            (PROJECT_ROOT / "data" / "korean-overrides.json").read_text(
                encoding="utf-8"
            )
        )
        catalog_ids = {str(item["id"]) for item in catalog["items"]}
        baseline_ids = catalog_ids - EXPECTED_AIRING_ADDITIONS
        additions = {
            str(item["id"]): item
            for item in catalog["items"]
            if str(item["id"]) in EXPECTED_AIRING_ADDITIONS
        }

        self.assertEqual(len(baseline_ids), 61)
        self.assertEqual(set(additions), EXPECTED_AIRING_ADDITIONS)
        self.assertEqual(catalog["count"], len(catalog["items"]))
        for anime_id, item in additions.items():
            with self.subTest(anime_id=anime_id):
                self.assertTrue(item["genres"])
                self.assertTrue(overrides[anime_id]["title_ko"])
                self.assertTrue(overrides[anime_id]["synopsis_ko"])


if __name__ == "__main__":
    unittest.main()
