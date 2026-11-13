import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ai_synopsis
import character_info
import database
import server


ANIME = {
    "id": "42",
    "title": "Sample Anime",
    "title_ko": "샘플 애니",
    "synopsis": "A student joins a club and discovers a hidden world.",
    "synopsis_ko": None,
}


class AiSynopsisTests(unittest.TestCase):
    def test_summarize_returns_korean_text_and_persists_it(self):
        with (
            patch.object(database, "get_catalog", return_value=[ANIME]),
            patch.object(database, "get_ai_synopsis", return_value=None),
            patch.object(database, "save_ai_synopsis") as save,
            patch.object(
                character_info,
                "_provider_config",
                return_value=("gemini", "private-test-key", "test-model"),
            ),
            patch.object(
                character_info,
                "_provider_text",
                return_value="주인공은 동아리에 가입한 뒤 숨겨진 세계를 발견합니다.",
            ) as provider_text,
        ):
            result = ai_synopsis.summarize("42", provider="gemini")

        self.assertEqual(
            result,
            {
                "summary": "주인공은 동아리에 가입한 뒤 숨겨진 세계를 발견합니다.",
                "provider": "gemini",
                "cached": False,
                "source": "ai",
            },
        )
        prompt = provider_text.call_args.args[3]
        self.assertIn("결말과 반전은 밝히지 말고", prompt)
        self.assertIn("hidden world", prompt)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], "42")
        self.assertEqual(save.call_args.args[2], result["summary"])
        self.assertEqual(save.call_args.args[3], "gemini")

    def test_summarize_uses_persistent_cache_without_provider_call(self):
        with (
            patch.object(database, "get_catalog", return_value=[ANIME]),
            patch.object(
                database,
                "get_ai_synopsis",
                return_value={"summary": "저장된 요약입니다.", "provider": "claude"},
            ),
            patch.object(character_info, "_provider_config") as provider_config,
            patch.object(character_info, "_provider_text") as provider_text,
        ):
            result = ai_synopsis.summarize("42", provider="openai")

        self.assertTrue(result["cached"])
        self.assertEqual(result["summary"], "저장된 요약입니다.")
        self.assertEqual(result["provider"], "claude")
        provider_config.assert_not_called()
        provider_text.assert_not_called()

    def test_summarize_preserves_catalog_korean_synopsis(self):
        anime = {**ANIME, "synopsis_ko": "편집된 공식 한국어 소개"}
        with (
            patch.object(database, "get_catalog", return_value=[anime]),
            patch.object(database, "get_ai_synopsis") as get_cached,
            patch.object(character_info, "_provider_text") as provider_text,
        ):
            result = ai_synopsis.summarize("42")

        self.assertEqual(result["summary"], "편집된 공식 한국어 소개")
        self.assertEqual(result["source"], "catalog")
        get_cached.assert_not_called()
        provider_text.assert_not_called()

    def test_summarize_rejects_unknown_anime_and_missing_source_synopsis(self):
        with patch.object(database, "get_catalog", return_value=[]):
            with self.assertRaises(ai_synopsis.AiSynopsisError) as unknown:
                ai_synopsis.summarize("404")
        self.assertEqual(unknown.exception.status, 404)

        with patch.object(
            database, "get_catalog", return_value=[{**ANIME, "synopsis": ""}]
        ):
            with self.assertRaises(ai_synopsis.AiSynopsisError) as missing:
                ai_synopsis.summarize("42")
        self.assertEqual(missing.exception.status, 422)

    def test_summarize_rejects_empty_or_oversized_provider_output(self):
        for output in ("  ", "x" * (ai_synopsis.MAX_SUMMARY_LENGTH + 1)):
            with (
                patch.object(database, "get_catalog", return_value=[ANIME]),
                patch.object(database, "get_ai_synopsis", return_value=None),
                patch.object(
                    character_info,
                    "_provider_config",
                    return_value=("claude", "test-key", "test-model"),
                ),
                patch.object(character_info, "_provider_text", return_value=output),
            ):
                with self.assertRaises(ai_synopsis.AiSynopsisError):
                    ai_synopsis.summarize("42")


class AiSynopsisDatabaseTests(unittest.TestCase):
    def test_ai_synopsis_cache_persists_and_updates_for_changed_source(self):
        test_database = os.environ.get("ANIPICK_TEST_MYSQL_DATABASE")
        if not test_database or not test_database.endswith("_test"):
            self.skipTest("Set ANIPICK_TEST_MYSQL_DATABASE to a dedicated *_test schema")
        with patch.dict(os.environ, {"ANIPICK_MYSQL_DATABASE": test_database}):
            database.initialize_database()
            anime_id = database.get_catalog()[0]["id"]
            database.save_ai_synopsis(anime_id, "a" * 64, "첫 요약", "openai")
            self.assertEqual(
                database.get_ai_synopsis(anime_id, "a" * 64),
                {"summary": "첫 요약", "provider": "openai"},
            )
            self.assertIsNone(database.get_ai_synopsis(anime_id, "b" * 64))

            database.save_ai_synopsis(anime_id, "b" * 64, "새 원문 요약", "gemini")

            self.assertIsNone(database.get_ai_synopsis(anime_id, "a" * 64))
            self.assertEqual(
                database.get_ai_synopsis(anime_id, "b" * 64),
                {"summary": "새 원문 요약", "provider": "gemini"},
            )


class AiSynopsisApiTests(unittest.TestCase):
    def setUp(self):
        server.ai_synopsis_attempts.clear()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.AnimeRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/ai/synopsis"

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

    def test_synopsis_endpoint_returns_summary_and_provider(self):
        expected = {
            "summary": "한국어 요약입니다.",
            "provider": "gemini",
            "cached": False,
            "source": "ai",
        }
        with patch("server.ai_synopsis.summarize", return_value=expected) as summarize:
            with urlopen(
                self.request({"anime_id": "42", "provider": "gemini"}), timeout=5
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        summarize.assert_called_once_with("42", provider="gemini")

    def test_synopsis_endpoint_validates_body_and_provider(self):
        with patch("server.ai_synopsis.summarize") as summarize:
            with self.assertRaises(HTTPError) as invalid_id:
                urlopen(self.request({"anime_id": ""}), timeout=5)
            self.assertEqual(invalid_id.exception.code, 400)

            with self.assertRaises(HTTPError) as invalid_provider:
                urlopen(
                    self.request({"anime_id": "42", "provider": "unknown"}),
                    timeout=5,
                )
            self.assertEqual(invalid_provider.exception.code, 400)

        summarize.assert_not_called()

    def test_synopsis_endpoint_preserves_provider_errors(self):
        with patch(
            "server.ai_synopsis.summarize",
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
