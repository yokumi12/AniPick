import base64
import io
import json
import os
import unittest
from http.server import ThreadingHTTPServer
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ai_character_creator
import character_info
import server


CHARACTER = {
    "name": "윤슬",
    "role": "바다 열차의 정비사",
    "appearance": "푸른 작업복과 은빛 단발머리를 하고 있다.",
    "personality": "수줍지만 맡은 일에는 끈기가 있다.",
    "ability": "파도의 소리를 들으며 기관의 이상을 찾아낸다.",
    "backstory": "해안 마을에서 자라 열차를 고치는 일을 배웠다.",
    "quote": "고장 난 길도 다시 이어 붙이면 돼.",
}


class AiCharacterCreatorTests(unittest.TestCase):
    def test_creates_a_structured_original_character(self):
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("claude", "private-test-key", "test-model"),
            ),
            patch.object(
                character_info,
                "_provider_text",
                return_value=json.dumps(CHARACTER, ensure_ascii=False),
            ) as provider_text,
        ):
            result = ai_character_creator.create_character(
                "바다 위 기차를 고치는 정비사", "claude"
            )

        self.assertEqual(result["character"], CHARACTER)
        self.assertEqual(result["provider"], "claude")
        self.assertIn("기존 애니 캐릭터를 복제하지 마라", provider_text.call_args.args[3])

    def test_validates_prompt_and_provider_output(self):
        with self.assertRaises(ai_character_creator.AiCharacterCreationError) as empty:
            ai_character_creator.create_character(" ")
        self.assertEqual(empty.exception.status, 400)

        with self.assertRaises(ai_character_creator.AiCharacterCreationError) as oversized:
            ai_character_creator.create_character(
                "가" * (ai_character_creator.MAX_PROMPT_LENGTH + 1)
            )
        self.assertEqual(oversized.exception.status, 400)

        invalid = {**CHARACTER, "name": ""}
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "test-key", "test-model"),
            ),
            patch.object(
                character_info,
                "_provider_text",
                return_value=json.dumps(invalid, ensure_ascii=False),
            ),
        ):
            with self.assertRaises(ai_character_creator.AiCharacterCreationError):
                ai_character_creator.create_character("마법 학교의 학생")

    def test_generates_and_validates_an_original_png(self):
        png = b"\x89PNG\r\n\x1a\n"
        image_b64 = base64.b64encode(png).decode("ascii")
        response = io.BytesIO(
            json.dumps({"data": [{"b64_json": image_b64}]}).encode("utf-8")
        )
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "private-test-key", "gpt-4o-mini"),
            ),
            patch.object(ai_character_creator, "urlopen", return_value=response) as openai_call,
        ):
            result = ai_character_creator.create_character_image(
                "바다 위 기차의 정비사", CHARACTER
            )

        self.assertEqual(result["image"], image_b64)
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(result["model"], "gpt-image-2.5-sunburst")
        request = openai_call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/images/generations")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-test-key")
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request_body["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(request_body["output_format"], "png")

    def test_generates_png_with_gemini_image_api(self):
        image_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
        response = io.BytesIO(
            json.dumps(
                {
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {
                                    "type": "image",
                                    "mime_type": "image/png",
                                    "data": image_b64,
                                }
                            ],
                        }
                    ]
                }
            ).encode("utf-8")
        )
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("gemini", "private-gemini-key", "gemini-3.8-flash"),
            ),
            patch.object(
                ai_character_creator, "urlopen", return_value=response
            ) as gemini_call,
        ):
            result = ai_character_creator.create_character_image(
                "바다 위 기차의 정비사", CHARACTER, image_provider="gemini"
            )

        self.assertEqual(result["image"], image_b64)
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(result["model"], "gemini-nano-banana-2.1")
        request = gemini_call.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/interactions",
        )
        self.assertEqual(request.get_header("X-goog-api-key"), "private-gemini-key")
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request_body["model"], "gemini-nano-banana-2.1")
        self.assertFalse(request_body["store"])
        self.assertIn("Create one original anime-style character portrait", request_body["input"][0]["text"])

    def test_image_creation_rejects_claude_as_image_provider(self):
        with self.assertRaises(ai_character_creator.AiCharacterImageError) as raised:
            ai_character_creator.create_character_image(
                "캐릭터", CHARACTER, image_provider="claude"
            )
        self.assertEqual(raised.exception.status, 400)
        self.assertIn("Gemini", str(raised.exception))

    def test_retired_image_model_error_explains_how_to_update(self):
        error_body = io.BytesIO(
            json.dumps(
                {
                    "error": {
                        "message": "Model gpt-image-1 is no longer available.",
                        "code": "model_not_found",
                        "param": "model",
                    }
                }
            ).encode()
        )
        error = HTTPError(
            "https://api.openai.com/v1/images/generations",
            400,
            "bad request",
            {},
            error_body,
        )
        with (
            patch.dict(os.environ, {"ANIPICK_OPENAI_IMAGE_MODEL": "gpt-image-1"}),
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "private-test-key", "gpt-4o-mini"),
            ),
            patch.object(ai_character_creator, "urlopen", side_effect=error),
        ):
            with self.assertRaises(
                ai_character_creator.AiCharacterImageError
            ) as raised:
                ai_character_creator.create_character_image("캐릭터", CHARACTER)

        self.assertIn("지원이 종료됐어요", str(raised.exception))
        self.assertIn("gpt-image-2.5-sunburst", str(raised.exception))
        self.assertNotIn("private-test-key", str(raised.exception))

    def test_bad_request_shows_only_safe_error_code_and_parameter(self):
        error_body = io.BytesIO(
            json.dumps(
                {
                    "error": {
                        "message": "Do not expose private prompt content.",
                        "code": "invalid_value",
                        "param": "quality",
                    }
                }
            ).encode()
        )
        error = HTTPError(
            "https://api.openai.com/v1/images/generations",
            400,
            "bad request",
            {},
            error_body,
        )
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "private-test-key", "gpt-4o-mini"),
            ),
            patch.object(ai_character_creator, "urlopen", side_effect=error),
        ):
            with self.assertRaises(
                ai_character_creator.AiCharacterImageError
            ) as raised:
                ai_character_creator.create_character_image("캐릭터", CHARACTER)

        self.assertIn("invalid_value", str(raised.exception))
        self.assertIn("quality", str(raised.exception))
        self.assertNotIn("private prompt content", str(raised.exception))

    def test_image_creation_rejects_bad_input_and_bad_image_data(self):
        with self.assertRaises(ai_character_creator.AiCharacterImageError) as empty:
            ai_character_creator.create_character_image(" ", CHARACTER)
        self.assertEqual(empty.exception.status, 400)

        with self.assertRaises(ai_character_creator.AiCharacterImageError) as invalid_character:
            ai_character_creator.create_character_image("캐릭터", {"name": "불완전"})
        self.assertEqual(invalid_character.exception.status, 400)

        response = io.BytesIO(
            json.dumps({"data": [{"b64_json": base64.b64encode(b"not png").decode()}]}).encode()
        )
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "private-test-key", "gpt-4o-mini"),
            ),
            patch.object(ai_character_creator, "urlopen", return_value=response),
        ):
            with self.assertRaises(ai_character_creator.AiCharacterImageError):
                ai_character_creator.create_character_image("캐릭터", CHARACTER)

        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "private-test-key", "gpt-4o-mini"),
            ),
            patch.object(
                ai_character_creator,
                "urlopen",
                side_effect=HTTPError(
                    "https://api.openai.com/v1/images/generations",
                    429,
                    "rate limited",
                    {},
                    None,
                ),
            ),
        ):
            with self.assertRaises(ai_character_creator.AiCharacterImageError) as limited:
                ai_character_creator.create_character_image("캐릭터", CHARACTER)
        self.assertEqual(limited.exception.status, 429)

    def test_image_authentication_error_points_to_account_key_settings(self):
        error = HTTPError(
            "https://api.openai.com/v1/images/generations",
            401,
            "unauthorized",
            {},
            None,
        )
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "private-test-key", "gpt-4o-mini"),
            ),
            patch.object(ai_character_creator, "urlopen", side_effect=error),
        ):
            with self.assertRaises(
                ai_character_creator.AiCharacterImageError
            ) as raised:
                ai_character_creator.create_character_image("캐릭터", CHARACTER)

        self.assertIn("OpenAI", str(raised.exception))
        self.assertIn("로그인 계정에 저장한", str(raised.exception))
        self.assertNotIn("private-test-key", str(raised.exception))


