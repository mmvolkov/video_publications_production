"""Настройки приложения из переменных окружения."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

# Claude
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium")

# Доступ к сайту (HTTP Basic). Если APP_PASSWORD не задан или пустой — пароль по умолчанию;
# APP_PASSWORD=off — сайт открыт без пароля.
DEFAULT_PASSWORD = "U$er0k!"
APP_USER = os.getenv("APP_USER", "").strip() or "admin"
_password = os.getenv("APP_PASSWORD", "").strip()
APP_PASSWORD = "" if _password.lower() in ("off", "none", "false", "0") else (_password or DEFAULT_PASSWORD)

# Интеграции
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Видео
WIDTH = 1080
HEIGHT = 1920
FPS = int(os.getenv("REEL_FPS", "30"))
FONT_PATH = os.getenv("FONT_PATH", "")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
