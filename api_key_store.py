import os
import tempfile
import threading

from cryptography.fernet import Fernet, InvalidToken

import database


ENCRYPTION_KEY_ENV = "ANIPICK_API_KEY_ENCRYPTION_KEY"
_key_lock = threading.Lock()


class ApiKeyStoreError(Exception):
    pass


def _write_encryption_key_to_local_environment(key):
    env_path = database.ENV_PATH
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    replacement = f"{ENCRYPTION_KEY_ENV}={key.decode('ascii')}"
    output = []
    replaced = False
    for line in lines:
        name, separator, _ = line.partition("=")
        if separator and name.strip() == ENCRYPTION_KEY_ENV:
            if not replaced:
                output.append(replacement)
                replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(replacement)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=env_path.parent,
            prefix=".env-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write("\n".join(output) + "\n")
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, env_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    os.environ[ENCRYPTION_KEY_ENV] = key.decode("ascii")


def _fernet(create_local_key=False):
    with _key_lock:
        configured_key = os.environ.get(ENCRYPTION_KEY_ENV, "").strip()
        if not configured_key and create_local_key:
            if os.environ.get("ANIPICK_ENV", "").strip().lower() == "production":
                raise ApiKeyStoreError(
                    "운영 환경의 API 키 암호화 키가 서버 비밀 설정에 없습니다."
                )
            key = Fernet.generate_key()
            try:
                _write_encryption_key_to_local_environment(key)
            except OSError as error:
                raise ApiKeyStoreError(
                    "API 키 암호화 설정을 로컬 환경 파일에 저장하지 못했어요."
                ) from error
            configured_key = key.decode("ascii")
    if not configured_key:
        raise ApiKeyStoreError("API 키 암호화 설정이 서버에 없습니다.")
    try:
        return Fernet(configured_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise ApiKeyStoreError("API 키 암호화 설정이 올바르지 않아요.") from error


def encryption_is_available():
    if os.environ.get("ANIPICK_ENV", "").strip().lower() != "production":
        return True
    try:
        _fernet()
    except ApiKeyStoreError:
        return False
    return True


def encrypt_api_key(api_key):
    return _fernet(create_local_key=True).encrypt(api_key.encode("utf-8"))


def get_user_api_keys(user_id):
    encrypted_keys = database.get_user_ai_api_keys(user_id)
    if not encrypted_keys:
        return {}
    fernet = _fernet()
    try:
        return {
            provider: fernet.decrypt(encrypted_key).decode("utf-8")
            for provider, encrypted_key in encrypted_keys.items()
        }
    except (InvalidToken, UnicodeDecodeError) as error:
        raise ApiKeyStoreError(
            "저장된 API 키를 복호화할 수 없어요. 서버 암호화 키 설정을 확인해주세요."
        ) from error
