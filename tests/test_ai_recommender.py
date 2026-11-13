import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ai_recommender
import character_info
import server


CATALOG = [
    {
        "id": "1",
        "title": "One Punch Man",
        "title_ko": "원펀맨",
        "titles": {"en": "One Punch Man"},
        "year": 2015,
        "genres": ["Action", "Comedy"],
        "synopsis": "A hero defeats every enemy with one punch.",
    },
    {
        "id": "2",
        "title": "Cafe Days",
        "title_ko": "카페 데이즈",
        "titles": {"en": "Cafe Days"},
        "year": 2020,
        "genres": ["Slice of Life"],
        "synopsis": "Friends enjoy a peaceful life at a cafe.",
    },
]


class AiRecommenderTests(unittest.TestCase):
    def test_recommend_returns_only_catalog_ids_with_reasons(self):
        response = json.dumps(
            {
                "recommendations": [
                    {"id": "1", "reason": "한 방에 적을 쓰러뜨리는 통쾌한 액션이에요."},
                    {"id": "outside", "reason": "목록에 없는 작품"},
                    {"id": "1", "reason": "중복 작품"},
                    {"id": "2", "reason": "일상 작품"},
                ]
            },
            ensure_ascii=False,
        )
        with (
            patch.object(ai_recommender.database, "get_catalog", return_value=CATALOG),
            patch.object(
                ai_recommender.character_info,
                "_provider_config",
                return_value=("gemini", "private-test-key", "test-model"),
            ),
            patch.object(
                ai_recommender.character_info,
                "_provider_text",
                return_value=response,
            ) as provider_text,
        ):
            result = ai_recommender.recommend(
                "액션인데 시원시원한 거", provider="gemini"
            )

        self.assertEqual(result["provider"], "gemini")
        self.assertEqual([item["id"] for item in result["items"]], ["1", "2"])
        self.assertIn("통쾌한 액션", result["items"][0]["reason"])
        prompt = provider_text.call_args.args[3]
        self.assertIn("One Punch Man", prompt)
        self.assertIn("액션인데 시원시원한 거", prompt)

    def test_recommend_rejects_empty_and_oversized_queries(self):
        with self.assertRaises(ai_recommender.AiRecommendationError) as empty:
            ai_recommender.recommend(" ")
        self.assertEqual(empty.exception.status, 400)

        with self.assertRaises(ai_recommender.AiRecommendationError) as oversized:
            ai_recommender.recommend("x" * (ai_recommender.MAX_QUERY_LENGTH + 1))
        self.assertEqual(oversized.exception.status, 400)

    def test_recommend_rejects_malformed_ai_json(self):
        with (
            patch.object(ai_recommender.database, "get_catalog", return_value=CATALOG),
            patch.object(
                ai_recommender.character_info,
                "_provider_config",
                return_value=("openai", "test-key", "test-model"),
            ),
            patch.object(
                ai_recommender.character_info,
                "_provider_text",
                return_value="not json",
            ),
        ):
            with self.assertRaises(ai_recommender.AiRecommendationError):
                ai_recommender.recommend("유쾌한 액션", provider="openai")

    def test_recommend_requires_a_configured_provider_key(self):
        with patch.object(
            ai_recommender.character_info,
            "_provider_config",
            side_effect=character_info.CharacterInfoError("키가 필요해요.", 503),
        ):
            with self.assertRaises(character_info.CharacterInfoError) as raised:
                ai_recommender.recommend("재미있는 작품", provider="claude")
        self.assertEqual(raised.exception.status, 503)


class AiRecommendationApiTests(unittest.TestCase):
    def setUp(self):
        server.ai_recommend_attempts.clear()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.AnimeRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/ai/recommend"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, body):
        return Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
            },
            method="POST",
        )

    def test_ai_recommendation_endpoint_is_public_and_passes_provider(self):
        expected = {"items": [{"id": "1", "reason": "통쾌한 액션이에요."}], "provider": "gemini"}
        with patch(
            "server.ai_recommender.recommend", return_value=expected
        ) as recommend:
            with urlopen(
                self.request({"query": "한 방에 적을 물리치는 액션", "provider": "gemini"}),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        recommend.assert_called_once_with("한 방에 적을 물리치는 액션", provider="gemini")

    def test_ai_recommendation_endpoint_validates_body(self):
        with patch("server.ai_recommender.recommend") as recommend:
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.request({"query": ""}), timeout=5)

        self.assertEqual(raised.exception.code, 400)
        recommend.assert_not_called()

    def test_ai_recommendation_endpoint_rejects_unknown_provider(self):
        with patch("server.ai_recommender.recommend") as recommend:
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    self.request({"query": "액션", "provider": "unknown"}),
                    timeout=5,
                )

        self.assertEqual(raised.exception.code, 400)
        recommend.assert_not_called()

    def test_ai_recommendation_endpoint_preserves_provider_errors(self):
        with patch(
            "server.ai_recommender.recommend",
            side_effect=character_info.CharacterInfoError("API 키를 확인해주세요.", 503),
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.request({"query": "편안한 힐링물"}), timeout=5)

        self.assertEqual(raised.exception.code, 503)
        with raised.exception as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["error"], "API 키를 확인해주세요.")


if __name__ == "__main__":
    unittest.main()