class AiCharacterCreatorApiTests(unittest.TestCase):
    def setUp(self):
        server.ai_character_attempts.clear()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.AnimeRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/ai/character"
        self.image_url = (
            f"http://127.0.0.1:{self.server.server_port}/api/ai/character/image"
        )
        server.ai_character_image_attempts.clear()

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

    def image_request(self, body):
        return Request(
            self.image_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
            },
            method="POST",
        )

    def test_character_endpoint_returns_structured_profile(self):
        expected = {"character": CHARACTER, "provider": "claude"}
        with patch(
            "server.ai_character_creator.create_character", return_value=expected
        ) as create:
            with urlopen(
                self.request({"prompt": "바다 위 기차의 정비사"}), timeout=5
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        create.assert_called_once_with("바다 위 기차의 정비사", provider=None)

    def test_character_endpoint_validates_body(self):
        with patch("server.ai_character_creator.create_character") as create:
            with self.assertRaises(HTTPError) as raised:
                urlopen(self.request({"prompt": ""}), timeout=5)

        self.assertEqual(raised.exception.code, 400)
        create.assert_not_called()

    def test_image_endpoint_returns_generated_image(self):
        expected = {
            "image": "cG5n",
            "mime_type": "image/png",
            "model": "gpt-image-2.5-sunburst",
        }
        with patch(
            "server.ai_character_creator.create_character_image", return_value=expected
        ) as create:
            with urlopen(
                self.image_request({"prompt": "바다 열차의 정비사", "character": CHARACTER}),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        create.assert_called_once_with(
            "바다 열차의 정비사", CHARACTER, image_provider="openai"
        )

    def test_image_endpoint_accepts_gemini_and_rejects_claude(self):
        expected = {"image": "cG5n", "mime_type": "image/png", "model": "gemini"}
        with patch(
            "server.ai_character_creator.create_character_image", return_value=expected
        ) as create:
            with urlopen(
                self.image_request(
                    {
                        "prompt": "바다 열차의 정비사",
                        "character": CHARACTER,
                        "image_provider": "gemini",
                    }
                ),
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        create.assert_called_once_with(
            "바다 열차의 정비사", CHARACTER, image_provider="gemini"
        )

        with patch("server.ai_character_creator.create_character_image") as create:
            with self.assertRaises(HTTPError) as unsupported:
                urlopen(
                    self.image_request(
                        {
                            "prompt": "바다 열차의 정비사",
                            "character": CHARACTER,
                            "image_provider": "claude",
                        }
                    ),
                    timeout=5,
                )
        self.assertEqual(unsupported.exception.code, 400)
        create.assert_not_called()

    def test_image_endpoint_validates_character_and_limits_paid_calls(self):
        with patch("server.ai_character_creator.create_character_image") as create:
            with self.assertRaises(HTTPError) as invalid:
                urlopen(
                    self.image_request({"prompt": "캐릭터", "character": {"name": "잘못됨"}}),
                    timeout=5,
                )
            self.assertEqual(invalid.exception.code, 400)
            create.assert_not_called()

            create.return_value = {"image": "cG5n", "mime_type": "image/png"}
            for _ in range(server.AI_CHARACTER_IMAGE_RATE_LIMIT_ATTEMPTS):
                with urlopen(
                    self.image_request({"prompt": "캐릭터", "character": CHARACTER}),
                    timeout=5,
                ):
                    pass
            with self.assertRaises(HTTPError) as limited:
                urlopen(
                    self.image_request({"prompt": "캐릭터", "character": CHARACTER}),
                    timeout=5,
                )

        self.assertEqual(limited.exception.code, 429)
        self.assertEqual(create.call_count, server.AI_CHARACTER_IMAGE_RATE_LIMIT_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
