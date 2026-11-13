import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server
import character_info


class CharacterApiTests(unittest.TestCase):
    def setUp(self):
        server.character_attempts.clear()
        server.ai_recommend_attempts.clear()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.AnimeRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/characters"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_character_endpoint_returns_translated_payload(self):
        expected = {
            "anime_title": "Attack on Titan",
            "characters": [{"id": "42", "name_ko": "에렌 예거"}],
            "provider": "claude",
            "source": "AniList",
        }
        with patch("server.character_info.get_characters", return_value=expected) as get:
            with urlopen(f"{self.url}?anime_id=7442", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        get.assert_called_once_with("7442")

    def test_character_endpoint_passes_an_explicit_provider(self):
        expected = {"characters": [], "provider": "gemini"}
        with patch(
            "server.character_info.get_characters", return_value=expected
        ) as get:
            with urlopen(
                f"{self.url}?anime_id=7442&provider=gemini", timeout=5
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        get.assert_called_once_with("7442", provider="gemini")

    def test_ai_providers_endpoint_reports_safe_configuration_status(self):
        expected = {
            "default_provider": "claude",
            "providers": [
                {"id": "openai", "label": "OpenAI", "configured": False, "key_name": "OPENAI_API_KEY"},
                {"id": "gemini", "label": "Gemini", "configured": True, "key_name": "GEMINI_API_KEY"},
                {"id": "claude", "label": "Claude", "configured": True, "key_name": "ANTHROPIC_API_KEY"},
            ],
        }
        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value={"user_id": 5},
        ):
            with patch(
                "server.database.get_user_ai_api_key_providers",
                return_value={"gemini"},
            ):
                with patch(
                    "server.api_key_store.encryption_is_available",
                    return_value=True,
                ):
                    with patch(
                        "server.character_info.get_provider_options",
                        return_value=expected,
                    ) as get_options:
                        with urlopen(
                            f"http://127.0.0.1:{self.server.server_port}/api/ai/providers",
                            timeout=5,
                        ) as response:
                            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["authenticated"])
        self.assertTrue(payload["key_management_enabled"])
        configured = {
            provider["id"]: provider for provider in payload["providers"]
        }
        self.assertTrue(configured["gemini"]["personal_configured"])
        self.assertFalse(configured["claude"]["personal_configured"])
        self.assertTrue(configured["gemini"]["configured"])
        self.assertNotIn("claude-test-secret", json.dumps(payload))
        get_options.assert_called_once_with()

    def test_api_key_endpoint_saves_encrypted_keys_for_signed_in_account(self):
        secret = "openai-test-secret"
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/ai/keys",
            data=json.dumps({"openai": secret, "gemini": "", "claude": ""}).encode(
                "utf-8"
            ),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
                "X-CSRF-Token": "valid-csrf-token",
            },
            method="POST",
        )
        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value={"user_id": 42, "csrf_token": "valid-csrf-token"},
        ):
            with patch(
                "server.api_key_store.encrypt_api_key", return_value=b"encrypted"
            ):
                with patch("server.database.save_user_ai_api_keys") as save_keys:
                    with patch(
                        "server.database.get_user_ai_api_key_providers",
                        return_value={"openai"},
                    ):
                        with urlopen(request, timeout=5) as response:
                            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertNotIn(secret, json.dumps(payload))
        save_keys.assert_called_once_with(42, {"openai": b"encrypted"})

    def test_api_key_endpoint_requires_authentication_and_csrf(self):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/ai/keys",
            data=json.dumps({"openai": "test-key"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
                "X-CSRF-Token": "invalid",
            },
            method="POST",
        )
        with patch.object(
            server.AnimeRequestHandler, "get_current_session", return_value=None
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

        self.assertEqual(raised.exception.code, 401)
        with raised.exception as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertIn("로그인", payload["error"])

        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value={"user_id": 42, "csrf_token": "expected"},
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

        self.assertEqual(raised.exception.code, 403)

    def test_api_key_endpoint_can_delete_only_the_signed_in_users_key(self):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/ai/keys",
            data=json.dumps({"delete": "claude"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
                "X-CSRF-Token": "valid-csrf-token",
            },
            method="POST",
        )
        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value={"user_id": 42, "csrf_token": "valid-csrf-token"},
        ):
            with patch("server.database.delete_user_ai_api_key", return_value=True) as delete:
                with urlopen(
                    request, timeout=5
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        delete.assert_called_once_with(42, "claude")

    def test_signed_in_ai_request_uses_only_that_accounts_api_key(self):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/ai/recommend",
            data=json.dumps(
                {"query": "우정과 모험", "provider": "openai"}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
            },
            method="POST",
        )

        def recommend(query, provider):
            self.assertEqual(query, "우정과 모험")
            self.assertEqual(provider, "openai")
            self.assertEqual(
                character_info._provider_config(provider)[1], "account-secret"
            )
            return {"items": []}

        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value={"user_id": 42},
        ):
            with patch(
                "server.api_key_store.get_user_api_keys",
                return_value={"openai": "account-secret"},
            ):
                with patch(
                    "server.ai_recommender.recommend", side_effect=recommend
                ) as call:
                    with urlopen(request, timeout=5) as response:
                        payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, {"items": []})
        call.assert_called_once_with("우정과 모험", provider="openai")

    def test_character_endpoint_reports_missing_anime_id(self):
        with patch("server.character_info.get_characters") as get:
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.url, timeout=5)

        self.assertEqual(raised.exception.code, 400)
        with raised.exception as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertIn("anime_id", payload["error"])
        get.assert_not_called()

    def test_character_service_error_preserves_status_and_message(self):
        with patch(
            "server.character_info.get_characters",
            side_effect=character_info.CharacterInfoError("AI 키를 확인해주세요.", 503),
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{self.url}?anime_id=7442", timeout=5)

        self.assertEqual(raised.exception.code, 503)
        with raised.exception as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["error"], "AI 키를 확인해주세요.")


if __name__ == "__main__":
    unittest.main()
