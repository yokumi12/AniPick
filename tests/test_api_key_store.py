import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import api_key_store


class ApiKeyStoreTests(unittest.TestCase):
    def test_local_key_is_generated_and_api_key_round_trips_encrypted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("ANIPICK_AI_PROVIDER=claude\n", encoding="utf-8")
            with patch.object(api_key_store.database, "ENV_PATH", env_path):
                with patch.dict(os.environ, {"ANIPICK_ENV": "development"}, clear=True):
                    encrypted = api_key_store.encrypt_api_key("private-api-key")
                    encryption_key = os.environ[
                        api_key_store.ENCRYPTION_KEY_ENV
                    ]
                    with patch.object(
                        api_key_store.database,
                        "get_user_ai_api_keys",
                        return_value={"openai": encrypted},
                    ):
                        decrypted = api_key_store.get_user_api_keys(17)

            saved_environment = env_path.read_text(encoding="utf-8")

        self.assertEqual(decrypted, {"openai": "private-api-key"})
        self.assertIn(f"{api_key_store.ENCRYPTION_KEY_ENV}={encryption_key}", saved_environment)
        self.assertNotIn("private-api-key", saved_environment)
        self.assertNotIn(b"private-api-key", encrypted)

    def test_production_requires_encryption_key_and_never_generates_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            with patch.object(api_key_store.database, "ENV_PATH", env_path):
                with patch.dict(os.environ, {"ANIPICK_ENV": "production"}, clear=True):
                    with self.assertRaises(api_key_store.ApiKeyStoreError):
                        api_key_store.encrypt_api_key("private-api-key")

            self.assertFalse(env_path.exists())

    def test_invalid_encryption_key_fails_closed_for_saved_keys(self):
        with patch.dict(
            os.environ,
            {
                "ANIPICK_API_KEY_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
                "ANIPICK_ENV": "production",
            },
            clear=True,
        ):
            with patch.object(
                api_key_store.database,
                "get_user_ai_api_keys",
                return_value={"claude": b"not-a-valid-token"},
            ):
                with self.assertRaises(api_key_store.ApiKeyStoreError):
                    api_key_store.get_user_api_keys(17)


if __name__ == "__main__":
    unittest.main()
