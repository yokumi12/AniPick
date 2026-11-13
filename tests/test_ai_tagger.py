import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ai_tagger
import character_info
import database
import recommender
import server


ANIME = {
    "id": "42",
    "title": "Sample Anime",
    "title_ko": "샘플 애니",
    "synopsis": "A student survives in a mysterious world.",
    "synopsis_ko": None,
    "ai_tags": [],
}


class AiTaggerTests(unittest.TestCase):
    def test_tag_anime_returns_only_allowed_tags_and_persists_them(self):
        response = json.dumps(
            {
                "tags": [
                    {"category": "mood", "tag": "긴장감"},
                    {"category": "theme", "tag": "생존"},
                    {"category": "theme", "tag": "신규 태그"},
                    {"category": "mood", "tag": "긴장감"},
                ]
            },
            ensure_ascii=False,
        )
        with (
            patch.object(database, "get_catalog", return_value=[ANIME]),
            patch.object(database, "get_ai_tags", return_value=None),
            patch.object(database, "save_ai_tags") as save,
            patch.object(recommender, "get_index") as rebuild,
            patch.object(
                character_info,
                "_provider_config",
                return_value=("gemini", "private-test-key", "test-model"),
            ),
            patch.object(
                character_info, "_provider_text", return_value=response
            ) as provider_text,
        ):
            result = ai_tagger.tag_anime("42", provider="gemini")

        self.assertEqual(
            result["tags"],
            [
                {"category": "mood", "name": "긴장감"},
                {"category": "theme", "name": "생존"},
            ],
        )
        self.assertEqual(result["provider"], "gemini")
        self.assertFalse(result["cached"])
        self.assertIn("허용 태그(JSON)", provider_text.call_args.args[3])
        save.assert_called_once_with("42", ai_tagger._source_hash(ANIME["synopsis"]), result["tags"], "gemini")
        rebuild.assert_called_once_with(force=True)

    def test_tag_anime_returns_persisted_cache_without_provider_call(self):
        cached_tags = [{"category": "theme", "name": "성장"}]
        with (
            patch.object(database, "get_catalog", return_value=[ANIME]),
            patch.object(
                database,
                "get_ai_tags",
                return_value={"tags": cached_tags, "provider": "claude"},
            ),
            patch.object(character_info, "_provider_config") as provider_config,
            patch.object(character_info, "_provider_text") as provider_text,
            patch.object(recommender, "get_index") as rebuild,
        ):
            result = ai_tagger.tag_anime("42", provider="openai")

        self.assertEqual(result["tags"], cached_tags)
        self.assertTrue(result["cached"])
        self.assertEqual(result["provider"], "claude")
        provider_config.assert_not_called()
        provider_text.assert_not_called()
        rebuild.assert_not_called()

    def test_tag_anime_validates_work_and_synopsis(self):
        with patch.object(database, "get_catalog", return_value=[]):
            with self.assertRaises(ai_tagger.AiTaggingError) as unknown:
                ai_tagger.tag_anime("missing")
        self.assertEqual(unknown.exception.status, 404)

        with patch.object(
            database, "get_catalog", return_value=[{**ANIME, "synopsis": ""}]
        ):
            with self.assertRaises(ai_tagger.AiTaggingError) as missing:
                ai_tagger.tag_anime("42")
        self.assertEqual(missing.exception.status, 422)

    def test_tag_parser_rejects_malformed_or_empty_output(self):
        for output in (
            "not json",
            '{"tags":[]}',
            '{"tags":[{"category":"genre","tag":"action"}]}',
            '{"tags":[{"category":[],"tag":"긴장감"}]}',
        ):
            with self.subTest(output=output):
                with self.assertRaises(ai_tagger.AiTaggingError):
                    ai_tagger._parse_tags(output)

    def test_tag_cache_persists_and_appears_in_catalog(self):
        test_database = os.environ.get("ANIPICK_TEST_MYSQL_DATABASE")
        if not test_database or not test_database.endswith("_test"):
            self.skipTest("Set ANIPICK_TEST_MYSQL_DATABASE to a dedicated *_test schema")
        with patch.dict(os.environ, {"ANIPICK_MYSQL_DATABASE": test_database}):
            database.initialize_database()
            anime_id = database.get_catalog()[0]["id"]
            tags = [{"category": "theme", "name": "모험"}]
            database.save_ai_tags(anime_id, "a" * 64, tags, "openai")

            self.assertEqual(
                database.get_ai_tags(anime_id, "a" * 64),
                {"tags": tags, "provider": "openai"},
            )
            self.assertIsNone(database.get_ai_tags(anime_id, "b" * 64))
            catalog_item = next(
                item
                for item in database.get_catalog()
                if item["id"] == anime_id
            )
            self.assertEqual(catalog_item["ai_tags"], tags)


class AiTaggingApiTests(unittest.TestCase):
    def setUp(self):
        server.ai_tag_attempts.clear()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.AnimeRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/ai/tags"

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

    def test_tags_endpoint_returns_generated_tags(self):
        expected = {
            "tags": [{"category": "mood", "name": "긴장감"}],
            "provider": "gemini",
            "cached": False,
            "source": "ai",
        }
        with patch("server.ai_tagger.tag_anime", return_value=expected) as tag_anime:
            with urlopen(
                self.request({"anime_id": "42", "provider": "gemini"}), timeout=5
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        tag_anime.assert_called_once_with("42", provider="gemini")

    def test_tags_endpoint_validates_body_and_provider(self):
        with patch("server.ai_tagger.tag_anime") as tag_anime:
            with self.assertRaises(HTTPError) as invalid_id:
                urlopen(self.request({"anime_id": ""}), timeout=5)
            self.assertEqual(invalid_id.exception.code, 400)

            with self.assertRaises(HTTPError) as invalid_provider:
                urlopen(
                    self.request({"anime_id": "42", "provider": "unknown"}),
                    timeout=5,
                )
            self.assertEqual(invalid_provider.exception.code, 400)

        tag_anime.assert_not_called()

    def test_tags_endpoint_preserves_provider_errors(self):
        with patch(
            "server.ai_tagger.tag_anime",
            side_effect=character_info.CharacterInfoError("AI API 인증에 실패했어요.", 503),
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.request({"anime_id": "42"}), timeout=5)

        self.assertEqual(raised.exception.code, 503)
        with raised.exception as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["error"], "AI API 인증에 실패했어요.")


if __name__ == "__main__":
    unittest.main()
