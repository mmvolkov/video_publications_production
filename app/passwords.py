"""Хеши паролей (PBKDF2-SHA256 с солью) — чтобы в коде и .env не лежал пароль открытым текстом.

Сгенерировать хеш для APP_PASSWORD_HASH:
    python -m app.passwords 'мой пароль'
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from functools import lru_cache

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"{ALGORITHM}${iterations}${b64(salt)}${b64(digest)}"


@lru_cache(maxsize=64)  # PBKDF2 намеренно медленный — не пересчитываем на каждый запрос
def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Использование: python -m app.passwords 'пароль'")
    print(hash_password(sys.argv[1]))
