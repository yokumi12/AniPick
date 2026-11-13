import json
import os
import threading
import unittest
import uuid
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import database
from server import AnimeRequestHandler
from database import MySQLConnection
from http.server import ThreadingHTTPServer


class AuthenticationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_database = os.environ.get("ANIPICK_TEST_MYSQL_DATABASE")
        if not test_database or not test_database.endswith("_test"):
            raise unittest.SkipTest(
                "Set ANIPICK_TEST_MYSQL_DATABASE to a dedicated *_test schema"
            )
        cls.original_database = os.environ.get("ANIPICK_MYSQL_DATABASE")
        os.environ["ANIPICK_MYSQL_DATABASE"] = test_database
        database.initialize_database()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AnimeRequestHandler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.account_number = 0

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()
        if cls.original_database is None:
            os.environ.pop("ANIPICK_MYSQL_DATABASE", None)
        else:
            os.environ["ANIPICK_MYSQL_DATABASE"] = cls.original_database

    @classmethod
    def new_account(cls):
        cls.account_number += 1
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        username = f"t{uuid.uuid4().hex[:8]}{cls.account_number}"
        status, payload, _ = cls.request(
            opener,
            "/api/auth/register",
            "POST",
            {"username": username, "password": "correct horse battery"},
        )
        if status != 200:
            raise AssertionError(f"Could not register test account: {payload}")
        return opener, username, payload

    @classmethod
    def request(cls, opener, path, method="GET", body=None, headers=None):
        request_headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            cls.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = opener.open(request)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            payload = json.loads(content) if content else {}
            return response.status, payload, response.headers

    def test_public_catalog_and_login_required_for_favorites(self):
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, catalog, _ = self.request(opener, "/api/anime")
        self.assertEqual(status, 200)
        self.assertTrue(catalog["items"])
        status, payload, _ = self.request(opener, "/api/me")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["user"])
        status, _, _ = self.request(opener, "/api/favorites")
        self.assertEqual(status, 401)

    def test_favorites_are_isolated_and_csrf_protected(self):
        account_a, _, auth_a = self.new_account()
        account_b, _, auth_b = self.new_account()
        anime_id = database.get_catalog()[0]["id"]

        status, _, _ = self.request(
            account_a,
            "/api/favorites",
            "POST",
            {"anime_id": anime_id, "saved": True},
            {"X-CSRF-Token": auth_a["csrf_token"]},
        )
        self.assertEqual(status, 200)

        status, payload, _ = self.request(account_b, "/api/favorites")
        self.assertEqual(status, 200)
        self.assertNotIn(anime_id, payload["items"])

        status, _, _ = self.request(
            account_b,
            "/api/favorites",
            "POST",
            {"anime_id": anime_id, "saved": True},
            {"X-CSRF-Token": auth_a["csrf_token"]},
        )
        self.assertEqual(status, 403)

        status, payload, _ = self.request(account_a, "/api/favorites")
        self.assertEqual(status, 200)
        self.assertIn(anime_id, payload["items"])

        status, _, _ = self.request(
            account_a,
            "/api/auth/logout",
            "POST",
            {},
            {"X-CSRF-Token": auth_a["csrf_token"]},
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request(account_a, "/api/favorites")
        self.assertEqual(status, 401)

    def test_account_deletion_requires_password_and_removes_related_data(self):
        account_a, username, auth_a = self.new_account()
        user_id = database.get_user_by_username(username)["id"]
        anime_id = database.get_catalog()[0]["id"]
        status, _, _ = self.request(
            account_a,
            "/api/favorites",
            "POST",
            {"anime_id": anime_id, "saved": True},
            {"X-CSRF-Token": auth_a["csrf_token"]},
        )
        self.assertEqual(status, 200)

        account_b = build_opener(HTTPCookieProcessor(CookieJar()))
        status, _, _ = self.request(
            account_b,
            "/api/auth/login",
            "POST",
            {"username": username, "password": "correct horse battery"},
        )
        self.assertEqual(status, 200)

        status, payload, _ = self.request(
            account_a,
            "/api/auth/delete",
            "POST",
            {"password": "incorrect password"},
            {"X-CSRF-Token": auth_a["csrf_token"]},
        )
        self.assertEqual(status, 403)
        self.assertIn("비밀번호", payload["error"])
        status, favorites, _ = self.request(account_a, "/api/favorites")
        self.assertEqual(status, 200)
        self.assertIn(anime_id, favorites["items"])

        status, _, _ = self.request(
            account_a,
            "/api/auth/delete",
            "POST",
            {"password": "correct horse battery"},
        )
        self.assertEqual(status, 403)

        status, _, headers = self.request(
            account_a,
            "/api/auth/delete",
            "POST",
            {"password": "correct horse battery"},
            {"X-CSRF-Token": auth_a["csrf_token"]},
        )
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

        status, current, _ = self.request(account_a, "/api/me")
        self.assertEqual(status, 200)
        self.assertIsNone(current["user"])
        status, current, _ = self.request(account_b, "/api/me")
        self.assertEqual(status, 200)
        self.assertIsNone(current["user"])

        self.assertIsNone(database.get_user_by_username(username))
        with database.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM favorite WHERE user_id = ? AND anime_id = ?",
                    (user_id, anime_id),
                ).fetchone()
            )

    def test_registration_validation_and_password_hashing(self):
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, _, _ = self.request(
            opener,
            "/api/auth/register",
            "POST",
            {"username": "bad id!", "password": "correct horse battery"},
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            opener,
            "/api/auth/register",
            "POST",
            {"username": "newuser", "password": "1234567"},
        )
        self.assertEqual(status, 400)

        eight_character_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, short_password_payload, _ = self.request(
            eight_character_opener,
            "/api/auth/register",
            "POST",
            {"username": "eightchar", "password": "12345678"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(short_password_payload["user"]["username"], "eightchar")

        registered_opener, username, auth_payload = self.new_account()
        status, _, _ = self.request(
            registered_opener,
            "/api/auth/register",
            "POST",
            {"username": username.upper(), "password": "correct horse battery"},
        )
        self.assertEqual(status, 409)

        login_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, _, _ = self.request(
            login_opener,
            "/api/auth/login",
            "POST",
            {"username": username, "password": "incorrect password"},
        )
        self.assertEqual(status, 401)
        status, login_payload, response_headers = self.request(
            login_opener,
            "/api/auth/login",
            "POST",
            {"username": username, "password": "correct horse battery"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(login_payload["user"]["username"], username)
        self.assertIn("HttpOnly", response_headers["Set-Cookie"])
        status, current_payload, _ = self.request(login_opener, "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(current_payload["user"]["username"], username)
        self.assertNotEqual(auth_payload["csrf_token"], login_payload["csrf_token"])

        with database.connect() as connection:
            account = connection.execute(
                "SELECT password_salt, password_hash FROM user_account "
                "WHERE username = ?",
                (username,),
            ).fetchone()
        self.assertIsNotNone(account)
        self.assertNotEqual(account["password_hash"], b"correct horse battery")
        self.assertNotEqual(account["password_salt"], b"")

    def test_mysql_cursor_adapter_rewrites_placeholders_and_closes_writes(self):
        class FakeCursor:
            with_rows = False
            lastrowid = 23
            rowcount = 1

            def execute(self, statement, parameters):
                self.statement = statement
                self.parameters = parameters
                self.closed = False

            def close(self):
                self.closed = True

        class FakeConnection:
            def __init__(self, cursor):
                self.cursor_instance = cursor

            def cursor(self, dictionary):
                if not dictionary:
                    raise AssertionError("MySQL queries must return dictionary rows")
                return self.cursor_instance

        cursor = FakeCursor()
        connection = MySQLConnection(FakeConnection(cursor))
        adapted_cursor = connection.execute(
            "INSERT INTO example VALUES (?, ?)", (1, "two")
        )
        self.assertEqual(cursor.statement, "INSERT INTO example VALUES (%s, %s)")
        self.assertEqual(cursor.parameters, (1, "two"))
        self.assertTrue(cursor.closed)
        self.assertEqual(adapted_cursor.lastrowid, 23)


if __name__ == "__main__":
    unittest.main()
