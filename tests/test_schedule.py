import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import airing_schedule


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


class AiringScheduleTests(unittest.TestCase):
    def test_schedule_normalization_uses_korean_time_and_safe_anilist_link(self):
        airing_at = int(datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc).timestamp())
        item = {
            "airingAt": airing_at,
            "episode": 5,
            "media": {
                "id": 123,
                "isAdult": False,
                "format": "TV",
                "genres": ["Drama"],
                "popularity": 12345,
                "averageScore": 82,
                "startDate": {"year": 2026},
                "synonyms": ["니아 리스트온"],
                "title": {
                    "english": "English title",
                    "romaji": "Romaji title",
                    "native": "日本語タイトル",
                },
                "coverImage": {"large": "https://cdn.example.com/poster.jpg"},
                "siteUrl": "https://example.com/untrusted",
            },
        }

        result = airing_schedule._normalize_anime(item)

        self.assertEqual(result["title"], "English title")
        self.assertEqual(result["title_ko"], "니아 리스트온")
        self.assertEqual(result["original_title"], "日本語タイトル")
        self.assertEqual(result["title_synonyms"], ["니아 리스트온"])
        self.assertEqual(result["genres"], ["Drama"])
        self.assertEqual(result["popularity"], 12345)
        self.assertEqual(result["score"], 82)
        self.assertEqual(result["year"], 2026)
        self.assertEqual(result["broadcast_time"], "23:30")
        self.assertEqual(result["broadcast_date"], "2026-10-27")
        self.assertEqual(result["broadcast_timezone"], "Asia/Seoul")
        self.assertEqual(result["episode"], 5)
        self.assertEqual(result["url"], "https://anilist.co/anime/123")

    def test_schedule_normalization_preserves_title_synonyms_for_catalog_matching(self):
        item = {
            "airingAt": 1793103960,
            "episode": 1,
            "media": {
                "id": 123,
                "isAdult": False,
                "format": "TV",
                "genres": [],
                "synonyms": ["Kitsu catalog alias", None, 42],
                "title": {"english": "Sample"},
            },
        }

        result = airing_schedule._normalize_anime(item)

        self.assertEqual(result["title_synonyms"], ["Kitsu catalog alias"])

    def test_schedule_normalization_returns_genres_for_catalog_import(self):
        item = {
            "airingAt": 1793103960,
            "episode": 1,
            "media": {
                "id": 123,
                "isAdult": False,
                "format": "TV",
                "genres": ["Action", 42, "Fantasy"],
                "synonyms": [],
                "title": {"english": "Sample"},
            },
        }

        result = airing_schedule._normalize_anime(item)

        self.assertEqual(result["genres"], ["Action", "Fantasy"])

    def test_schedule_excludes_adult_and_non_tv_entries(self):
        base = {
            "airingAt": 1793103960,
            "episode": 4,
            "media": {
                "id": 123,
                "isAdult": False,
                "format": "TV",
                "genres": [],
                "synonyms": [],
                "title": {"english": "Sample"},
            },
        }

        adult = json.loads(json.dumps(base))
        adult["media"]["isAdult"] = True
        movie = json.loads(json.dumps(base))
        movie["media"]["format"] = "MOVIE"
        ecchi = json.loads(json.dumps(base))
        ecchi["media"]["genres"] = ["Ecchi"]

        self.assertIsNone(airing_schedule._normalize_anime(adult))
        self.assertIsNone(airing_schedule._normalize_anime(movie))
        self.assertIsNone(airing_schedule._normalize_anime(ecchi))

    def test_day_range_uses_upcoming_korean_weekday(self):
        now = datetime(2026, 10, 27, 12, 0, tzinfo=airing_schedule.KOREA_TIMEZONE)

        after, before, schedule_date = airing_schedule._day_range("monday", now)

        self.assertEqual(schedule_date, "2026-11-02")
        self.assertEqual(after, int(datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(before - after, 24 * 60 * 60)

    def test_schedule_rejects_invalid_day(self):
        with self.assertRaises(airing_schedule.ScheduleError) as raised:
            airing_schedule.get_schedule("funday")

        self.assertEqual(raised.exception.status, 400)

    def test_schedule_fetches_all_pages_and_caches_results(self):
        first_page = (
            [{
                "airingAt": 1793103960,
                "episode": 4,
                "media": {
                    "id": 1,
                    "isAdult": False,
                    "format": "TV",
                    "title": {"english": "First"},
                },
            }],
            {"hasNextPage": True},
        )
        second_page = (
            [{
                "airingAt": 1793106000,
                "episode": 2,
                "media": {
                    "id": 2,
                    "isAdult": False,
                    "format": "TV",
                    "title": {"english": "Second"},
                },
            }],
            {"hasNextPage": False},
        )
        with patch.dict(airing_schedule._cache, {}, clear=True), patch.object(
            airing_schedule, "_day_range", return_value=(100, 200, "2026-10-27")
        ), patch.object(
            airing_schedule, "_request_page", side_effect=[first_page, second_page]
        ) as request_page:
            result = airing_schedule.get_schedule("monday")
            cached = airing_schedule.get_schedule("monday")

        self.assertEqual([item["id"] for item in result["items"]], [1, 2])
        self.assertEqual(result["date"], "2026-10-27")
        self.assertEqual(cached, result)
        self.assertEqual(request_page.call_count, 2)

    def test_public_graphql_query_does_not_require_api_key(self):
        payload = {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": False},
                    "airingSchedules": [],
                }
            }
        }
        with patch.object(airing_schedule.time, "sleep"), patch.object(
            airing_schedule, "urlopen", return_value=FakeResponse(payload)
        ) as open_url:
            items, page_info = airing_schedule._request_page(100, 200, 1)

        request = open_url.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://graphql.anilist.co")
        self.assertEqual(request.method, "POST")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request_body["variables"]["after"], 100)
        self.assertIn("airingSchedules", request_body["query"])
        self.assertIn("synonyms", request_body["query"])
        self.assertIn("popularity", request_body["query"])
        self.assertIn("averageScore", request_body["query"])
        self.assertIn("startDate", request_body["query"])
        self.assertEqual(items, [])
        self.assertEqual(page_info, {"hasNextPage": False})

    def test_schedule_rejects_malformed_pagination(self):
        with patch.dict(airing_schedule._cache, {}, clear=True), patch.object(
            airing_schedule,
            "_day_range",
            return_value=(100, 200, "2026-10-27"),
        ), patch.object(
            airing_schedule,
            "_request_page",
            return_value=([], {"hasNextPage": "false"}),
        ):
            with self.assertRaises(airing_schedule.ScheduleError):
                airing_schedule.get_schedule("tuesday")

    def test_schedule_refreshes_cache_when_next_weekday_changes(self):
        old_payload = {
            "day": "monday",
            "date": "2026-10-26",
            "items": [],
            "updated_at": "2026-10-26T00:00:00+00:00",
        }
        with patch.dict(
            airing_schedule._cache,
            {"monday": {"cached_at": 1000, "payload": old_payload}},
            clear=True,
        ), patch.object(
            airing_schedule.time, "monotonic", return_value=1001
        ), patch.object(
            airing_schedule,
            "_day_range",
            return_value=(200, 300, "2026-11-02"),
        ), patch.object(
            airing_schedule,
            "_fetch_day",
            return_value=([], "2026-11-02"),
        ) as fetch_day:
            result = airing_schedule.get_schedule("monday")

        self.assertEqual(result["date"], "2026-11-02")
        fetch_day.assert_called_once_with("monday")


if __name__ == "__main__":
    unittest.main()
