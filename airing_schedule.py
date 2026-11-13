import json
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://graphql.anilist.co"
CACHE_SECONDS = 6 * 60 * 60
REQUEST_INTERVAL_SECONDS = 2.1
MAX_PAGES = 5
PAGE_SIZE = 50
KOREA_TIMEZONE = timezone(timedelta(hours=9))
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

QUERY = """
query ($after: Int!, $before: Int!, $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo {
      hasNextPage
    }
    airingSchedules(
      airingAt_greater: $after
      airingAt_lesser: $before
      sort: TIME
    ) {
      airingAt
      episode
      media {
        id
        isAdult
        format
        genres
        popularity
        averageScore
        startDate {
          year
        }
        synonyms
        title {
          english
          romaji
          native
        }
        coverImage {
          large
        }
      }
    }
  }
}
"""

_cache = {}
_lock = threading.Lock()
_last_request_at = 0.0


class ScheduleError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _day_range(day, now=None):
    now = now or datetime.now(KOREA_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KOREA_TIMEZONE)
    else:
        now = now.astimezone(KOREA_TIMEZONE)

    target_weekday = WEEKDAYS.index(day)
    days_ahead = (target_weekday - now.weekday()) % 7
    target_date = now.date() + timedelta(days=days_ahead)
    starts_at = datetime.combine(target_date, datetime_time.min, KOREA_TIMEZONE)
    ends_at = starts_at + timedelta(days=1)
    after = max(int(now.timestamp()), int(starts_at.timestamp()))
    return after, int(ends_at.timestamp()), target_date.isoformat()


