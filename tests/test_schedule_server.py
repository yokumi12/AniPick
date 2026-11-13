import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from server import AnimeRequestHandler
import airing_schedule


class ScheduleApiTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AnimeRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/schedule"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_schedule_endpoint_returns_schedule_payload(self):
        expected = {"day": "thursday", "date": "2026-10-29", "items": []}
        with patch("server.airing_schedule.get_schedule", return_value=expected) as get_schedule:
            with urlopen(f"{self.url}?day=thursday", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload, expected)
        get_schedule.assert_called_once_with("thursday")

    def test_schedule_errors_preserve_the_service_status(self):
        with patch(
            "server.airing_schedule.get_schedule",
            side_effect=airing_schedule.ScheduleError("잘못된 요일입니다.", 400),
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{self.url}?day=invalid", timeout=5)

        self.assertEqual(raised.exception.code, 400)
        with raised.exception as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["error"], "잘못된 요일입니다.")


if __name__ == "__main__":
    unittest.main()
