import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import character_info


class CharacterInfoTests(unittest.TestCase):
    def setUp(self):
        self._cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache_dir.cleanup)
        cache_patch = patch.object(
            character_info, "CACHE_PATH", Path(self._cache_dir.name) / "cache.json"
        )
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.anime = {
            "id": "7442",
            "title": "Attack on Titan",
            "titles": {"en": "Attack on Titan"},
        }
        self.media = {
            "siteUrl": "https://anilist.co/anime/16498",
            "title": {"english": "Attack on Titan", "romaji": "Shingeki no Kyojin"},
            "characters": {
                "edges": [
                    {
                        "role": "MAIN",
                        "node": {
                            "id": 42,
                            "name": {"full": "Eren Yeager", "native": "エレン・イェーガー"},
                            "image": {
                                "large": "https://s4.anilist.co/file/anilistcdn/character/42.jpg"
                            },
                            "description": "A member of the Survey Corps.",
                            "siteUrl": "https://anilist.co/character/42",
                        },
                    }
                ]
            },
        }

    def test_get_characters_translates_and_preserves_source_data(self):
        translated = json.dumps(
            {
                "characters": [
                    {
                        "id": "42",
                        "name_ko": "에렌 예거",
                        "explanation_ko": "조사병단의 일원입니다.",
                    }
                ]
            },
            ensure_ascii=False,
        )
        with (
            patch.object(character_info.database, "get_catalog", return_value=[self.anime]),
            patch.object(character_info, "_request_anilist", return_value=self.media) as request,
            patch.object(
                character_info,
                "_provider_config",
                return_value=("claude", "test-key", "test-model"),
            ),
            patch.object(character_info, "_provider_text", return_value=translated),
            patch.dict(character_info._cache, {}, clear=True),
        ):
            result = character_info.get_characters("7442")

        request.assert_called_once_with("Attack on Titan")
        self.assertEqual(result["provider"], "claude")
        self.assertEqual(result["source"], "AniList")
        self.assertEqual(result["characters"][0]["name_ko"], "에렌 예거")
        self.assertEqual(
            result["characters"][0]["explanation_ko"], "조사병단의 일원입니다."
        )
        self.assertEqual(result["characters"][0]["native_name"], "エレン・イェーガー")
        self.assertEqual(
            result["characters"][0]["image_url"],
            "https://s4.anilist.co/file/anilistcdn/character/42.jpg",
        )

    def test_get_characters_rejects_anime_not_in_local_catalog(self):
        with patch.object(character_info.database, "get_catalog", return_value=[]):
            with self.assertRaises(character_info.CharacterInfoError) as raised:
                character_info.get_characters("missing")

        self.assertEqual(raised.exception.status, 404)

    def test_missing_provider_key_returns_clear_error(self):
        with (
            patch.dict(
                os.environ,
                {"ANIPICK_AI_PROVIDER": "claude"},
                clear=True,
            ),
        ):
            with self.assertRaises(character_info.CharacterInfoError) as raised:
                character_info._provider_config()

        self.assertEqual(raised.exception.status, 503)
        self.assertIn("ANTHROPIC_API_KEY", str(raised.exception))

    def test_explicit_provider_uses_its_own_environment_key(self):
        with patch.dict(
            os.environ,
            {
                "ANIPICK_AI_PROVIDER": "claude",
                "OPENAI_API_KEY": "openai-test-key",
                "ANIPICK_OPENAI_MODEL": "test-openai-model",
            },
            clear=True,
        ):
            self.assertEqual(
                character_info._provider_config("openai"),
                ("openai", "openai-test-key", "test-openai-model"),
            )

    def test_account_api_key_overrides_server_key_only_inside_request_context(self):
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "server-key"},
            clear=True,
        ):
            with character_info.use_user_api_keys({"openai": "account-key"}):
                account_config = character_info._provider_config("openai")
            server_config = character_info._provider_config("openai")

        self.assertEqual(account_config[1], "account-key")
        self.assertEqual(server_config[1], "server-key")

    def test_gemini_model_setting_accepts_models_prefix(self):
        with patch.dict(
            os.environ,
            {
                "ANIPICK_GEMINI_MODEL": "models/gemini-2.5-flash",
                "GEMINI_API_KEY": "test-gemini-key",
            },
            clear=True,
        ):
            self.assertEqual(
                character_info._provider_config("gemini"),
                ("gemini", "test-gemini-key", "gemini-2.5-flash"),
            )

    def test_gemini_default_model_uses_the_supported_flash_model(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "test-gemini-key"},
            clear=True,
        ):
            self.assertEqual(
                character_info._provider_config("gemini"),
                ("gemini", "test-gemini-key", "gemini-3.8-flash"),
            )

    def test_provider_options_report_key_presence_without_exposing_keys(self):
        with patch.dict(
            os.environ,
            {
                "ANIPICK_AI_PROVIDER": "claude",
                "ANTHROPIC_API_KEY": "claude-test-secret",
                "GEMINI_API_KEY": "gemini-test-secret",
            },
            clear=True,
        ):
            result = character_info.get_provider_options()

        configured = {
            provider["id"]: provider["configured"]
            for provider in result["providers"]
        }
        self.assertEqual(result["default_provider"], "claude")
        self.assertEqual(
            configured,
            {"openai": False, "gemini": True, "claude": True},
        )
        self.assertNotIn("claude-test-secret", json.dumps(result))
        self.assertNotIn("gemini-test-secret", json.dumps(result))

    def test_provider_placeholders_are_not_reported_as_configured_keys(self):
        with patch.dict(
            os.environ,
            {
                "ANIPICK_AI_PROVIDER": "claude",
                "ANTHROPIC_API_KEY": "your_ANTHROPIC_API_KEY_value",
                "OPENAI_API_KEY": "여기에_발급한_키",
            },
            clear=True,
        ):
            result = character_info.get_provider_options()
            with self.assertRaises(character_info.CharacterInfoError) as raised:
                character_info._provider_config("claude")

        configured = {
            provider["id"]: provider["configured"]
            for provider in result["providers"]
        }
        self.assertEqual(configured, {"openai": False, "gemini": False, "claude": False})
        self.assertEqual(raised.exception.status, 503)

    def test_provider_requests_use_the_configured_api(self):
        fixtures = {
            "openai": (
                {"choices": [{"message": {"content": '{"characters":[]}'}}]},
                "https://api.openai.com/v1/chat/completions",
                "Authorization",
            ),
            "gemini": (
                {"output_text": '{"characters":[]}'},
                "https://generativelanguage.googleapis.com/v1beta/interactions",
                "x-goog-api-key",
            ),
            "claude": (
                {"content": [{"text": '{"characters":[]}'}]},
                "https://api.anthropic.com/v1/messages",
                "x-api-key",
            ),
        }
        for provider, (payload, expected_url, expected_header) in fixtures.items():
            with self.subTest(provider=provider):
                response = FakeResponse(payload)
                with patch.object(character_info, "urlopen", return_value=response) as open_url:
                    result = character_info._provider_text(
                        provider, "private-test-key", "test-model", "prompt"
                    )

                self.assertEqual(result, '{"characters":[]}')
                request = open_url.call_args.args[0]
                self.assertTrue(request.full_url.startswith(expected_url))
                if expected_header == "Authorization":
                    expected_value = "Bearer private-test-key"
                elif expected_header:
                    expected_value = "private-test-key"
                else:
                    expected_value = None
                if expected_header:
                    header_values = {
                        name.casefold(): value
                        for name, value in request.header_items()
                    }
                    self.assertEqual(
                        header_values[expected_header.casefold()], expected_value
                    )
                if provider == "gemini":
                    body = json.loads(request.data.decode("utf-8"))
                    self.assertEqual(body["model"], "test-model")
                    self.assertEqual(body["input"], "prompt")
                    self.assertFalse(body["store"])
                if provider == "claude":
                    self.assertEqual(
                        {
                            name.casefold(): value
                            for name, value in request.header_items()
                        }["anthropic-version"],
                        "2023-06-01",
                    )

    def test_gemini_404_includes_safe_provider_error_details(self):
        from io import BytesIO
        from urllib.error import HTTPError

        payload = json.dumps(
            {
                "error": {
                    "code": 404,
                    "message": "models/gemini-test was not found",
                    "status": "NOT_FOUND",
                }
            }
        ).encode("utf-8")
        error = HTTPError(
            "https://generativelanguage.googleapis.com/test",
            404,
            "Not Found",
            {},
            BytesIO(payload),
        )
        with patch.object(character_info, "urlopen", side_effect=error):
            with self.assertRaises(character_info.CharacterInfoError) as raised:
                character_info._provider_text(
                    "gemini", "private-test-key", "gemini-test", "prompt"
                )

        self.assertIn("models/gemini-test was not found", str(raised.exception))
        self.assertNotIn("private-test-key", str(raised.exception))

    def test_provider_authentication_errors_give_account_key_guidance(self):
        from urllib.error import HTTPError

        for status, expected_text in (
            (401, "저장한 키가 선택한 제공자와 일치하는지"),
            (403, "모델 사용 권한"),
        ):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://api.openai.com/v1/chat/completions",
                    status,
                    "authentication failure",
                    {},
                    None,
                )
                with patch.object(character_info, "urlopen", side_effect=error):
                    with self.assertRaises(
                        character_info.CharacterInfoError
                    ) as raised:
                        character_info._provider_text(
                            "openai", "private-test-key", "test-model", "prompt"
                        )

                self.assertIn("OpenAI", str(raised.exception))
                self.assertIn(expected_text, str(raised.exception))
                self.assertNotIn("private-test-key", str(raised.exception))

    def test_translation_rejects_missing_character_results(self):
        with (
            patch.object(
                character_info,
                "_provider_config",
                return_value=("openai", "test-key", "test-model"),
            ),
            patch.object(
                character_info, "_provider_text", return_value='{"characters":[]}'
            ),
            patch.dict(character_info._cache, {}, clear=True),
        ):
            with self.assertRaises(character_info.CharacterInfoError):
                character_info._translate_characters(
                    [
                        {
                            "id": "42",
                            "name": "Eren Yeager",
                            "description": "A member of the Survey Corps.",
                            "role": "MAIN",
                        }
                    ]
                )


    def _translated(self, name="에렌 예거", text="조사병단의 일원입니다."):
        return json.dumps(
            {"characters": [{"id": "42", "name_ko": name, "explanation_ko": text}]},
            ensure_ascii=False,
        )

    def test_clean_description_removes_spoiler_blocks_and_markup(self):
        cleaned = character_info._clean_description(
            "Brave.<br>~!He dies in the finale.!~ Loyal. ~!unterminated spoiler"
        )
        self.assertEqual(cleaned, "Brave. Loyal.")

    def test_spoilers_are_not_sent_to_the_ai_provider(self):
        self.media["characters"]["edges"][0]["node"]["description"] = (
            "A soldier. ~!Secret ending twist.!~"
        )
        with (
            patch.object(character_info.database, "get_catalog", return_value=[self.anime]),
            patch.object(character_info, "_request_anilist", return_value=self.media),
            patch.object(
                character_info, "_provider_config",
                return_value=("claude", "test-key", "test-model"),
            ),
            patch.object(
                character_info, "_provider_text", return_value=self._translated()
            ) as provider_text,
        ):
            result = character_info.get_characters("7442")

        self.assertNotIn("Secret ending twist", provider_text.call_args.args[3])
        self.assertEqual(result["characters"][0]["description"], "A soldier.")

    def test_fuzzy_neighbor_is_skipped_and_next_title_is_tried(self):
        season_two = {
            "id": "7442",
            "title": "Attack on Titan Season 2",
            "year": 2017,
            "titles": {"en": "Attack on Titan Season 2", "en_jp": "Shingeki no Kyojin 2"},
        }
        wrong = {"title": {"english": "Attack on Titan"}, "startDate": {"year": 2013}}
        right = {
            **self.media,
            "title": {"english": "Attack on Titan Season 2", "romaji": "Shingeki no Kyojin 2"},
            "startDate": {"year": 2017},
        }
        with (
            patch.object(character_info.database, "get_catalog", return_value=[season_two]),
            patch.object(
                character_info, "_request_anilist", side_effect=[wrong, right]
            ) as request,
            patch.object(
                character_info, "_provider_config",
                return_value=("claude", "test-key", "test-model"),
            ),
            patch.object(character_info, "_provider_text", return_value=self._translated()),
        ):
            result = character_info.get_characters("7442")

        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["anime_title"], "Attack on Titan Season 2")

    def test_no_matching_media_returns_404_instead_of_wrong_characters(self):
        wrong = {"title": {"english": "Completely Different Show"}, "startDate": {"year": 2001}}
        with (
            patch.object(character_info.database, "get_catalog", return_value=[self.anime]),
            patch.object(character_info, "_request_anilist", return_value=wrong),
        ):
            with self.assertRaises(character_info.CharacterInfoError) as raised:
                character_info.get_characters("7442")

        self.assertEqual(raised.exception.status, 404)

    def test_translation_is_saved_and_reused_without_an_api_key(self):
        characters = [
            {
                "id": "42",
                "name": "Eren Yeager",
                "description": "A member of the Survey Corps.",
                "role": "MAIN",
            }
        ]
        with (
            patch.object(
                character_info, "_provider_config",
                return_value=("openai", "test-key", "test-model"),
            ),
            patch.object(
                character_info, "_provider_text", return_value=self._translated()
            ) as provider_text,
            patch.dict(character_info._cache, {}, clear=True),
        ):
            first, _provider = character_info._translate_characters(characters)
        self.assertEqual(provider_text.call_count, 1)
        self.assertTrue(character_info.CACHE_PATH.exists())

        with (
            patch.object(
                character_info, "_provider_config",
                return_value=("openai", "", "test-model"),
            ),
            patch.object(character_info, "_provider_text") as second_call,
            patch.dict(character_info._cache, {}, clear=True),
        ):
            second, provider = character_info._translate_characters(characters)

        second_call.assert_not_called()
        self.assertEqual(provider, "openai")
        self.assertEqual(second[0]["name_ko"], first[0]["name_ko"])

    def test_changed_source_description_invalidates_saved_translation(self):
        base = {"id": "42", "name": "Eren Yeager", "role": "MAIN"}
        first = [{**base, "description": "A member of the Survey Corps."}]
        changed = [{**base, "description": "A soldier of the Scout Regiment."}]
        with (
            patch.object(
                character_info, "_provider_config",
                return_value=("openai", "test-key", "test-model"),
            ),
            patch.object(
                character_info, "_provider_text", return_value=self._translated()
            ) as provider_text,
            patch.dict(character_info._cache, {}, clear=True),
        ):
            character_info._translate_characters(first)
            character_info._translate_characters(changed)

        self.assertEqual(provider_text.call_count, 2)

    def test_translation_cache_is_separate_for_each_provider(self):
        characters = [
            {
                "id": "42",
                "name": "Eren Yeager",
                "description": "A member of the Survey Corps.",
                "role": "MAIN",
            }
        ]
        with (
            patch.object(
                character_info,
                "_provider_config",
                side_effect=[
                    ("openai", "openai-test-key", "test-model"),
                    ("gemini", "gemini-test-key", "test-model"),
                ],
            ),
            patch.object(
                character_info, "_provider_text", return_value=self._translated()
            ) as provider_text,
            patch.dict(character_info._cache, {}, clear=True),
        ):
            character_info._translate_characters(characters, "openai")
            character_info._translate_characters(characters, "gemini")

        self.assertEqual(provider_text.call_count, 2)

    def test_existing_provider_cache_entries_are_migrated(self):
        characters = [
            {
                "id": "42",
                "name": "Eren Yeager",
                "description": "A member of the Survey Corps.",
                "role": "MAIN",
            }
        ]
        legacy_hash = character_info._legacy_source_hash(characters)
        with character_info.CACHE_PATH.open("w", encoding="utf-8") as cache_file:
            json.dump(
                {
                    legacy_hash: {
                        "provider": "claude",
                        "model": "test-model",
                        "by_id": {
                            "42": {
                                "name_ko": "에렌 예거",
                                "explanation_ko": "조사병단의 일원입니다.",
                            }
                        },
                    }
                },
                cache_file,
            )

        with (
            patch.dict(
                os.environ,
                {
                    "ANIPICK_AI_PROVIDER": "claude",
                    "ANIPICK_CLAUDE_MODEL": "test-model",
                },
                clear=True,
            ),
            patch.object(character_info, "_provider_text") as provider_text,
            patch.dict(character_info._cache, {}, clear=True),
        ):
            result, provider = character_info._translate_characters(characters)

        provider_text.assert_not_called()
        self.assertEqual(provider, "claude")
        self.assertEqual(result[0]["name_ko"], "에렌 예거")
        migrated_cache = json.loads(character_info.CACHE_PATH.read_text(encoding="utf-8"))
        self.assertIn(
            character_info._source_hash(characters, "claude", "test-model"),
            migrated_cache,
        )


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


if __name__ == "__main__":
    unittest.main()
