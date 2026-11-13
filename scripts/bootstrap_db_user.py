import os
import re
import time

import mysql.connector


DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
APP_USERNAME = "anipick_app"


def main():
    database_name = os.environ.get("ANIPICK_MYSQL_DATABASE", "anipick")
    if not DATABASE_NAME_PATTERN.fullmatch(database_name):
        raise RuntimeError("The configured database name is invalid.")

    required = (
        "ANIPICK_DB_ADMIN_USER",
        "ANIPICK_DB_ADMIN_PASSWORD",
        "ANIPICK_DB_APP_PASSWORD",
        "ANIPICK_MYSQL_HOST",
        "ANIPICK_MYSQL_SSL_CA",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing database bootstrap settings: {', '.join(missing)}")

    app_password = os.environ["ANIPICK_DB_APP_PASSWORD"]
    if not re.fullmatch(r"[A-Za-z0-9]{20,128}", app_password):
        raise RuntimeError("The generated application database password is invalid.")

    connection = None
    for attempt in range(30):
        try:
            connection = mysql.connector.connect(
                host=os.environ["ANIPICK_MYSQL_HOST"],
                port=int(os.environ.get("ANIPICK_MYSQL_PORT", "3306")),
                user=os.environ["ANIPICK_DB_ADMIN_USER"],
                password=os.environ["ANIPICK_DB_ADMIN_PASSWORD"],
                database=database_name,
                charset="utf8mb4",
                use_unicode=True,
                connection_timeout=5,
                ssl_ca=os.environ["ANIPICK_MYSQL_SSL_CA"],
                ssl_verify_cert=True,
                ssl_verify_identity=True,
            )
            break
        except mysql.connector.Error as error:
            if attempt == 29:
                raise RuntimeError("Could not connect to the RDS bootstrap database.") from error
            print(f"Waiting for RDS database (attempt {attempt + 1}/30).")
            time.sleep(10)

    try:
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE USER IF NOT EXISTS '{APP_USERNAME}'@'%' IDENTIFIED BY %s",
            (app_password,),
        )
        cursor.execute(
            f"ALTER USER '{APP_USERNAME}'@'%' IDENTIFIED BY %s",
            (app_password,),
        )
        cursor.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES "
            f"ON `{database_name}`.* TO '{APP_USERNAME}'@'%'"
        )
        connection.commit()
        cursor.close()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
