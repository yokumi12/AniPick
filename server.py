import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlsplit

import airing_schedule
import api_key_store
import ai_character_creator
import ai_curator
import ai_recommender
import ai_synopsis
import ai_tagger
import character_info
import database
import recommender


HOST = os.environ.get("ANIPICK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
INDEX_PATH = database.PROJECT_ROOT / "index.html"
SESSION_COOKIE = "anipick_session"
SESSION_SECONDS = 60 * 60 * 24 * 14
PASSWORD_ITERATIONS = 600_000
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{4,20}$")
RATE_LIMIT_WINDOW = 15 * 60
RATE_LIMIT_ATTEMPTS = 8
auth_attempts = {}
auth_attempts_lock = Lock()
character_attempts = {}
character_attempts_lock = Lock()
CHARACTER_RATE_LIMIT_WINDOW = 15 * 60
CHARACTER_RATE_LIMIT_ATTEMPTS = 8
ai_recommend_attempts = {}
ai_recommend_attempts_lock = Lock()
AI_RECOMMEND_RATE_LIMIT_WINDOW = 15 * 60
AI_RECOMMEND_RATE_LIMIT_ATTEMPTS = 8
ai_synopsis_attempts = {}
ai_synopsis_attempts_lock = Lock()
AI_SYNOPSIS_RATE_LIMIT_WINDOW = 15 * 60
AI_SYNOPSIS_RATE_LIMIT_ATTEMPTS = 8
ai_tag_attempts = {}
ai_tag_attempts_lock = Lock()
AI_TAG_RATE_LIMIT_WINDOW = 15 * 60
AI_TAG_RATE_LIMIT_ATTEMPTS = 8
ai_curator_attempts = {}
ai_curator_attempts_lock = Lock()
AI_CURATOR_RATE_LIMIT_WINDOW = 15 * 60
AI_CURATOR_RATE_LIMIT_ATTEMPTS = 8
ai_character_attempts = {}
ai_character_attempts_lock = Lock()
AI_CHARACTER_RATE_LIMIT_WINDOW = 15 * 60
AI_CHARACTER_RATE_LIMIT_ATTEMPTS = 6
ai_character_image_attempts = {}
ai_character_image_attempts_lock = Lock()
AI_CHARACTER_IMAGE_RATE_LIMIT_WINDOW = 15 * 60
AI_CHARACTER_IMAGE_RATE_LIMIT_ATTEMPTS = 3
API_KEY_PROVIDERS = set(character_info.PROVIDER_SETTINGS)
API_KEY_VALUE_PATTERN = re.compile(r"^[\x21-\x7e]{1,4096}$")


def hash_session_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )


