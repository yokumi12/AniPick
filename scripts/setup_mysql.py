import getpass
import os
import secrets
import sys
import tempfile
from pathlib import Path

import mysql.connector
from mysql.connector import Error


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
DATABASE_NAME = "anipick"
APP_USER = "anipick_app"
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 3306


def write_environment_settings(settings):
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    updated = set()
    output = []
    for line in lines:
        name, separator, _ = line.partition("=")
        key = name.strip() if separator else None
        if key in settings:
            if key not in updated:
                output.append(f"{key}={settings[key]}")
                updated.add(key)
        else:
            output.append(line)
    for key, value in settings.items():
        if key not in updated:
            output.append(f"{key}={value}")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=ENV_PATH.parent,
            prefix=".env-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write("\n".join(output) + "\n")
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, ENV_PATH)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def main():
    root_user = input("MySQL 관리자 아이디 [root]: ").strip() or "root"
    root_password = getpass.getpass("MySQL 관리자 비밀번호 (입력 내용은 표시되지 않음): ")
    if not root_password:
        print("비밀번호를 입력하지 않아 설정을 취소했습니다.", file=sys.stderr)
        return 1

    app_password = secrets.token_urlsafe(36)
    connection = None
    try:
        connection = mysql.connector.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=root_user,
            password=root_password,
            connection_timeout=5,
        )
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{DATABASE_NAME}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.execute(
            f"CREATE USER IF NOT EXISTS '{APP_USER}'@'{MYSQL_HOST}' IDENTIFIED BY %s",
            (app_password,),
        )
        cursor.execute(
            f"ALTER USER '{APP_USER}'@'{MYSQL_HOST}' IDENTIFIED BY %s",
            (app_password,),
        )
        cursor.execute(
            f"GRANT ALL PRIVILEGES ON `{DATABASE_NAME}`.* "
            f"TO '{APP_USER}'@'{MYSQL_HOST}'"
        )
        connection.commit()
        cursor.close()
        connection.close()
        connection = None

        app_connection = mysql.connector.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=APP_USER,
            password=app_password,
            database=DATABASE_NAME,
            connection_timeout=5,
        )
        app_connection.close()

        write_environment_settings(
            {
                "ANIPICK_MYSQL_HOST": MYSQL_HOST,
                "ANIPICK_MYSQL_PORT": MYSQL_PORT,
                "ANIPICK_MYSQL_USER": APP_USER,
                "ANIPICK_MYSQL_PASSWORD": app_password,
                "ANIPICK_MYSQL_DATABASE": DATABASE_NAME,
            }
        )
    except Error as error:
        print(
            f"MySQL 설정에 실패했습니다: {error.__class__.__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    print("MySQL 연결과 전용 AniPick 계정을 확인했습니다.")
    print("접속 정보는 Git에 포함되지 않는 프로젝트 .env 파일에 저장했습니다.")
    print("이제 python server.py를 실행하면 MySQL을 사용합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
