import json
import os
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ai_curator
import character_info
import database
import server


CATALOG = [
    {
        "id": "1",
        "title": "Dragon Quest",
        "title_ko": "용사의 여행",
        "titles": {"en": "Dragon Quest"},
        "genres": ["Fantasy", "Adventure"],
        "ai_tags": [{"category": "theme", "name": "모험"}],
        "synopsis": "A young hero travels to save a kingdom.",
    },
    {
        "id": "2",
        "title": "Cafe Days",
        "title_ko": "카페의 하루",
        "titles": {"en": "Cafe Days"},
        "genres": ["Slice of Life"],
        "ai_tags": [],
        "synopsis": "Friends enjoy quiet days at a cafe.",
    },
]


class AiCuratorTests(unittest.TestCase):
    def test_respond_returns_only_catalog_recommendations(self):
        response = json.dumps(
            {
                "message": "성장과 모험을 좋아하시는군요. 이런 작품은 어떠세요?",
                "recommendations": [
                    {"id": "1", "reason": "여정을 통해 왕국을 구하는 모험담이에요."},
                    {"id": "outside", "reason": "카탈로그에 없는 작품"},
                ],
            },
            ensure_ascii=False,
        )
        history = [
            {"role": "user", "content": "판타지 좋아해"},
            {
                "role": "assistant",
                "content": "모험이 좋으시군요.",
                "recommendations": [{"id": "1", "reason": "이전 추천"}],
            },
        ]
        with (
            patch.object(database, "get_catalog", return_value=CATALOG),
            patch.object(
                character_info,
                "_provider_config",
                return_value=("gemini", "private-test-key", "test-model"),
            ),
            patch.object(
                character_info, "_provider_text", return_value=response
            ) as provider_text,
        ):
            result = ai_curator.respond("성장 이야기도 좋아", history, "gemini")

        self.assertEqual(result["provider"], "gemini")
        self.assertEqual([item["id"] for item in result["recommendations"]], ["1"])
        prompt = provider_text.call_args.args[3]
        self.assertIn("Dragon Quest", prompt)
        self.assertIn("이전 추천", prompt)
        self.assertIn("바로 추천", prompt)

    def test_respond_can_ask_a_follow_up_without_recommendations(self):
        response = json.dumps(
            {"message": "가벼운 분위기와 진지한 분위기 중 어느 쪽을 더 좋아하세요?", "recommendations": []},
            ensure_ascii=False,
        )
        with (
            patch.object(database, "get_catalog", return_value=CATALOG),
            patch.object(
                character_info,
                "_provider_config",
                return_value=("claude", "private-test-key", "test-model"),
            ),
            patch.object(character_info, "_provider_text", return_value=response),
        ):
            result = ai_curator.respond("뭘 좋아하는지 아직 잘 모르겠어")

        self.assertEqual(result["recommendations"], [])
        self.assertIn("어느 쪽", result["message"])

    def test_respond_validates_message_and_history(self):
        with self.assertRaises(ai_curator.AiCuratorError) as empty:
            ai_curator.respond(" ")
        self.assertEqual(empty.exception.status, 400)

        with self.assertRaises(ai_curator.AiCuratorError) as too_long:
            ai_curator.respond("가" * (ai_curator.MAX_MESSAGE_LENGTH + 1))
        self.assertEqual(too_long.exception.status, 400)

        invalid_history = [{"role": "assistant", "content": "먼저 말해요."}]
        with self.assertRaises(ai_curator.AiCuratorError):
            ai_curator.respond("다음 작품", invalid_history)

    def test_response_parser_discards_unknown_and_duplicate_ids(self):
        payload = {
            "message": "추천 목록을 확인해보세요.",
            "recommendations": [
                {"id": "1", "reason": "첫 추천"},
                {"id": "1", "reason": "중복"},
                {"id": "missing", "reason": "외부 ID"},
            ],
        }
        result = ai_curator._parse_response(
            json.dumps(payload, ensure_ascii=False),
            ai_curator._catalog_context(CATALOG),
        )
        self.assertEqual(len(result["recommendations"]), 1)

    def test_curator_can_return_up_to_five_catalog_recommendations(self):
        catalog = CATALOG + [
            {"id": str(index), "title": f"Anime {index}"}
            for index in range(3, 7)
        ]
        payload = {
            "message": "조건에 맞는 작품을 찾아봤어요.",
            "recommendations": [
                {"id": anime["id"], "reason": f"{anime['title']} 추천 이유"}
                for anime in catalog
            ],
        }

        result = ai_curator._parse_response(
            json.dumps(payload, ensure_ascii=False),
            ai_curator._catalog_context(catalog),
        )

        self.assertEqual(
            [item["id"] for item in result["recommendations"]],
            ["1", "2", "3", "4", "5"],
        )


