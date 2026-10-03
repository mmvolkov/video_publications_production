import pytest

from app import config


@pytest.fixture(autouse=True)
def no_site_password(monkeypatch):
    """В тестах сайт без пароля; тесты авторизации включают его сами."""
    monkeypatch.setattr(config, "APP_PASSWORD", "")
