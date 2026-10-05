"""Движок сценариев через официальный Claude Code CLI — с поддельным `claude` вместо настоящего."""
import json
import os
import stat
import sys
import textwrap

import pytest

from app import ai, config

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    request = json.loads(sys.stdin.readline())
    with open(os.environ["FAKE_CLAUDE_LOG"], "w") as f:
        json.dump({{"args": args, "request": request, "api_key": os.getenv("ANTHROPIC_API_KEY")}}, f)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    print(json.dumps({{"type": "system", "subtype": "init"}}))
    if mode == "ok":
        script = {{"title": "Т", "cover_text": "О", "caption": "П", "hashtags": ["a"],
                  "scenes": [{{"material_id": "", "text": "Хук", "voice": "", "duration": 3}}]}}
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "structured_output": script}}))
    elif mode == "error":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"}}))
    else:
        sys.stderr.write("boom")
        sys.exit(1)
''')

PROJECT = {"id": "p1", "title": "Кофейня", "brief": {"topic": "Кофе"}, "materials": []}
OPTIONS = {"duration": 15}


@pytest.fixture()
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLI.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setattr(config, "CLAUDE_CODE_BIN", str(path))
    monkeypatch.setattr(config, "CLAUDE_CODE_MODEL", "")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return lambda: json.loads(log.read_text())


def test_engine_selection(fake_cli, monkeypatch):
    assert ai.engine() == "claude-code"  # ключа API нет, есть CLI с токеном
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert ai.engine() == "api"  # auto: ключ API в приоритете
    monkeypatch.setenv("AI_ENGINE", "claude-code")
    assert ai.engine() == "claude-code"
    monkeypatch.setenv("AI_ENGINE", "off")
    assert ai.engine() == "draft" and not ai.ai_available()
    monkeypatch.setenv("AI_ENGINE", "auto")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv("HOME", os.fspath(config.DATA_DIR / "no-such-home"))
    assert ai.engine() == "draft"  # CLI есть, но вход не выполнен


def test_engine_without_cli(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert ai.engine() == "draft"


def test_generate_with_claude_code(fake_cli, monkeypatch):
    monkeypatch.setenv("AI_ENGINE", "claude-code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setattr(config, "CLAUDE_CODE_MODEL", "sonnet")
    script, source = ai.generate_script(PROJECT, OPTIONS)
    assert source == "claude-code"
    assert script["scenes"][0]["text"] == "Хук" and script["hashtags"] == ["#a"]

    call = fake_cli()
    args = call["args"]
    assert args[0] == "-p" and args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "sonnet"
    assert json.loads(args[args.index("--json-schema") + 1]) == ai.SCRIPT_SCHEMA
    assert args[args.index("--system-prompt") + 1] == ai.SYSTEM_PROMPT
    assert call["api_key"] is None  # иначе CLI ушёл бы в оплату по API вместо подписки
    content = call["request"]["message"]["content"]
    assert call["request"]["type"] == "user" and "Кофе" in content[0]["text"]


@pytest.mark.parametrize("mode,message", [("error", "Not logged in"), ("crash", "boom")])
def test_claude_code_errors(fake_cli, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(RuntimeError, match=message):
        ai.generate_with_claude_code(PROJECT, OPTIONS)
