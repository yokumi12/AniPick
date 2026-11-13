import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
CATALOG_PATH = DATA_DIR / "anime.json"
KOREAN_OVERRIDES_PATH = DATA_DIR / "korean-overrides.json"
YOUTUBE_OVERRIDES_PATH = DATA_DIR / "youtube-overrides.json"
ENV_PATH = PROJECT_ROOT / ".env"


def _load_local_environment():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


_load_local_environment()


class MySQLConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=()):
        cursor = self.connection.cursor(dictionary=True)
        cursor.execute(statement.replace("?", "%s"), parameters)
        return MySQLCursor(cursor)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


class MySQLCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        if not cursor.with_rows:
            cursor.close()

    def fetchone(self):
        try:
            return self.cursor.fetchone()
        finally:
            self.cursor.close()

    def fetchall(self):
        try:
            return self.cursor.fetchall()
        finally:
            self.cursor.close()


@contextmanager
def connect():
    import mysql.connector

    ssl_ca = os.environ.get("ANIPICK_MYSQL_SSL_CA", "").strip()
    if (
        os.environ.get("ANIPICK_ENV", "").strip().lower() == "production"
        and not ssl_ca
    ):
        raise RuntimeError("운영 환경의 MySQL TLS 인증서 설정이 없습니다.")

    connection_options = {
        "host": os.environ.get("ANIPICK_MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("ANIPICK_MYSQL_PORT", "3306")),
        "user": os.environ.get("ANIPICK_MYSQL_USER", ""),
        "password": os.environ.get("ANIPICK_MYSQL_PASSWORD", ""),
        "database": os.environ.get("ANIPICK_MYSQL_DATABASE", "anipick"),
        "charset": "utf8mb4",
        "use_unicode": True,
        "connection_timeout": 5,
    }
    if ssl_ca:
        connection_options.update(
            ssl_ca=ssl_ca,
            ssl_verify_cert=True,
            ssl_verify_identity=True,
        )

    connection = MySQLConnection(
        mysql.connector.connect(**connection_options)
    )
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    _initialize_mysql_database()


def _initialize_mysql_database():
    with connect() as connection:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS anime (
                id VARCHAR(64) PRIMARY KEY,
                title VARCHAR(512) NOT NULL,
                title_ko VARCHAR(512),
                titles_json JSON NOT NULL,
                year SMALLINT,
                episodes SMALLINT,
                score DECIMAL(5, 2),
                synopsis TEXT NOT NULL,
                synopsis_ko TEXT,
                poster_image TEXT,
                source_url TEXT,
                youtube_video_id VARCHAR(32),
                related_anime_json JSON NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS genre (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(191) NOT NULL UNIQUE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS anime_genre (
                anime_id VARCHAR(64) NOT NULL,
                genre_id BIGINT UNSIGNED NOT NULL,
                PRIMARY KEY (anime_id, genre_id),
                FOREIGN KEY (anime_id) REFERENCES anime(id) ON DELETE CASCADE,
                FOREIGN KEY (genre_id) REFERENCES genre(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_synopsis_cache (
                anime_id VARCHAR(64) PRIMARY KEY,
                source_hash CHAR(64) NOT NULL,
                summary TEXT NOT NULL,
                provider VARCHAR(16) NOT NULL,
                FOREIGN KEY (anime_id) REFERENCES anime(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_tags_cache (
                anime_id VARCHAR(64) PRIMARY KEY,
                source_hash CHAR(64) NOT NULL,
                tags_json TEXT NOT NULL,
                provider VARCHAR(16) NOT NULL,
                FOREIGN KEY (anime_id) REFERENCES anime(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS user_account (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(20) NOT NULL UNIQUE,
                password_salt VARBINARY(16) NOT NULL,
                password_hash VARBINARY(32) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS user_session (
                token_hash CHAR(64) PRIMARY KEY,
                user_id BIGINT UNSIGNED NOT NULL,
                csrf_token VARCHAR(128) NOT NULL,
                expires_at BIGINT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX user_session_user_id_idx (user_id),
                FOREIGN KEY (user_id) REFERENCES user_account(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS user_ai_api_key (
                user_id BIGINT UNSIGNED NOT NULL,
                provider VARCHAR(16) NOT NULL,
                encrypted_key VARBINARY(8192) NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, provider),
                FOREIGN KEY (user_id) REFERENCES user_account(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS favorite (
                user_id BIGINT UNSIGNED NOT NULL,
                anime_id VARCHAR(64) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, anime_id),
                FOREIGN KEY (user_id) REFERENCES user_account(id) ON DELETE CASCADE,
                FOREIGN KEY (anime_id) REFERENCES anime(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_curator_conversation (
                id VARCHAR(64) PRIMARY KEY,
                user_id BIGINT UNSIGNED NOT NULL,
                title VARCHAR(300) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX ai_curator_conversation_user_idx (user_id, updated_at),
                FOREIGN KEY (user_id) REFERENCES user_account(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_curator_message (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                conversation_id VARCHAR(64) NOT NULL,
                role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL,
                recommendations_json TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX ai_curator_message_conversation_idx (conversation_id, id),
                FOREIGN KEY (conversation_id)
                    REFERENCES ai_curator_conversation(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        )
        for statement in statements:
            connection.execute(statement)
        columns = {
            row["Field"]
            for row in connection.execute("SHOW COLUMNS FROM anime").fetchall()
        }
        if "youtube_video_id" not in columns:
            connection.execute("ALTER TABLE anime ADD COLUMN youtube_video_id VARCHAR(32)")
        if "related_anime_json" not in columns:
            connection.execute(
                "ALTER TABLE anime ADD COLUMN related_anime_json JSON NOT NULL"
            )
        import_catalog(connection)


def integrity_error_type():
    import mysql.connector

    return mysql.connector.IntegrityError


def import_catalog(connection):
    if not CATALOG_PATH.exists():
        return 0

    snapshot = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    items = snapshot.get("items", [])
    if not items:
        return 0

    overrides = {}
    if KOREAN_OVERRIDES_PATH.exists():
        overrides = json.loads(KOREAN_OVERRIDES_PATH.read_text(encoding="utf-8"))
    youtube_overrides = {}
    if YOUTUBE_OVERRIDES_PATH.exists():
        youtube_overrides = json.loads(
            YOUTUBE_OVERRIDES_PATH.read_text(encoding="utf-8")
        )

    catalog_ids = {str(item["id"]) for item in items}
    if (
        os.environ.get("ANIPICK_ENV", "").strip().lower() != "production"
        or os.environ.get("ANIPICK_ALLOW_CATALOG_DELETIONS") == "1"
    ):
        placeholders = ",".join("?" for _ in catalog_ids)
        connection.execute(
            f"DELETE FROM anime WHERE id NOT IN ({placeholders})",
            tuple(catalog_ids),
        )

    for item in items:
        anime_id = str(item["id"])
        title = item.get("title") or item.get("slug") or anime_id
        localized = overrides.get(anime_id, {})
        related_anime = []
        for related_item in item.get("related_anime") or []:
            related_localized = overrides.get(str(related_item.get("id")), {})
            localized_related = dict(related_item)
            if related_localized.get("title_ko"):
                localized_related["title_ko"] = related_localized["title_ko"]
            related_anime.append(localized_related)
        values = (
            anime_id,
            title,
            localized.get("title_ko"),
            json.dumps(item.get("titles") or {}, ensure_ascii=False),
            item.get("year"),
            item.get("episodes"),
            item.get("score"),
            item.get("synopsis") or "",
            localized.get("synopsis_ko"),
            item.get("poster_image"),
            item.get("url"),
            youtube_overrides.get(anime_id, item.get("youtube_video_id")),
            json.dumps(related_anime, ensure_ascii=False),
        )
        connection.execute(
            """
            INSERT INTO anime (id, title, title_ko, titles_json, year, episodes,
                               score, synopsis, synopsis_ko, poster_image, source_url,
                               youtube_video_id, related_anime_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                title_ko = VALUES(title_ko),
                titles_json = VALUES(titles_json),
                year = VALUES(year),
                episodes = VALUES(episodes),
                score = VALUES(score),
                synopsis = VALUES(synopsis),
                synopsis_ko = VALUES(synopsis_ko),
                poster_image = VALUES(poster_image),
                source_url = VALUES(source_url),
                youtube_video_id = VALUES(youtube_video_id),
                related_anime_json = VALUES(related_anime_json)
            """,
            values,
        )

        connection.execute("DELETE FROM anime_genre WHERE anime_id = ?", (anime_id,))
        for name in dict.fromkeys(item.get("genres") or []):
            connection.execute("INSERT IGNORE INTO genre (name) VALUES (?)", (name,))
            genre_id = connection.execute(
                "SELECT id FROM genre WHERE name = ?", (name,)
            ).fetchone()["id"]
            connection.execute(
                "INSERT IGNORE INTO anime_genre (anime_id, genre_id) VALUES (?, ?)",
                (anime_id, genre_id),
            )

    connection.execute(
        "DELETE FROM genre WHERE id NOT IN (SELECT DISTINCT genre_id FROM anime_genre)"
    )
    return len(items)


def get_catalog():
    with connect() as connection:
        anime_rows = connection.execute(
            "SELECT id, title, title_ko, titles_json, year, episodes, score, "
            "synopsis, synopsis_ko, "
            "poster_image, source_url, youtube_video_id, related_anime_json FROM anime "
            "ORDER BY score IS NULL, score DESC, LOWER(title)"
        ).fetchall()
        genre_rows = connection.execute(
            "SELECT ag.anime_id, g.name FROM anime_genre ag "
            "JOIN genre g ON g.id = ag.genre_id ORDER BY LOWER(g.name)"
        ).fetchall()
        tag_rows = connection.execute(
            "SELECT anime_id, tags_json FROM ai_tags_cache"
        ).fetchall()

    genres_by_anime = {}
    for row in genre_rows:
        genres_by_anime.setdefault(row["anime_id"], []).append(row["name"])
    tags_by_anime = {}
    for row in tag_rows:
        try:
            tags = json.loads(row["tags_json"])
        except (TypeError, ValueError):
            tags = []
        if isinstance(tags, list):
            tags_by_anime[row["anime_id"]] = tags
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "title_ko": row["title_ko"],
            "titles": json.loads(row["titles_json"]),
            "year": row["year"],
            "episodes": row["episodes"],
            "score": float(row["score"]) if row["score"] is not None else None,
            "genres": genres_by_anime.get(row["id"], []),
            "ai_tags": tags_by_anime.get(row["id"], []),
            "synopsis": row["synopsis"],
            "synopsis_ko": row["synopsis_ko"],
            "poster_image": row["poster_image"],
            "url": row["source_url"],
            "youtube_video_id": row["youtube_video_id"],
            "related_anime": json.loads(row["related_anime_json"] or "[]"),
            "saved": False,
        }
        for row in anime_rows
    ]


def get_genres():
    with connect() as connection:
        rows = connection.execute(
            "SELECT name FROM genre ORDER BY LOWER(name)"
        ).fetchall()
    return [row["name"] for row in rows]


def get_ai_synopsis(anime_id, source_hash):
    with connect() as connection:
        row = connection.execute(
            "SELECT summary, provider FROM ai_synopsis_cache "
            "WHERE anime_id = ? AND source_hash = ?",
            (anime_id, source_hash),
        ).fetchone()
    return dict(row) if row else None


def list_curator_conversations(user_id, limit=20):
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, title, updated_at FROM ai_curator_conversation "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def get_curator_conversation(user_id, conversation_id):
    with connect() as connection:
        conversation = connection.execute(
            "SELECT id, title, updated_at FROM ai_curator_conversation "
            "WHERE user_id = ? AND id = ?",
            (user_id, conversation_id),
        ).fetchone()
        if not conversation:
            return None
        rows = connection.execute(
            "SELECT role, content, recommendations_json FROM ai_curator_message "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    messages = []
    for row in rows:
        try:
            recommendations = json.loads(row["recommendations_json"])
        except (TypeError, ValueError):
            recommendations = []
        messages.append(
            {
                "role": row["role"],
                "content": row["content"],
                "recommendations": recommendations
                if isinstance(recommendations, list)
                else [],
            }
        )
    return {
        "id": conversation["id"],
        "title": conversation["title"],
        "updated_at": str(conversation["updated_at"]),
        "messages": messages,
    }


def delete_curator_conversation(user_id, conversation_id):
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM ai_curator_conversation WHERE user_id = ? AND id = ?",
            (user_id, conversation_id),
        )
        return cursor.rowcount == 1


def save_curator_turn(user_id, conversation_id, user_message, assistant_message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = user_message[:300]
    with connect() as connection:
        existing = connection.execute(
            "SELECT id FROM ai_curator_conversation "
            "WHERE user_id = ? AND id = ?",
            (user_id, conversation_id),
        ).fetchone()
        if not existing:
            connection.execute(
                "INSERT INTO ai_curator_conversation "
                "(id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, user_id, title, now, now),
            )
        else:
            connection.execute(
                "UPDATE ai_curator_conversation SET updated_at = ? "
                "WHERE user_id = ? AND id = ?",
                (now, user_id, conversation_id),
            )

        connection.execute(
            "INSERT INTO ai_curator_message "
            "(conversation_id, role, content, recommendations_json) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, "user", user_message, "[]"),
        )
        connection.execute(
            "INSERT INTO ai_curator_message "
            "(conversation_id, role, content, recommendations_json) "
            "VALUES (?, ?, ?, ?)",
            (
                conversation_id,
                "assistant",
                assistant_message["message"],
                json.dumps(assistant_message["recommendations"], ensure_ascii=False),
            ),
        )

        old_messages = connection.execute(
            "SELECT id FROM ai_curator_message WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT 40",
            (conversation_id,),
        ).fetchall()
        if old_messages:
            oldest_id = min(row["id"] for row in old_messages)
            connection.execute(
                "DELETE FROM ai_curator_message "
                "WHERE conversation_id = ? AND id < ?",
                (conversation_id, oldest_id),
            )

        old_conversations = connection.execute(
            "SELECT id FROM ai_curator_conversation WHERE user_id = ? "
            "ORDER BY updated_at DESC LIMIT 20",
            (user_id,),
        ).fetchall()
        keep_ids = {row["id"] for row in old_conversations}
        for row in connection.execute(
            "SELECT id FROM ai_curator_conversation WHERE user_id = ?",
            (user_id,),
        ).fetchall():
            if row["id"] not in keep_ids:
                connection.execute(
                    "DELETE FROM ai_curator_conversation WHERE user_id = ? AND id = ?",
                    (user_id, row["id"]),
                )


def save_ai_synopsis(anime_id, source_hash, summary, provider):
    statement = (
        "INSERT INTO ai_synopsis_cache (anime_id, source_hash, summary, provider) "
        "VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE "
        "source_hash = VALUES(source_hash), summary = VALUES(summary), "
        "provider = VALUES(provider)"
    )
    with connect() as connection:
        connection.execute(statement, (anime_id, source_hash, summary, provider))


def get_ai_tags(anime_id, source_hash):
    with connect() as connection:
        row = connection.execute(
            "SELECT tags_json, provider FROM ai_tags_cache "
            "WHERE anime_id = ? AND source_hash = ?",
            (anime_id, source_hash),
        ).fetchone()
    if not row:
        return None
    try:
        tags = json.loads(row["tags_json"])
    except (TypeError, ValueError):
        return None
    return {"tags": tags, "provider": row["provider"]} if isinstance(tags, list) else None


def save_ai_tags(anime_id, source_hash, tags, provider):
    statement = (
        "INSERT INTO ai_tags_cache (anime_id, source_hash, tags_json, provider) "
        "VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE "
        "source_hash = VALUES(source_hash), tags_json = VALUES(tags_json), "
        "provider = VALUES(provider)"
    )
    with connect() as connection:
        connection.execute(
            statement,
            (anime_id, source_hash, json.dumps(tags, ensure_ascii=False), provider),
        )


def create_user(username, password_salt, password_hash):
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO user_account (username, password_salt, password_hash) "
            "VALUES (?, ?, ?)",
            (username, password_salt, password_hash),
        )
        return cursor.lastrowid


def get_user_by_username(username):
    with connect() as connection:
        row = connection.execute(
            "SELECT id, username, password_salt, password_hash "
            "FROM user_account WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def delete_account(user_id):
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM user_account WHERE id = ?",
            (user_id,),
        )
        return cursor.rowcount == 1


def create_session(token_hash, user_id, csrf_token, expires_at):
    with connect() as connection:
        connection.execute(
            "DELETE FROM user_session WHERE expires_at <= ?", (int(time.time()),)
        )
        connection.execute(
            "INSERT INTO user_session (token_hash, user_id, csrf_token, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, user_id, csrf_token, expires_at),
        )


def get_session(token_hash):
    now = int(time.time())
    with connect() as connection:
        connection.execute("DELETE FROM user_session WHERE expires_at <= ?", (now,))
        row = connection.execute(
            "SELECT s.user_id, s.csrf_token, u.username FROM user_session s "
            "JOIN user_account u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, now),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token_hash):
    with connect() as connection:
        connection.execute("DELETE FROM user_session WHERE token_hash = ?", (token_hash,))


def save_user_ai_api_keys(user_id, encrypted_keys):
    with connect() as connection:
        for provider, encrypted_key in encrypted_keys.items():
            connection.execute(
                "INSERT INTO user_ai_api_key (user_id, provider, encrypted_key) "
                "VALUES (?, ?, ?) ON DUPLICATE KEY UPDATE "
                "encrypted_key = VALUES(encrypted_key)",
                (user_id, provider, encrypted_key),
            )


def get_user_ai_api_keys(user_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT provider, encrypted_key FROM user_ai_api_key WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {row["provider"]: bytes(row["encrypted_key"]) for row in rows}


def get_user_ai_api_key_providers(user_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT provider FROM user_ai_api_key WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {row["provider"] for row in rows}


def delete_user_ai_api_key(user_id, provider):
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM user_ai_api_key WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        return cursor.rowcount == 1


def get_favorites(user_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT anime_id FROM favorite WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [row["anime_id"] for row in rows]


def set_favorite(user_id, anime_id, saved):
    with connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM anime WHERE id = ?", (anime_id,)
        ).fetchone()
        if not exists:
            return False
        if saved:
            connection.execute(
                "INSERT IGNORE INTO favorite (user_id, anime_id) VALUES (?, ?)",
                (user_id, anime_id),
            )
        else:
            connection.execute(
                "DELETE FROM favorite WHERE user_id = ? AND anime_id = ?",
                (user_id, anime_id),
            )
    return True
