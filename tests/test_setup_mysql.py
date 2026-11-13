import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import setup_mysql


class SetupMySqlTests(unittest.TestCase):
    def test_mysql_setup_preserves_other_environment_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    (
                        "ANIPICK_API_KEY_ENCRYPTION_KEY=keep-this-encryption-key",
                        "ANTHROPIC_API_KEY=keep-this-provider-key",
                        "ANIPICK_MYSQL_HOST=old-host",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            with patch.object(setup_mysql, "ENV_PATH", env_path):
                setup_mysql.write_environment_settings(
                    {
                        "ANIPICK_MYSQL_HOST": "127.0.0.1",
                        "ANIPICK_MYSQL_PASSWORD": "new-database-password",
                    }
                )
            contents = env_path.read_text(encoding="utf-8")

        self.assertIn(
            "ANIPICK_API_KEY_ENCRYPTION_KEY=keep-this-encryption-key", contents
        )
        self.assertIn("ANTHROPIC_API_KEY=keep-this-provider-key", contents)
        self.assertIn("ANIPICK_MYSQL_HOST=127.0.0.1", contents)
        self.assertIn("ANIPICK_MYSQL_PASSWORD=new-database-password", contents)
        self.assertNotIn("ANIPICK_MYSQL_HOST=old-host", contents)


if __name__ == "__main__":
    unittest.main()
