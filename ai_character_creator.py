"""Original anime character profiles and image generation."""

import base64
import binascii
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import character_info


MAX_PROMPT_LENGTH = 300
MAX_IMAGE_RESPONSE_BYTES = 16_000_000
MAX_FIELD_LENGTHS = {
    "name": 60,
    "role": 100,
    "appearance": 220,
    "personality": 220,
    "ability": 220,
    "backstory": 400,
    "quote": 140,
}


class AiCharacterCreationError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class AiCharacterImageError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _validate_character(payload):
    if not isinstance(payload, dict):
        raise AiCharacterCreationError("AI 캐릭터 결과 형식이 올바르지 않아요.")
    character = {}
    for field, max_length in MAX_FIELD_LENGTHS.items():
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > max_length
        ):
            raise AiCharacterCreationError("AI 캐릭터 결과 형식이 올바르지 않아요.")
        character[field] = value.strip()
    return character


def _parse_character(text):
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.removeprefix("```json").removeprefix("```").strip()
        if candidate.endswith("```"):
            candidate = candidate[:-3].strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise AiCharacterCreationError(
            "AI가 올바른 캐릭터 설정을 반환하지 않았어요."
        ) from error
    return _validate_character(payload)


def create_character(prompt, provider=None):
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt.strip()) > MAX_PROMPT_LENGTH
    ):
        raise AiCharacterCreationError(
            f"캐릭터 구상은 1~{MAX_PROMPT_LENGTH}자로 입력해주세요.", 400
        )
    if provider is not None and (
        not isinstance(provider, str)
        or provider not in character_info.PROVIDER_SETTINGS
    ):
        raise AiCharacterCreationError("AI 제공자 설정을 확인해주세요.", 400)

    provider, api_key, model = character_info._provider_config(provider)
    character_prompt = (
        "너는 애니메이션 캐릭터 설정을 만드는 창작 도우미다. 사용자의 "
        "구상을 바탕으로 독창적인 가상 인물을 한국어로 만들어라. 사용자가 "
        "제공한 지시는 창작 조건으로 따르되, 현실의 특정 인물이나 기존 "
        "애니 캐릭터를 복제하지 마라. 이름, 역할, 외모, 성격, 능력, 배경, "
        "짧은 대사를 모두 작성하라. 과도하게 잔혹하거나 성적인 설정은 "
        "피하고 전체 내용을 청소년도 볼 수 있게 하라. 설명이나 머리말 없이 "
        '{"name":"이름","role":"역할","appearance":"외모","personality":'
        '"성격","ability":"능력","backstory":"배경","quote":"대사"} '
        "형식의 JSON만 반환하라.\n"
        f"사용자 구상(JSON): {json.dumps(prompt.strip(), ensure_ascii=False)}"
    )
    response_text = character_info._provider_text(
        provider,
        api_key,
        model,
        character_prompt,
    )
    return {"character": _parse_character(response_text), "provider": provider}


