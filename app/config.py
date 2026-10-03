"""Настройки приложения из переменных окружения."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

# Claude
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium")

# Доступ к сайту (HTTP Basic), по порядку:
#   APP_PASSWORD=off      — сайт открыт без пароля;
#   APP_PASSWORD=...      — этот пароль;
#   APP_PASSWORD_HASH=... — хеш пароля (python -m app.passwords 'пароль');
#   ничего не задано      — пароль по умолчанию, в коде хранится только его хеш.
DEFAULT_PASSWORD_HASH = "pbkdf2_sha256$600000$cYTLHp9i7t7KmTDmo7qHcg==$4+KjiXN98tjvvqnWkxbWihHvYG2t7GMqCF9R2Ki/dYI="
APP_USER = os.getenv("APP_USER", "").strip() or "admin"
_password = os.getenv("APP_PASSWORD", "").strip()
_disabled = _password.lower() in ("off", "none", "false", "0")
APP_PASSWORD = "" if _disabled else _password
APP_PASSWORD_HASH = "" if _disabled or _password else (os.getenv("APP_PASSWORD_HASH", "").strip() or DEFAULT_PASSWORD_HASH)

# Интеграции
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Видео
WIDTH = 1080
HEIGHT = 1920
FPS = int(os.getenv("REEL_FPS", "30"))
FONT_PATH = os.getenv("FONT_PATH", "")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
