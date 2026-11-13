import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import recommender
import server


def anime(anime_id, title, synopsis, genres, title_ko=None):
    return {
        "id": anime_id,
        "title": title,
        "title_ko": title_ko,
        "synopsis": synopsis,
        "genres": genres,
    }


CATALOG = [
    anime(
        "1",
        "Dragon Quest",
        "A young hero fights dragons in a magic kingdom.",
        ["Fantasy", "Action"],
        "용사",
    ),
    anime(
        "2",
        "Wizard Tale",
        "A hero learns magic and fights a dragon king.",
        ["Fantasy", "Magic"],
    ),
    anime(
        "3",
        "Cafe Days",
        "Friends spend peaceful daily life at a small cafe.",
        ["Slice of Life"],
        "카페",
    ),
    anime(
        "4",
        "Ghost House",
        "Students fear a ghost haunting the school.",
        ["Horror"],
    ),
]


class RecommenderTests(unittest.TestCase):
    def setUp(self):
        self.catalog_patcher = patch(
            "recommender.database.get_catalog", return_value=CATALOG
        )
        self.catalog_patcher.start()
        recommender.get_index(force=True)

    def tearDown(self):
        self.catalog_patcher.stop()
        with recommender._lock:
            recommender._index = None

    def test_similar_ranks_related_story_first(self):
        result = recommender.similar("1", limit=3)
        self.assertEqual(result[0]["id"], "2")
        self.assertIn("Fantasy", result[0]["shared_genres"])

    def test_similar_unknown_id_returns_none(self):
        self.assertIsNone(recommender.similar("missing"))

    def test_recommend_excludes_favorites_and_explains(self):
        result = recommender.recommend(["1"], limit=3)
        self.assertNotIn("1", [row["id"] for row in result])
        self.assertEqual(result[0]["id"], "2")
        self.assertIn("줄거리가 비슷해요", result[0]["reason"])

    def test_recommend_blends_preferred_genres(self):
        result = recommender.recommend([], ["Horror"], limit=2)
        self.assertEqual(result[0]["id"], "4")

    def test_recommend_without_signal_is_empty(self):
        self.assertEqual(recommender.recommend([], []), [])

    def test_korean_search_hint_matches_genre(self):
        result = recommender.search("힐링")
        self.assertEqual(result[0]["id"], "3")

    def test_ai_tags_extend_search_and_similarity(self):
        tagged_catalog = [
            anime("1", "Revenge Story", "A hero returns.", [], "복수 이야기"),
            {
                **anime("2", "Dark Journey", "A traveler moves on.", [], "어두운 여정"),
                "ai_tags": [
                    {"category": "theme", "name": "복수"},
                    {"category": "mood", "name": "긴장감"},
                ],
            },
            {
                **anime("3", "Quiet Days", "Friends share tea.", [], "고요한 나날"),
                "ai_tags": [{"category": "mood", "name": "잔잔함"}],
            },
        ]
        tagged_catalog[0]["ai_tags"] = [{"category": "theme", "name": "복수"}]
        with patch.object(recommender.database, "get_catalog", return_value=tagged_catalog):
            recommender.get_index(force=True)
            self.assertEqual(recommender.search("긴장감")[0]["id"], "2")
            self.assertEqual(recommender.similar("1", limit=1)[0]["id"], "2")
            self.assertEqual(recommender.recommend(["1"], limit=1)[0]["id"], "2")

    def test_title_substring_search(self):
        self.assertEqual(recommender.search("용사")[0]["id"], "1")

    def test_original_title_search(self):
        self.assertEqual(recommender.search("Dragon")[0]["id"], "1")

    def test_blank_search_is_empty(self):
        self.assertEqual(recommender.search("   "), [])

    def test_genre_weights_are_normalized(self):
        weights = recommender.genre_weights(["1", "2"])
        self.assertEqual(weights["Fantasy"], 1.0)
        self.assertEqual(weights["Magic"], 0.5)


class RecommenderApiTests(unittest.TestCase):
    def setUp(self):
        self.catalog_patcher = patch(
            "recommender.database.get_catalog", return_value=CATALOG
        )
        self.catalog_patcher.start()
        recommender.get_index(force=True)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.AnimeRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.catalog_patcher.stop()
        with recommender._lock:
            recommender._index = None

    def test_search_endpoint_validates_query(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base_url}/api/search?q=", timeout=5)
        self.assertEqual(raised.exception.code, 400)

    def test_search_endpoint_returns_similar_catalog_items(self):
        with urlopen(
            f"{self.base_url}/api/search?q=%ED%9E%90%EB%A7%81", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["items"][0]["id"], "3")

    def test_similar_endpoint_returns_catalog_candidates(self):
        with urlopen(
            f"{self.base_url}/api/anime/1/similar", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["items"][0]["id"], "2")

    def test_personalized_recommendations_require_login(self):
        with patch.object(server.AnimeRequestHandler, "get_current_session", return_value=None):
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{self.base_url}/api/recommendations", timeout=5)
        self.assertEqual(raised.exception.code, 401)

    def test_personalized_recommendations_use_favorites_and_genres(self):
        with (
            patch.object(
                server.AnimeRequestHandler,
                "get_current_session",
                return_value={"user_id": 7, "username": "tester"},
            ),
            patch("server.database.get_favorites", return_value=["1"]),
            patch(
                "server.database.get_genres",
                return_value=["Action", "Fantasy", "Magic", "Slice of Life", "Horror"],
            ),
            urlopen(
                f"{self.base_url}/api/recommendations?genres=Fantasy",
                timeout=5,
            ) as response,
        ):
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload["favorite_count"], 1)
        self.assertEqual(payload["items"][0]["id"], "2")
        self.assertEqual(payload["items"][0]["matched_genres"], ["Fantasy"])


if __name__ == "__main__":
    unittest.main()