class AiCuratorDatabaseTests(unittest.TestCase):
    def test_curator_conversation_is_persisted_and_private_to_its_owner(self):
        test_database = os.environ.get("ANIPICK_TEST_MYSQL_DATABASE")
        if not test_database or not test_database.endswith("_test"):
            self.skipTest("Set ANIPICK_TEST_MYSQL_DATABASE to a dedicated *_test schema")
        with patch.dict(os.environ, {"ANIPICK_MYSQL_DATABASE": test_database}):
            database.initialize_database()
            suffix = uuid.uuid4().hex[:8]
            first_user = database.create_user(f"first{suffix}", b"1234567890123456", b"x" * 32)
            second_user = database.create_user(f"second{suffix}", b"1234567890123456", b"y" * 32)
            conversation_id = f"conversation-{suffix}"
            response = {
                "message": "이 작품을 추천해요.",
                "recommendations": [{"id": "7442", "reason": "모험이 이어져요."}],
            }
            database.save_curator_turn(first_user, conversation_id, "모험물 추천해줘", response)

            conversation = database.get_curator_conversation(
                first_user, conversation_id
            )
            self.assertIsNotNone(conversation)
            self.assertEqual(len(conversation["messages"]), 2)
            self.assertEqual(
                conversation["messages"][1]["recommendations"],
                response["recommendations"],
            )
            self.assertIsNone(
                database.get_curator_conversation(second_user, conversation_id)
            )
            self.assertEqual(
                database.list_curator_conversations(first_user)[0]["title"],
                "모험물 추천해줘",
            )
            self.assertFalse(
                database.delete_curator_conversation(
                    second_user, conversation_id
                )
            )
            self.assertTrue(
                database.delete_curator_conversation(
                    first_user, conversation_id
                )
            )
            self.assertIsNone(
                database.get_curator_conversation(first_user, conversation_id)
            )


