import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

import database
from server import AnimeRequestHandler


class DeploymentRequestTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(AnimeRequestHandler)
        self.handler.headers = Message()

    def test_production_requires_cloudfront_origin_secret(self):
        with patch.dict(
            os.environ,
            {"ANIPICK_ENV": "production"},
            clear=True,
        ):
            self.assertFalse(self.handler.has_trusted_proxy())

    def test_cloudfront_origin_secret_is_compared_in_constant_time(self):
        with patch.dict(
                os.environ,
                {
                    "ANIPICK_ENV": "production",
                    "ANIPICK_CLOUDFRONT_ORIGIN_SECRET": "expected-secret",
                },
                clear=True,
        ):
                self.handler.headers = {"X-AniPick-Origin-Secret": "expected-secret"}
                self.assertTrue(self.handler.has_trusted_proxy())
                self.handler.headers = {"X-AniPick-Origin-Secret": "wrong-secret"}
                self.assertFalse(self.handler.has_trusted_proxy())

    def test_production_public_origin_is_checked_instead_of_backend_host(self):
        with patch.dict(
            os.environ,
            {"ANIPICK_PUBLIC_ORIGIN": "https://d123456789.cloudfront.net"},
            clear=True,
        ):
            self.handler.headers = {
                "Origin": "https://d123456789.cloudfront.net",
                "Host": "internal-api.example",
            }
            self.assertTrue(self.handler.has_same_origin())
            self.handler.headers["Origin"] = "https://attacker.example"
            self.assertFalse(self.handler.has_same_origin())

    def test_rate_limit_uses_validated_forwarded_ip_only_behind_origin_proxy(self):
        with patch.dict(
            os.environ,
            {
                "ANIPICK_ENV": "production",
                "ANIPICK_CLOUDFRONT_ORIGIN_SECRET": "expected-secret",
            },
            clear=True,
        ):
            self.handler.headers = {
                "X-AniPick-Origin-Secret": "expected-secret",
                "X-Forwarded-For": "198.51.100.10, 203.0.113.8",
            }
            self.handler.client_address = ("10.77.1.4", 8000)
            self.assertEqual(self.handler.rate_limit_client_ip(), "203.0.113.8")

            self.handler.headers["X-Forwarded-For"] = "203.0.113.8, invalid"
            self.assertEqual(self.handler.rate_limit_client_ip(), "10.77.1.4")

            self.handler.headers["X-AniPick-Origin-Secret"] = "wrong-secret"
            self.assertEqual(self.handler.rate_limit_client_ip(), "10.77.1.4")


class CatalogDeploymentTests(unittest.TestCase):
    def test_production_catalog_import_does_not_delete_missing_anime(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "anime.json"
            catalog_path.write_text(
                '{"items":[{"id":"one","title":"One","genres":[]}]}',
                encoding="utf-8",
            )
            connection = Mock()
            with (
                patch.object(database, "CATALOG_PATH", catalog_path),
                patch.object(
                    database,
                    "KOREAN_OVERRIDES_PATH",
                    Path(directory) / "missing-overrides.json",
                ),
                patch.object(
                    database,
                    "YOUTUBE_OVERRIDES_PATH",
                    Path(directory) / "missing-youtube.json",
                ),
                patch.dict(os.environ, {"ANIPICK_ENV": "production"}, clear=True),
            ):
                database.import_catalog(connection)

        deleted_anime = [
            call
            for call in connection.execute.call_args_list
            if call.args[0].startswith("DELETE FROM anime WHERE")
        ]
        self.assertEqual(deleted_anime, [])


if __name__ == "__main__":
    unittest.main()