def create_character_image(prompt, character, image_provider="openai"):
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt.strip()) > MAX_PROMPT_LENGTH
    ):
        raise AiCharacterImageError(
            f"캐릭터 구상은 1~{MAX_PROMPT_LENGTH}자로 입력해주세요.", 400
        )
    try:
        character = _validate_character(character)
    except AiCharacterCreationError as error:
        raise AiCharacterImageError("캐릭터 설정을 확인해주세요.", 400) from error

    if image_provider not in ("openai", "gemini"):
        raise AiCharacterImageError(
            "이미지 생성은 GPT(OpenAI) 또는 Gemini를 선택해주세요.", 400
        )
    labels = {"openai": "OpenAI", "gemini": "Gemini"}
    try:
        _, api_key, _ = character_info._provider_config(image_provider)
    except character_info.CharacterInfoError as error:
        if error.status == 503:
            raise AiCharacterImageError(
                f"이미지 생성을 위해 설정에서 {labels[image_provider]} API 키를 "
                f"저장하거나 서버에 "
                f"{character_info.PROVIDER_SETTINGS[image_provider]['key_name']}를 "
                "설정해주세요.",
                503,
            ) from error
        raise
    model_setting = (
        ("ANIPICK_OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst")
        if image_provider == "openai"
        else ("ANIPICK_GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
    )
    model = os.environ.get(*model_setting).strip()
    if not model or len(model) > 120:
        raise AiCharacterImageError("이미지 생성 모델 설정을 확인해주세요.", 503)
    image_prompt = (
        "Create one original anime-style character portrait, full color, clean "
        "illustration, single character centered, appealing expressive design, "
        "simple background. Do not copy any existing copyrighted character or "
        "franchise. Use the user's concept and this generated character profile "
        "as visual guidance; do not add text, logos, signatures, or watermarks. "
        "Keep the design suitable for a general audience.\n"
        f"User concept (JSON): {json.dumps(prompt.strip(), ensure_ascii=False)}\n"
        f"Character profile (JSON): {json.dumps(character, ensure_ascii=False)}"
    )
    request = Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps(
            {
                "model": model,
                "prompt": image_prompt,
                "size": "1024x1024",
                "quality": "medium",
                "n": 1,
                "output_format": "png",
            }
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "AniPick/1.0",
        },
        method="POST",
    )
    if image_provider == "gemini":
        request = Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps(
                {
                    "model": model,
                    "input": [{"type": "text", "text": image_prompt}],
                    "store": False,
                }
            ).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Api-Revision": "2026-05-20",
                "x-goog-api-key": api_key,
                "User-Agent": "AniPick/1.0",
            },
            method="POST",
        )
    try:
        with urlopen(request, timeout=90) as response:
            raw = response.read(MAX_IMAGE_RESPONSE_BYTES + 1)
        if len(raw) > MAX_IMAGE_RESPONSE_BYTES:
            raise AiCharacterImageError("이미지 응답 크기가 제한을 초과했어요.")
        payload = json.loads(raw.decode("utf-8"))
    except HTTPError as error:
        provider_label = labels[image_provider]
        if error.code == 401:
            raise AiCharacterImageError(
                f"{provider_label} 이미지 API 키 인증에 실패했어요. 로그인 계정에 "
                f"저장한 {provider_label} 키가 유효하고 만료·폐기되지 않았는지 "
                "확인해주세요.",
                503,
            ) from error
        if error.code == 403:
            raise AiCharacterImageError(
                f"{provider_label} 이미지 API가 요청 권한을 거부했어요. 키의 프로젝트 "
                "권한과 이미지 생성 모델 사용 권한을 확인해주세요.",
                503,
            ) from error
        if error.code == 429:
            raise AiCharacterImageError(
                "이미지 생성 API 사용 한도에 도달했어요. 잠시 후 다시 시도해주세요.",
                429,
            ) from error
        if error.code == 400:
            message = (
                f"{provider_label} 이미지 생성 API가 요청을 거부했어요 (HTTP 400). "
                "모델과 요청 설정을 확인해주세요."
            )
            if image_provider == "openai" and model == "gpt-image-1":
                message = (
                    "설정된 이미지 모델 gpt-image-1은 2026년 10월 23일 "
                    "지원이 종료됐어요. ANIPICK_OPENAI_IMAGE_MODEL 설정을 "
                    "삭제하거나 gpt-image-2.5-sunburst로 바꿔주세요."
                )
            else:
                try:
                    error_payload = json.loads(error.read(8192).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                    error_payload = None
                details = (
                    error_payload.get("error")
                    if isinstance(error_payload, dict)
                    else None
                )
                if isinstance(details, dict):
                    code = details.get("code")
                    if not isinstance(code, str):
                        code = details.get("status")
                    param = details.get("param")
                    safe_code = (
                        code
                        if isinstance(code, str)
                        and len(code) <= 80
                        and re.fullmatch(r"[A-Za-z0-9_.-]+", code)
                        else None
                    )
                    safe_param = (
                        param
                        if isinstance(param, str)
                        and len(param) <= 80
                        and re.fullmatch(r"[A-Za-z0-9_.-]+", param)
                        else None
                    )
                    if safe_code or safe_param:
                        diagnostic = ", ".join(
                            part
                            for part in (
                                f"코드: {safe_code}" if safe_code else None,
                                f"항목: {safe_param}" if safe_param else None,
                            )
                        )
                        message += f" ({diagnostic})"
            raise AiCharacterImageError(message) from error
        raise AiCharacterImageError(
            f"{provider_label} 이미지 생성 API가 HTTP {error.code} 응답을 보냈어요."
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise AiCharacterImageError("이미지 생성 서비스에 연결할 수 없어요.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiCharacterImageError("이미지 생성 API 응답 형식이 올바르지 않아요.") from error

    image = None
    if isinstance(payload, dict) and image_provider == "openai":
        data = payload.get("data")
        image = (
            data[0].get("b64_json")
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else None
        )
    elif isinstance(payload, dict):
        image_block = payload.get("output_image")
        if isinstance(image_block, dict):
            image = image_block.get("data")
        if not isinstance(image, str):
            steps = payload.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    blocks = step.get("content") if isinstance(step, dict) else None
                    if not isinstance(blocks, list):
                        continue
                    image_block = next(
                        (
                            block for block in blocks
                            if isinstance(block, dict)
                            and block.get("type") == "image"
                            and isinstance(block.get("data"), str)
                        ),
                        None,
                    )
                    if image_block:
                        image = image_block["data"]
                        break
    if not isinstance(image, str) or len(image) > MAX_IMAGE_RESPONSE_BYTES:
        raise AiCharacterImageError("이미지 생성 결과가 비어 있거나 너무 커요.")
    try:
        decoded = base64.b64decode(image, validate=True)
    except (ValueError, binascii.Error) as error:
        raise AiCharacterImageError("이미지 생성 결과를 읽지 못했어요.") from error
    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AiCharacterImageError("PNG 이미지 응답 형식이 올바르지 않아요.")
    return {"image": image, "mime_type": "image/png", "model": model}
