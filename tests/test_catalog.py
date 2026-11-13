import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database


FETCHER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_anime.py"
FETCHER_SPEC = importlib.util.spec_from_file_location("fetch_anime", FETCHER_PATH)
fetch_anime = importlib.util.module_from_spec(FETCHER_SPEC)
FETCHER_SPEC.loader.exec_module(fetch_anime)


class KitsuCatalogTests(unittest.TestCase):
    def setUp(self):
        self.included = {
            ("anime", "series-1"): {
                "id": "series-1",
                "type": "anime",
                "attributes": {
                    "canonicalTitle": "Sample Series",
                    "startDate": "2023-01-01",
                    "episodeCount": 12,
                },
            },
            ("mediaRelationships", "season-link"): {
                "id": "season-link",
                "type": "mediaRelationships",
                "attributes": {"role": "sequel"},
                "relationships": {
                    "source": {"data": {"id": "series-1", "type": "anime"}},
                    "destination": {"data": {"id": "series-2", "type": "anime"}},
                },
            },
            ("anime", "series-2"): {
                "id": "series-2",
                "type": "anime",
                "links": {"self": "https://kitsu.io/api/edge/anime/series-2"},
                "attributes": {
                    "canonicalTitle": "Sample Season 2",
                    "startDate": "2024-04-01",
                    "episodeCount": 12,
                },
            },
        }

    def test_normalize_includes_kitsu_trailer_and_related_season(self):
        anime = {
            "id": "series-1",
            "type": "anime",
            "links": {"self": "https://kitsu.io/api/edge/anime/series-1"},
            "attributes": {
                "slug": "sample-series",
                "canonicalTitle": "Sample Series",
                "titles": {"en": "Sample Series"},
                "startDate": "2023-01-01",
                "episodeCount": 12,
                "averageRating": "82",
                "synopsis": "A sample synopsis.",
                "posterImage": {"small": "https://media.kitsu.app/poster.jpg"},
                "youtubeVideoId": "Video_id123",
            },
            "relationships": {
                "genres": {"data": [{"id": "action"}]},
                "mediaRelationships": {
                    "data": [{"id": "season-link", "type": "mediaRelationships"}]
                },
            },
        }

        result = fetch_anime.normalize_anime(
            anime,
            {"action": "Action"},
            {},
            self.included,
        )

        self.assertEqual(result["youtube_video_id"], "Video_id123")
        self.assertEqual(
            result["related_anime"],
            [{
                "id": "series-2",
                "title": "Sample Season 2",
                "role": "sequel",
                "year": 2024,
                "episodes": 12,
                "url": "https://kitsu.io/api/edge/anime/series-2",
            }],
        )

    def test_reverse_sequel_relationship_is_labeled_as_prequel(self):
        anime = {
            "id": "series-2",
            "attributes": {"canonicalTitle": "Sample Season 2"},
            "relationships": {
                "genres": {"data": [{"id": "action"}]},
                "mediaRelationships": {
                    "data": [{"id": "season-link", "type": "mediaRelationships"}]
                },
            },
        }

        result = fetch_anime.normalize_anime(
            anime,
            {"action": "Action"},
            {},
            self.included,
        )

        self.assertEqual(result["related_anime"][0]["id"], "series-1")
        self.assertEqual(result["related_anime"][0]["role"], "prequel")

    def test_database_import_and_catalog_include_kitsu_media_metadata(self):
        test_database = os.environ.get("ANIPICK_TEST_MYSQL_DATABASE")
        if not test_database or not test_database.endswith("_test"):
            self.skipTest("Set ANIPICK_TEST_MYSQL_DATABASE to a dedicated *_test schema")
        item = {
            "id": "series-1",
            "title": "Sample Series",
            "titles": {},
            "genres": ["Action"],
            "youtube_video_id": "Video_id123",
            "related_anime": [{
                "id": "series-2",
                "title": "Sample Season 2",
                "role": "sequel",
                "year": 2024,
                "episodes": 12,
                "url": "https://kitsu.io/api/edge/anime/series-2",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "anime.json"
            youtube_overrides_path = Path(directory) / "youtube-overrides.json"
            korean_overrides_path = Path(directory) / "korean-overrides.json"
            catalog_path.write_text(
                json.dumps({"items": [item]}),
                encoding="utf-8",
            )
            youtube_overrides_path.write_text(
                json.dumps({"series-1": "Curated_id123"}),
                encoding="utf-8",
            )
            korean_overrides_path.write_text(
                json.dumps({"series-2": {"title_ko": "두 번째 시즌"}}),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"ANIPICK_MYSQL_DATABASE": test_database}),
                patch.object(database, "CATALOG_PATH", catalog_path),
                patch.object(
                    database, "KOREAN_OVERRIDES_PATH", korean_overrides_path
                ),
                patch.object(
                    database, "YOUTUBE_OVERRIDES_PATH", youtube_overrides_path
                ),
            ):
                database.initialize_database()
                result = database.get_catalog()[0]

        self.assertEqual(result["youtube_video_id"], "Curated_id123")
        self.assertEqual(result["related_anime"][0]["role"], "sequel")
        self.assertEqual(result["related_anime"][0]["title_ko"], "두 번째 시즌")


if __name__ == "__main__":
    unittest.main()