class AnimeRequestHandler(BaseHTTPRequestHandler):
    server_version = "AniPick/0.2"

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "ok"})
            return
        if not self.has_trusted_proxy():
            self.send_json(403, {"error": "허용되지 않은 요청입니다."})
            return
        if path in ("/", "/index.html"):
            self.serve_index()
        elif path == "/api/anime":
            self.send_json(200, {"items": database.get_catalog()})
        elif path == "/api/genres":
            self.send_json(200, {"items": database.get_genres()})
        elif path == "/api/schedule":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            try:
                self.send_json(200, airing_schedule.get_schedule(day))
            except airing_schedule.ScheduleError as error:
                self.send_json(error.status, {"error": str(error)})
        elif path == "/api/search":
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0].strip()
            if not query or len(query) > 100:
                self.send_json(400, {"error": "검색어는 1~100자로 입력해주세요."})
                return
            self.send_json(200, {"items": recommender.search(query)})
        elif re.fullmatch(r"/api/anime/[^/]+/similar", path):
            similar = recommender.similar(path.split("/")[3])
            if similar is None:
                self.send_json(404, {"error": "AniPick 작품 목록에서 작품을 찾지 못했어요."})
                return
            self.send_json(200, {"items": similar})
        elif path == "/api/recommendations":
            session = self.get_current_session()
            if not session:
                self.send_json(401, {"error": "취향 추천은 로그인 후 이용할 수 있어요."})
                return
            favorites = database.get_favorites(session["user_id"])
            known_genres = set(database.get_genres())
            raw_genres = parse_qs(urlsplit(self.path).query).get("genres", [""])[0]
            preferred = [
                genre for genre in raw_genres.split(",") if genre in known_genres
            ][:10]
            self.send_json(
                200,
                {
                    "items": recommender.recommend(favorites, preferred, limit=100),
                    "genre_weights": recommender.genre_weights(favorites),
                    "favorite_count": len(favorites),
                },
            )
        elif path == "/api/ai/providers":
            try:
                payload = character_info.get_provider_options()
                session = self.get_current_session()
                configured_providers = (
                    database.get_user_ai_api_key_providers(session["user_id"])
                    if session
                    else set()
                )
                for provider in payload["providers"]:
                    provider["personal_configured"] = (
                        provider["id"] in configured_providers
                    )
                    provider["configured"] = (
                        provider["configured"] or provider["personal_configured"]
                    )
                payload["authenticated"] = bool(session)
                payload["key_management_enabled"] = bool(
                    session and api_key_store.encryption_is_available()
                )
                self.send_json(200, payload)
            except api_key_store.ApiKeyStoreError as error:
                self.send_json(503, {"error": str(error)})
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
        elif path == "/api/ai/curator/conversations":
            session = self.get_current_session()
            if not session:
                self.send_json(401, {"error": "저장된 대화는 로그인 후 확인할 수 있어요."})
                return
            self.send_json(
                200,
                {"items": database.list_curator_conversations(session["user_id"])},
            )
        elif re.fullmatch(r"/api/ai/curator/conversations/[^/]+", path):
            session = self.get_current_session()
            if not session:
                self.send_json(401, {"error": "저장된 대화는 로그인 후 확인할 수 있어요."})
                return
            conversation_id = path.rsplit("/", 1)[-1]
            if len(conversation_id) > 64:
                self.send_json(400, {"error": "대화 정보를 확인해주세요."})
                return
            conversation = database.get_curator_conversation(
                session["user_id"], conversation_id
            )
            if not conversation:
                self.send_json(404, {"error": "저장된 대화를 찾지 못했어요."})
                return
            self.send_json(200, conversation)
        elif path == "/api/characters":
            query = parse_qs(urlsplit(self.path).query)
            anime_id = query.get("anime_id", [""])[0]
            provider = query.get("provider", [None])[0]
            session = self.get_current_session()
            if not anime_id or len(anime_id) > 64:
                self.send_json(400, {"error": "anime_id를 확인해주세요."})
                return
            if not self.allow_character_attempt():
                self.send_json(
                    429, {"error": "캐릭터 정보 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            try:
                with self.use_account_api_keys(session):
                    if provider is None:
                        result = character_info.get_characters(anime_id)
                    else:
                        result = character_info.get_characters(
                            anime_id, provider=provider
                        )
                self.send_json(
                    200,
                    result,
                )
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
        elif path == "/api/me":
            session = self.get_current_session()
            if not session:
                self.send_json(200, {"user": None, "csrf_token": None})
                return
            self.send_json(
                200,
                {
                    "user": {"username": session["username"]},
                    "csrf_token": session["csrf_token"],
                },
            )
        elif path == "/api/favorites":
            session = self.get_current_session()
            if not session:
                self.send_json(401, {"error": "찜 목록을 보려면 로그인해주세요."})
                return
            self.send_json(
                200, {"items": database.get_favorites(session["user_id"])}
            )
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self.has_trusted_proxy() or not self.has_same_origin():
            self.send_json(403, {"error": "허용되지 않은 요청입니다."})
            return

        if path == "/api/ai/keys":
            session = self.get_current_session()
            if not session:
                self.send_json(401, {"error": "API 키를 저장하려면 로그인해주세요."})
                return
            if not self.has_valid_csrf_token(session):
                self.send_json(403, {"error": "요청을 확인할 수 없습니다. 새로고침해주세요."})
                return
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "API 키 설정 형식이 올바르지 않아요."})
                return
            if set(body) == {"delete"}:
                provider = body["delete"]
                if not isinstance(provider, str) or provider not in API_KEY_PROVIDERS:
                    self.send_json(400, {"error": "삭제할 AI 제공자를 확인해주세요."})
                    return
                database.delete_user_ai_api_key(session["user_id"], provider)
                self.send_json(200, {"ok": True})
                return
            if set(body) - API_KEY_PROVIDERS:
                self.send_json(400, {"error": "API 키 설정 형식이 올바르지 않아요."})
                return
            api_keys = {}
            for provider, value in body.items():
                if not isinstance(value, str):
                    self.send_json(400, {"error": "API 키는 문자열로 입력해주세요."})
                    return
                value = value.strip()
                if value and not API_KEY_VALUE_PATTERN.fullmatch(value):
                    self.send_json(
                        400,
                        {"error": "API 키는 공백이나 줄바꿈 없이 입력해주세요."},
                    )
                    return
                if value:
                    api_keys[provider] = value
            if not api_keys:
                self.send_json(400, {"error": "저장할 API 키를 입력해주세요."})
                return
            try:
                encrypted_keys = {
                    provider: api_key_store.encrypt_api_key(value)
                    for provider, value in api_keys.items()
                }
                database.save_user_ai_api_keys(
                    session["user_id"], encrypted_keys
                )
                configured_providers = database.get_user_ai_api_key_providers(
                    session["user_id"]
                )
            except api_key_store.ApiKeyStoreError as error:
                self.send_json(
                    503,
                    {"error": str(error)},
                )
                return
            self.send_json(
                200,
                {
                    "ok": True,
                    "providers": [
                        {
                            "id": provider,
                            "personal_configured": provider in configured_providers,
                        }
                        for provider in sorted(API_KEY_PROVIDERS)
                    ],
                },
            )
            return

        if path == "/api/ai/recommend":
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "요청 형식이 올바르지 않아요."})
                return
            query = body.get("query")
            provider = body.get("provider")
            if (
                not isinstance(query, str)
                or not query.strip()
                or len(query.strip()) > ai_recommender.MAX_QUERY_LENGTH
            ):
                self.send_json(
                    400,
                    {
                        "error": (
                            "원하는 애니 내용을 1~"
                            f"{ai_recommender.MAX_QUERY_LENGTH}자로 입력해주세요."
                        )
                    },
                )
                return
            if provider is not None and (
                not isinstance(provider, str)
                or provider not in character_info.PROVIDER_SETTINGS
            ):
                self.send_json(400, {"error": "AI 제공자 설정을 확인해주세요."})
                return
            if not self.allow_ai_recommend_attempt():
                self.send_json(
                    429, {"error": "AI 추천 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            try:
                session = self.get_current_session()
                with self.use_account_api_keys(session):
                    result = ai_recommender.recommend(query, provider=provider)
                self.send_json(200, result)
            except ai_recommender.AiRecommendationError as error:
                self.send_json(error.status, {"error": str(error)})
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
            return

        if path == "/api/ai/synopsis":
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "요청 형식이 올바르지 않아요."})
                return
            anime_id = body.get("anime_id")
            provider = body.get("provider")
            if (
                not isinstance(anime_id, str)
                or not anime_id.strip()
                or len(anime_id) > ai_synopsis.MAX_ANIME_ID_LENGTH
            ):
                self.send_json(400, {"error": "작품 정보를 확인해주세요."})
                return
            if provider is not None and (
                not isinstance(provider, str)
                or provider not in character_info.PROVIDER_SETTINGS
            ):
                self.send_json(400, {"error": "AI 제공자 설정을 확인해주세요."})
                return
            if not self.allow_ai_synopsis_attempt():
                self.send_json(
                    429, {"error": "AI 요약 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            try:
                session = self.get_current_session()
                with self.use_account_api_keys(session):
                    result = ai_synopsis.summarize(anime_id, provider=provider)
                self.send_json(200, result)
            except ai_synopsis.AiSynopsisError as error:
                self.send_json(error.status, {"error": str(error)})
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
            return

        if path == "/api/ai/tags":
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "요청 형식이 올바르지 않아요."})
                return
            anime_id = body.get("anime_id")
            provider = body.get("provider")
            if (
                not isinstance(anime_id, str)
                or not anime_id.strip()
                or len(anime_id) > ai_tagger.MAX_ANIME_ID_LENGTH
            ):
                self.send_json(400, {"error": "작품 정보를 확인해주세요."})
                return
            if provider is not None and (
                not isinstance(provider, str)
                or provider not in character_info.PROVIDER_SETTINGS
            ):
                self.send_json(400, {"error": "AI 제공자 설정을 확인해주세요."})
                return
            if not self.allow_ai_tag_attempt():
                self.send_json(
                    429, {"error": "AI 태그 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            try:
                session = self.get_current_session()
                with self.use_account_api_keys(session):
                    result = ai_tagger.tag_anime(anime_id, provider=provider)
                self.send_json(200, result)
            except ai_tagger.AiTaggingError as error:
                self.send_json(error.status, {"error": str(error)})
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
            return

        if path == "/api/ai/curator":
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "요청 형식이 올바르지 않아요."})
                return
            message = body.get("message")
            provider = body.get("provider")
            conversation_id = body.get("conversation_id")
            if (
                not isinstance(message, str)
                or not message.strip()
                or len(message.strip()) > ai_curator.MAX_MESSAGE_LENGTH
            ):
                self.send_json(
                    400,
                    {
                        "error": (
                            "메시지는 1~"
                            f"{ai_curator.MAX_MESSAGE_LENGTH}자로 입력해주세요."
                        )
                    },
                )
                return
            if provider is not None and (
                not isinstance(provider, str)
                or provider not in character_info.PROVIDER_SETTINGS
            ):
                self.send_json(400, {"error": "AI 제공자 설정을 확인해주세요."})
                return
            if conversation_id is not None and (
                not isinstance(conversation_id, str)
                or not conversation_id
                or len(conversation_id) > 64
            ):
                self.send_json(400, {"error": "대화 정보를 확인해주세요."})
                return

            session = self.get_current_session()
            if session and not self.has_valid_csrf_token(session):
                self.send_json(403, {"error": "요청을 확인할 수 없습니다. 새로고침해주세요."})
                return
            if not self.allow_ai_curator_attempt():
                self.send_json(
                    429, {"error": "큐레이터 대화 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return

            if session:
                conversation = None
                if conversation_id:
                    conversation = database.get_curator_conversation(
                        session["user_id"], conversation_id
                    )
                    if not conversation:
                        self.send_json(404, {"error": "저장된 대화를 찾지 못했어요."})
                        return
                history = [
                    {
                        "role": item["role"],
                        "content": item["content"][: ai_curator.MAX_HISTORY_MESSAGE_LENGTH],
                        "recommendations": item.get("recommendations", [])[
                            : ai_curator.MAX_RECOMMENDATIONS
                        ],
                    }
                    for item in (conversation or {}).get("messages", [])
                ][-ai_curator.MAX_HISTORY_MESSAGES :]
            else:
                if conversation_id is not None:
                    self.send_json(400, {"error": "로그인 후 대화를 저장할 수 있어요."})
                    return
                history = body.get("history", [])

            try:
                with self.use_account_api_keys(session):
                    result = ai_curator.respond(message, history, provider=provider)
            except ai_curator.AiCuratorError as error:
                self.send_json(error.status, {"error": str(error)})
                return
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
                return

            if session:
                conversation_id = conversation_id or secrets.token_urlsafe(24)
                database.save_curator_turn(
                    session["user_id"], conversation_id, message.strip(), result
                )
            else:
                conversation_id = None
            self.send_json(
                200,
                {
                    **result,
                    "conversation_id": conversation_id,
                },
            )
            return

        if path == "/api/ai/character":
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "요청 형식이 올바르지 않아요."})
                return
            prompt = body.get("prompt")
            provider = body.get("provider")
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt.strip()) > ai_character_creator.MAX_PROMPT_LENGTH
            ):
                self.send_json(
                    400,
                    {
                        "error": (
                            "캐릭터 구상은 1~"
                            f"{ai_character_creator.MAX_PROMPT_LENGTH}자로 입력해주세요."
                        )
                    },
                )
                return
            if provider is not None and (
                not isinstance(provider, str)
                or provider not in character_info.PROVIDER_SETTINGS
            ):
                self.send_json(400, {"error": "AI 제공자 설정을 확인해주세요."})
                return
            if not self.allow_ai_character_attempt():
                self.send_json(
                    429, {"error": "캐릭터 창작 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            try:
                session = self.get_current_session()
                with self.use_account_api_keys(session):
                    result = ai_character_creator.create_character(
                        prompt, provider=provider
                    )
                self.send_json(200, result)
            except ai_character_creator.AiCharacterCreationError as error:
                self.send_json(error.status, {"error": str(error)})
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
            return

        if path == "/api/ai/character/image":
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_json(400, {"error": "요청 형식이 올바르지 않아요."})
                return
            prompt = body.get("prompt")
            character = body.get("character")
            image_provider = body.get("image_provider", "openai")
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt.strip()) > ai_character_creator.MAX_PROMPT_LENGTH
            ):
                self.send_json(
                    400,
                    {
                        "error": (
                            "캐릭터 구상은 1~"
                            f"{ai_character_creator.MAX_PROMPT_LENGTH}자로 입력해주세요."
                        )
                    },
                )
                return
            try:
                character = ai_character_creator._validate_character(character)
            except ai_character_creator.AiCharacterCreationError as error:
                self.send_json(400, {"error": str(error)})
                return
            if image_provider not in ("openai", "gemini"):
                self.send_json(
                    400,
                    {"error": "이미지 생성은 GPT(OpenAI) 또는 Gemini를 선택해주세요."},
                )
                return
            if not self.allow_ai_character_image_attempt():
                self.send_json(
                    429, {"error": "이미지 생성 요청이 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            try:
                session = self.get_current_session()
                with self.use_account_api_keys(session):
                    result = ai_character_creator.create_character_image(
                        prompt, character, image_provider=image_provider
                    )
                self.send_json(200, result)
            except ai_character_creator.AiCharacterImageError as error:
                self.send_json(error.status, {"error": str(error)})
            except character_info.CharacterInfoError as error:
                self.send_json(error.status, {"error": str(error)})
            return

        if path not in (
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/logout",
            "/api/auth/delete",
            "/api/favorites",
        ):
            self.send_json(404, {"error": "Not found"})
            return

        if path in ("/api/auth/register", "/api/auth/login"):
            body = self.read_json_body()
            if body is None:
                return
            if not self.allow_auth_attempt(path):
                self.send_json(
                    429, {"error": "요청이 너무 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            if path == "/api/auth/register":
                self.register(body)
            else:
                self.login(body)
            return

        session = self.get_current_session()
        if not session:
            self.send_json(401, {"error": "로그인이 필요합니다."})
            return
        if not self.has_valid_csrf_token(session):
            self.send_json(403, {"error": "요청을 확인할 수 없습니다. 새로고침해주세요."})
            return

        if path == "/api/auth/logout":
            token_hash = self.request_session_token_hash()
            if token_hash:
                database.delete_session(token_hash)
            self.send_json(
                200,
                {"ok": True},
                extra_headers=[("Set-Cookie", self.expired_session_cookie())],
            )
            return

        if path == "/api/auth/delete":
            if not self.allow_auth_attempt(path):
                self.send_json(
                    429, {"error": "요청이 너무 많아요. 잠시 후 다시 시도해주세요."}
                )
                return
            body = self.read_json_body()
            if body is None:
                return
            password = body.get("password") if isinstance(body, dict) else None
            if not isinstance(password, str) or not password:
                self.send_json(400, {"error": "계정 삭제를 위해 비밀번호를 입력해주세요."})
                return
            user = database.get_user_by_username(session["username"])
            if (
                not user
                or len(password) > 128
                or not secrets.compare_digest(
                    hash_password(password, user["password_salt"]),
                    user["password_hash"],
                )
            ):
                self.send_json(403, {"error": "비밀번호가 일치하지 않아요."})
                return
            if not database.delete_account(session["user_id"]):
                self.send_json(404, {"error": "계정을 찾을 수 없습니다."})
                return
            self.send_json(
                200,
                {"ok": True},
                extra_headers=[("Set-Cookie", self.expired_session_cookie())],
            )
            return

        if path != "/api/favorites":
            self.send_json(404, {"error": "Not found"})
            return

        body = self.read_json_body()
        if body is None:
            return
        anime_id = body.get("anime_id") if isinstance(body, dict) else None
        saved = body.get("saved") if isinstance(body, dict) else None
        if not isinstance(anime_id, str) or not isinstance(saved, bool):
            self.send_json(400, {"error": "anime_id and boolean saved are required"})
            return
        if not database.set_favorite(session["user_id"], anime_id, saved):
            self.send_json(404, {"error": "Anime not found"})
            return

        self.send_json(200, {"anime_id": anime_id, "saved": saved})

    def do_DELETE(self):
        if not self.has_trusted_proxy() or not self.has_same_origin():
            self.send_json(403, {"error": "허용되지 않은 요청입니다."})
            return
        match = re.fullmatch(
            r"/api/ai/curator/conversations/([^/]+)", urlsplit(self.path).path
        )
        if not match:
            self.send_json(404, {"error": "Not found"})
            return
        session = self.get_current_session()
        if not session:
            self.send_json(401, {"error": "저장된 대화는 로그인 후 삭제할 수 있어요."})
            return
        if not self.has_valid_csrf_token(session):
            self.send_json(403, {"error": "요청을 확인할 수 없습니다. 새로고침해주세요."})
            return
        conversation_id = match.group(1)
        if len(conversation_id) > 64:
            self.send_json(400, {"error": "대화 정보를 확인해주세요."})
            return
        if not database.delete_curator_conversation(
            session["user_id"], conversation_id
        ):
            self.send_json(404, {"error": "저장된 대화를 찾지 못했어요."})
            return
        self.send_json(200, {"ok": True})

    def register(self, body):
        username = body.get("username") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(username, str) or not isinstance(password, str):
            self.send_json(400, {"error": "아이디와 비밀번호를 입력해주세요."})
            return

        username = username.strip()
        if not USERNAME_PATTERN.fullmatch(username):
            self.send_json(
                400, {"error": "아이디는 영문, 숫자, 밑줄로 4~20자 입력해주세요."}
            )
            return
        if len(password) < 8 or len(password) > 128:
            self.send_json(
                400, {"error": "비밀번호는 8자 이상 128자 이하로 입력해주세요."}
            )
            return

        salt = secrets.token_bytes(16)
        try:
            user_id = database.create_user(
                username, salt, hash_password(password, salt)
            )
        except database.integrity_error_type():
            self.send_json(409, {"error": "이미 사용 중인 아이디입니다."})
            return
        self.create_authenticated_session(user_id, username)

    def login(self, body):
        username = body.get("username") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(username, str) or not isinstance(password, str):
            self.send_json(400, {"error": "아이디와 비밀번호를 입력해주세요."})
            return

        user = database.get_user_by_username(username.strip())
        if not user:
            self.send_json(401, {"error": "아이디 또는 비밀번호를 확인해주세요."})
            return
        if len(password) > 128 or not secrets.compare_digest(
            hash_password(password, user["password_salt"]), user["password_hash"]
        ):
            self.send_json(401, {"error": "아이디 또는 비밀번호를 확인해주세요."})
            return
        self.create_authenticated_session(user["id"], user["username"])

    def create_authenticated_session(self, user_id, username):
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + SESSION_SECONDS
        database.create_session(
            hash_session_token(token), user_id, csrf_token, expires_at
        )
        self.send_json(
            200,
            {"user": {"username": username}, "csrf_token": csrf_token},
            extra_headers=[("Set-Cookie", self.session_cookie(token))],
        )

    def get_current_session(self):
        token_hash = self.request_session_token_hash()
        return database.get_session(token_hash) if token_hash else None

    def request_session_token_hash(self):
        cookie_header = self.headers.get("Cookie", "")
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
        except CookieError:
            return None
        morsel = cookies.get(SESSION_COOKIE)
        if not morsel or not morsel.value:
            return None
        return hash_session_token(morsel.value)

    def has_valid_csrf_token(self, session):
        supplied = self.headers.get("X-CSRF-Token", "")
        expected = session["csrf_token"]
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def has_same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed_origin = urlsplit(origin)
        public_origin = os.environ.get("ANIPICK_PUBLIC_ORIGIN", "").strip()
        if public_origin:
            parsed_public_origin = urlsplit(public_origin)
            return (
                parsed_origin.scheme == parsed_public_origin.scheme
                and parsed_origin.netloc.lower()
                == parsed_public_origin.netloc.lower()
                and parsed_origin.path in ("", "/")
                and not parsed_origin.query
                and not parsed_origin.fragment
            )
        host = self.headers.get("Host", "").lower()
        return parsed_origin.netloc.lower() == host and parsed_origin.scheme in (
            "http",
            "https",
        )

    def has_trusted_proxy(self):
        expected_secret = os.environ.get("ANIPICK_CLOUDFRONT_ORIGIN_SECRET", "")
        if not expected_secret:
            return os.environ.get("ANIPICK_ENV", "").strip().lower() != "production"
        supplied_secret = self.headers.get("X-AniPick-Origin-Secret", "")
        return bool(supplied_secret) and secrets.compare_digest(
            supplied_secret, expected_secret
        )

    def rate_limit_client_ip(self):
        if (
            os.environ.get("ANIPICK_CLOUDFRONT_ORIGIN_SECRET")
            and self.has_trusted_proxy()
        ):
            forwarded = (
                self.headers.get("X-Forwarded-For", "").rsplit(",", 1)[-1].strip()
            )
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return self.client_address[0]

    @contextmanager
    def use_account_api_keys(self, session):
        if not session:
            with character_info.use_user_api_keys({}):
                yield
            return
        try:
            api_keys = api_key_store.get_user_api_keys(session["user_id"])
        except api_key_store.ApiKeyStoreError as error:
            raise character_info.CharacterInfoError(str(error), 503) from error
        with character_info.use_user_api_keys(api_keys):
            yield

    def allow_auth_attempt(self, path):
        now = time.monotonic()
        key = (self.rate_limit_client_ip(), path)
        with auth_attempts_lock:
            attempts = [
                timestamp
                for timestamp in auth_attempts.get(key, [])
                if now - timestamp < RATE_LIMIT_WINDOW
            ]
            if len(attempts) >= RATE_LIMIT_ATTEMPTS:
                auth_attempts[key] = attempts
                return False
            attempts.append(now)
            auth_attempts[key] = attempts
            if len(auth_attempts) > 1000:
                for old_key in list(auth_attempts):
                    auth_attempts[old_key] = [
                        timestamp
                        for timestamp in auth_attempts[old_key]
                        if now - timestamp < RATE_LIMIT_WINDOW
                    ]
                    if not auth_attempts[old_key]:
                        del auth_attempts[old_key]
            return True

    def allow_character_attempt(self):
        now = time.monotonic()
        key = self.rate_limit_client_ip()
        with character_attempts_lock:
            attempts = [
                timestamp
                for timestamp in character_attempts.get(key, [])
                if now - timestamp < CHARACTER_RATE_LIMIT_WINDOW
            ]
            if len(attempts) >= CHARACTER_RATE_LIMIT_ATTEMPTS:
                character_attempts[key] = attempts
                return False
            attempts.append(now)
            character_attempts[key] = attempts
            if len(character_attempts) > 1000:
                for old_key in list(character_attempts):
                    character_attempts[old_key] = [
                        timestamp
                        for timestamp in character_attempts[old_key]
                        if now - timestamp < CHARACTER_RATE_LIMIT_WINDOW
                    ]
                    if not character_attempts[old_key]:
                        del character_attempts[old_key]
            return True

    def allow_ai_recommend_attempt(self):
        now = time.monotonic()
        key = self.rate_limit_client_ip()
        with ai_recommend_attempts_lock:
            attempts = [
                timestamp
                for timestamp in ai_recommend_attempts.get(key, [])
                if now - timestamp < AI_RECOMMEND_RATE_LIMIT_WINDOW
            ]
            if len(attempts) >= AI_RECOMMEND_RATE_LIMIT_ATTEMPTS:
                ai_recommend_attempts[key] = attempts
                return False
            attempts.append(now)
            ai_recommend_attempts[key] = attempts
            if len(ai_recommend_attempts) > 1000:
                for old_key in list(ai_recommend_attempts):
                    ai_recommend_attempts[old_key] = [
                        timestamp
                        for timestamp in ai_recommend_attempts[old_key]
                        if now - timestamp < AI_RECOMMEND_RATE_LIMIT_WINDOW
                    ]
                    if not ai_recommend_attempts[old_key]:
                        del ai_recommend_attempts[old_key]
            return True

    def allow_ai_synopsis_attempt(self):
        now = time.monotonic()
        key = self.rate_limit_client_ip()
        with ai_synopsis_attempts_lock:
            attempts = [
                timestamp
                for timestamp in ai_synopsis_attempts.get(key, [])
                if now - timestamp < AI_SYNOPSIS_RATE_LIMIT_WINDOW
            ]
            if len(attempts) >= AI_SYNOPSIS_RATE_LIMIT_ATTEMPTS:
                ai_synopsis_attempts[key] = attempts
                return False
            attempts.append(now)
            ai_synopsis_attempts[key] = attempts
            if len(ai_synopsis_attempts) > 1000:
                for old_key in list(ai_synopsis_attempts):
                    ai_synopsis_attempts[old_key] = [
                        timestamp
                        for timestamp in ai_synopsis_attempts[old_key]
                        if now - timestamp < AI_SYNOPSIS_RATE_LIMIT_WINDOW
                    ]
                    if not ai_synopsis_attempts[old_key]:
                        del ai_synopsis_attempts[old_key]
            return True

    def allow_ai_tag_attempt(self):
        now = time.monotonic()
        key = self.rate_limit_client_ip()
        with ai_tag_attempts_lock:
            attempts = [
                timestamp
                for timestamp in ai_tag_attempts.get(key, [])
                if now - timestamp < AI_TAG_RATE_LIMIT_WINDOW
            ]
            if len(attempts) >= AI_TAG_RATE_LIMIT_ATTEMPTS:
                ai_tag_attempts[key] = attempts
                return False
            attempts.append(now)
            ai_tag_attempts[key] = attempts
            if len(ai_tag_attempts) > 1000:
                for old_key in list(ai_tag_attempts):
                    ai_tag_attempts[old_key] = [
                        timestamp
                        for timestamp in ai_tag_attempts[old_key]
                        if now - timestamp < AI_TAG_RATE_LIMIT_WINDOW
                    ]
                    if not ai_tag_attempts[old_key]:
                        del ai_tag_attempts[old_key]
            return True

    def allow_ai_curator_attempt(self):
        return self._allow_rate_limited_attempt(
            ai_curator_attempts,
            ai_curator_attempts_lock,
            AI_CURATOR_RATE_LIMIT_WINDOW,
            AI_CURATOR_RATE_LIMIT_ATTEMPTS,
        )

    def allow_ai_character_attempt(self):
        return self._allow_rate_limited_attempt(
            ai_character_attempts,
            ai_character_attempts_lock,
            AI_CHARACTER_RATE_LIMIT_WINDOW,
            AI_CHARACTER_RATE_LIMIT_ATTEMPTS,
        )

    def allow_ai_character_image_attempt(self):
        return self._allow_rate_limited_attempt(
            ai_character_image_attempts,
            ai_character_image_attempts_lock,
            AI_CHARACTER_IMAGE_RATE_LIMIT_WINDOW,
            AI_CHARACTER_IMAGE_RATE_LIMIT_ATTEMPTS,
        )

    def _allow_rate_limited_attempt(self, attempts_by_key, lock, window, limit):
        now = time.monotonic()
        key = self.rate_limit_client_ip()
        with lock:
            attempts = [
                timestamp
                for timestamp in attempts_by_key.get(key, [])
                if now - timestamp < window
            ]
            if len(attempts) >= limit:
                attempts_by_key[key] = attempts
                return False
            attempts.append(now)
            attempts_by_key[key] = attempts
            if len(attempts_by_key) > 1000:
                for old_key in list(attempts_by_key):
                    attempts_by_key[old_key] = [
                        timestamp
                        for timestamp in attempts_by_key[old_key]
                        if now - timestamp < window
                    ]
                    if not attempts_by_key[old_key]:
                        del attempts_by_key[old_key]
            return True

    def read_json_body(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type.lower() != "application/json":
            self.send_json(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid Content-Length"})
            return None

        if content_length < 1 or content_length > 16384:
            self.send_json(400, {"error": "Invalid request body size"})
            return None

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must be valid JSON"})
            return None
        return body

    def serve_index(self):
        try:
            content = INDEX_PATH.read_bytes()
        except OSError:
            self.send_json(500, {"error": "Could not read index.html"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(content)

    def session_cookie(self, token):
        secure = os.environ.get("ANIPICK_COOKIE_SECURE")
        use_secure = secure == "1" or (
            secure is None and os.environ.get("ANIPICK_ENV") == "production"
        )
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_SECONDS}"
        )
        return f"{cookie}; Secure" if use_secure else cookie

    def expired_session_cookie(self):
        cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        if os.environ.get("ANIPICK_COOKIE_SECURE") == "1" or (
            os.environ.get("ANIPICK_COOKIE_SECURE") is None
            and os.environ.get("ANIPICK_ENV") == "production"
        ):
            return f"{cookie}; Secure"
        return cookie

    def send_json(self, status, payload, extra_headers=()):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


def main():
    database.initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), AnimeRequestHandler)
    print(f"AniPick is ready at http://{HOST}:{PORT}")
    print(
        "MySQL database: "
        f"{os.environ.get('ANIPICK_MYSQL_HOST', '127.0.0.1')}:"
        f"{os.environ.get('ANIPICK_MYSQL_PORT', '3306')}/"
        f"{os.environ.get('ANIPICK_MYSQL_DATABASE', 'anipick')}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping AniPick.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