class AiCuratorApiTests(unittest.TestCase):
    def setUp(self):
        server.ai_curator_attempts.clear()
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

    def request(self, path, body=None, headers=None, method=None):
        request_headers = {
            "Origin": f"http://127.0.0.1:{self.server.server_port}",
            **(headers or {}),
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        return Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=request_headers,
            method=method or ("POST" if body is not None else "GET"),
        )

    def test_anonymous_curator_endpoint_accepts_valid_turns(self):
        expected = {
            "message": "어떤 분위기를 더 좋아하세요?",
            "recommendations": [],
            "provider": "claude",
        }
        with (
            patch.object(server.AnimeRequestHandler, "get_current_session", return_value=None),
            patch("server.ai_curator.respond", return_value=expected) as respond,
        ):
            with urlopen(
                self.request(
                    "/api/ai/curator",
                    {
                        "message": "판타지 좋아",
                        "history": [
                            {"role": "user", "content": "애니 추천해줘"},
                            {"role": "assistant", "content": "어떤 장르를 좋아하세요?"},
                        ],
                    },
                ),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload["message"], expected["message"])
        self.assertIsNone(payload["conversation_id"])
        respond.assert_called_once()

    def test_saved_conversation_list_requires_login(self):
        with patch.object(
            server.AnimeRequestHandler, "get_current_session", return_value=None
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    self.request("/api/ai/curator/conversations"),
                    timeout=5,
                )
        self.assertEqual(raised.exception.code, 401)

        expected = [{"id": "conversation-one", "title": "모험 애니", "updated_at": "now"}]
        with (
            patch.object(
                server.AnimeRequestHandler,
                "get_current_session",
                return_value={"user_id": 8, "username": "tester"},
            ),
            patch(
                "server.database.list_curator_conversations",
                return_value=expected,
            ) as list_conversations,
        ):
            with urlopen(
                self.request("/api/ai/curator/conversations"),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload["items"], expected)
        list_conversations.assert_called_once_with(8)

    def test_logged_in_curator_saves_turn_and_requires_csrf(self):
        session = {
            "user_id": 7,
            "username": "tester",
            "csrf_token": "csrf-test-token",
        }
        expected = {
            "message": "이 모험물을 살펴보세요.",
            "recommendations": [{"id": "1", "reason": "여정이 잘 맞아요."}],
            "provider": "gemini",
        }
        with (
            patch.object(server.AnimeRequestHandler, "get_current_session", return_value=session),
            patch("server.ai_curator.respond", return_value=expected) as respond,
            patch("server.api_key_store.get_user_api_keys", return_value={}),
            patch("server.database.save_curator_turn") as save_turn,
        ):
            with urlopen(
                self.request(
                    "/api/ai/curator",
                    {"message": "모험물"},
                    {"X-CSRF-Token": "csrf-test-token"},
                ),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["conversation_id"])
        save_turn.assert_called_once()
        self.assertEqual(save_turn.call_args.args[0:3], (7, payload["conversation_id"], "모험물"))
        respond.assert_called_once()

        with (
            patch.object(server.AnimeRequestHandler, "get_current_session", return_value=session),
            patch("server.ai_curator.respond") as blocked,
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    self.request("/api/ai/curator", {"message": "모험물"}),
                    timeout=5,
                )
        self.assertEqual(raised.exception.code, 403)
        blocked.assert_not_called()

    def test_curator_api_rejects_bad_provider_and_unowned_conversation(self):
        with patch.object(server.AnimeRequestHandler, "get_current_session", return_value=None):
            with self.assertRaises(HTTPError) as invalid_provider:
                urlopen(
                    self.request(
                        "/api/ai/curator",
                        {"message": "안녕", "provider": "unknown"},
                    ),
                    timeout=5,
                )
        self.assertEqual(invalid_provider.exception.code, 400)

        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value={"user_id": 3, "csrf_token": "token"},
        ), patch("server.database.get_curator_conversation", return_value=None):
            with self.assertRaises(HTTPError) as missing:
                urlopen(
                    self.request(
                        "/api/ai/curator",
                        {"message": "계속", "conversation_id": "someone-elses"},
                        {"X-CSRF-Token": "token"},
                    ),
                    timeout=5,
                )
        self.assertEqual(missing.exception.code, 404)

    def test_saved_conversation_delete_requires_login_csrf_and_ownership(self):
        path = "/api/ai/curator/conversations/conversation-one"
        with patch.object(server.AnimeRequestHandler, "get_current_session", return_value=None):
            with self.assertRaises(HTTPError) as anonymous:
                urlopen(self.request(path, method="DELETE"), timeout=5)
        self.assertEqual(anonymous.exception.code, 401)

        session = {"user_id": 8, "csrf_token": "csrf-test-token"}
        with patch.object(
            server.AnimeRequestHandler,
            "get_current_session",
            return_value=session,
        ):
            with self.assertRaises(HTTPError) as missing_csrf:
                urlopen(self.request(path, method="DELETE"), timeout=5)
        self.assertEqual(missing_csrf.exception.code, 403)

        with (
            patch.object(
                server.AnimeRequestHandler,
                "get_current_session",
                return_value=session,
            ),
            patch("server.database.delete_curator_conversation", return_value=False)
            as delete_conversation,
        ):
            with self.assertRaises(HTTPError) as unowned:
                urlopen(
                    self.request(
                        path,
                        headers={"X-CSRF-Token": "csrf-test-token"},
                        method="DELETE",
                    ),
                    timeout=5,
                )
        self.assertEqual(unowned.exception.code, 404)
        delete_conversation.assert_called_once_with(8, "conversation-one")

        with (
            patch.object(
                server.AnimeRequestHandler,
                "get_current_session",
                return_value=session,
            ),
            patch("server.database.delete_curator_conversation", return_value=True)
            as delete_conversation,
        ):
            with urlopen(
                self.request(
                    path,
                    headers={"X-CSRF-Token": "csrf-test-token"},
                    method="DELETE",
                ),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload, {"ok": True})
        delete_conversation.assert_called_once_with(8, "conversation-one")


if __name__ == "__main__":
    unittest.main()