def _request_page(after, before, page):
    global _last_request_at
    wait = REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)

    request = Request(
        API_URL,
        data=json.dumps(
            {
                "query": QUERY,
                "variables": {
                    "after": after,
                    "before": before,
                    "page": page,
                    "perPage": PAGE_SIZE,
                },
            }
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AniPick/1.0 (public airing schedule)",
        },
        method="POST",
    )
    _last_request_at = time.monotonic()
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        status = error.code if error.code in (429, 503) else 502
        if error.code == 429:
            message = "편성 정보 API 요청이 많아요. 잠시 후 다시 시도해주세요."
        elif error.code == 403:
            message = "AniList 편성 정보 API가 일시적으로 중단되었어요."
        else:
            message = f"편성 정보를 제공하는 API가 HTTP {error.code} 응답을 보냈어요."
        raise ScheduleError(
            message,
            status,
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise ScheduleError("방영 정보를 제공하는 API에 연결할 수 없어요.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScheduleError("방영 정보 API가 올바르지 않은 데이터를 보냈어요.") from error

    if not isinstance(payload, dict):
        raise ScheduleError("방영 정보 API 응답 형식이 올바르지 않아요.")
    errors = payload.get("errors")
    if errors:
        error = errors[0] if isinstance(errors, list) and errors else {}
        error_status = error.get("status") if isinstance(error, dict) else None
        if error_status == 429:
            raise ScheduleError(
                "편성 정보 API 요청이 많아요. 잠시 후 다시 시도해주세요.", 429
            )
        if error_status == 403:
            raise ScheduleError("AniList 편성 정보 API가 일시적으로 중단되었어요.")
        raise ScheduleError("AniList에서 편성 정보를 가져오지 못했어요.")

    data = payload.get("data")
    page_data = data.get("Page") if isinstance(data, dict) else None
    schedules = page_data.get("airingSchedules") if isinstance(page_data, dict) else None
    page_info = page_data.get("pageInfo") if isinstance(page_data, dict) else None
    if not isinstance(schedules, list) or not isinstance(page_info, dict):
        raise ScheduleError("방영 정보 API 응답 형식이 올바르지 않아요.")
    return schedules, page_info


def _normalize_anime(item):
    if not isinstance(item, dict) or not isinstance(item.get("media"), dict):
        raise ScheduleError("방영 정보 API에 올바르지 않은 작품 항목이 있어요.")
    media = item["media"]
    anime_id = media.get("id")
    if type(anime_id) is not int or anime_id < 1:
        raise ScheduleError("방영 정보 API에 올바르지 않은 작품 ID가 있어요.")
    airing_at = item.get("airingAt")
    if type(airing_at) is not int or airing_at < 1:
        raise ScheduleError("방영 정보 API에 올바르지 않은 방영 시간이 있어요.")
    genres = media.get("genres")
    if not isinstance(genres, list):
        genres = []
    if (
        media.get("isAdult") is True
        or media.get("format") != "TV"
        or any(
            isinstance(genre, str)
            and genre.casefold() in {"ecchi", "hentai", "erotica"}
            for genre in genres
        )
    ):
        return None

    title = media.get("title")
    title = title if isinstance(title, dict) else {}
    synonyms = media.get("synonyms")
    if not isinstance(synonyms, list):
        synonyms = []
    korean_title = next(
        (
            synonym
            for synonym in synonyms
            if isinstance(synonym, str) and any("\uac00" <= char <= "\ud7a3" for char in synonym)
        ),
        None,
    )
    image = media.get("coverImage")
    image = image.get("large") if isinstance(image, dict) else None
    start_date = media.get("startDate")
    start_date = start_date if isinstance(start_date, dict) else {}
    scheduled_at = datetime.fromtimestamp(airing_at, KOREA_TIMEZONE)
    return {
        "id": anime_id,
        "title": title.get("english") or title.get("romaji") or "",
        "title_ko": korean_title,
        "original_title": title.get("native") or "",
        "title_synonyms": [
            synonym for synonym in synonyms if isinstance(synonym, str)
        ],
        "genres": [genre for genre in genres if isinstance(genre, str)],
        "popularity": media.get("popularity"),
        "score": media.get("averageScore"),
        "year": start_date.get("year"),
        "image_url": image if isinstance(image, str) and image.startswith("https://") else None,
        "broadcast_time": scheduled_at.strftime("%H:%M"),
        "broadcast_date": scheduled_at.date().isoformat(),
        "broadcast_timezone": "Asia/Seoul",
        "episode": item.get("episode"),
        "url": f"https://anilist.co/anime/{anime_id}",
    }


def _fetch_day(day, now=None):
    after, before, schedule_date = _day_range(day, now)
    anime = []
    for page in range(1, MAX_PAGES + 1):
        items, pagination = _request_page(after, before, page)
        anime.extend(
            normalized
            for item in items
            if (normalized := _normalize_anime(item)) is not None
        )
        has_next_page = pagination.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise ScheduleError("방영 정보 API의 페이지 상태가 올바르지 않아요.")
        if not has_next_page:
            return anime, schedule_date
    raise ScheduleError("방영 작품이 너무 많아 편성 정보를 끝까지 불러오지 못했어요.")


def get_schedule(day):
    day = day.strip().casefold() if isinstance(day, str) else ""
    if day not in WEEKDAYS:
        raise ScheduleError("요일을 확인할 수 없어요. 월요일부터 일요일까지 선택해주세요.", 400)

    now = time.monotonic()
    _, _, schedule_date = _day_range(day)
    cached = _cache.get(day)
    if (
        cached
        and cached["payload"].get("date") == schedule_date
        and now - cached["cached_at"] < CACHE_SECONDS
    ):
        return cached["payload"]

    with _lock:
        now = time.monotonic()
        _, _, schedule_date = _day_range(day)
        cached = _cache.get(day)
        if (
            cached
            and cached["payload"].get("date") == schedule_date
            and now - cached["cached_at"] < CACHE_SECONDS
        ):
            return cached["payload"]
        items, schedule_date = _fetch_day(day)
        payload = {
            "day": day,
            "date": schedule_date,
            "items": items,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _cache[day] = {"cached_at": time.monotonic(), "payload": payload}
        return payload
