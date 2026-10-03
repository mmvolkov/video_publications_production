import pytest

from app import config


@pytest.fixture(autouse=True)
def no_site_password(monkeypatch):
    """В тестах сайт без пароля; тесты авторизации включают его сами."""
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "APP_PASSWORD_HASH", "")


@pytest.fixture(autouse=True)
def no_claude_code_cli(monkeypatch):
    """Не вызывать настоящий Claude Code в тестах; тесты движка подставляют свой CLI."""
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", "claude-code-not-installed")
    monkeypatch.delenv("AI_ENGINE", raising=False)
