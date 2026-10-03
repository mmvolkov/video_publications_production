"""Настройки приложения из переменных окружения."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

# Claude
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium")

# Доступ к сайту (HTTP Basic). Если APP_PASSWORD пустой — сайт открыт.
APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# Интеграции
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Видео
WIDTH = 1080
HEIGHT = 1920
FPS = int(os.getenv("REEL_FPS", "30"))
FONT_PATH = os.getenv("FONT_PATH", "")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
