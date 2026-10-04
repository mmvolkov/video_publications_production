"""Сценарий рилса: Claude смотрит материалы и пишет раскадровку.

Если ключ Anthropic не задан, строится простой черновой сценарий без ИИ,
чтобы сайт работал «из коробки».
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, media, storage

MAX_SCENES = 12
MAX_IMAGES_FOR_AI = 24
FRAMES_PER_VIDEO = 4  # кадров с таймкодами на одно видео — чтобы выбрать сильный момент

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Рабочее название рилса"},
        "cover_text": {"type": "string", "description": "Короткий текст для обложки (до 40 символов)"},
        "cover_material_id": {"type": "string",
                              "description": "id самого выразительного фото/видео для обложки или пустая строка"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "material_id": {
                        "type": "string",
                        "description": "id фото/видео из материалов или пустая строка для текстовой карточки",
                    },
                    "text": {"type": "string",
                             "description": "Текст на экране, до 6–7 слов; 1–2 ключевых слова в *звёздочках* выделяются цветом"},
                    "voice": {"type": "string", "description": "Фраза диктора для этой сцены или пустая строка, если озвучка выключена"},
                    "duration": {"type": "number", "description": "Длительность сцены в секундах (1.5–8)"},
                    "start": {"type": "number",
                              "description": "Для видео — с какой секунды ролика начинать сцену (по кадрам с таймкодами); "
                                             "для фото и карточек 0"},
                },
                "required": ["material_id", "text", "voice", "duration", "start"],
                "additionalProperties": False,
            },
        },
        "caption": {"type": "string", "description": "Подпись к посту в Instagram"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "cover_text", "cover_material_id", "scenes", "caption", "hashtags"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты — сильный SMM-продюсер и сценарист вертикальных видео (Instagram Reels).
Из материалов пользователя ты собираешь раскадровку рилса, который удерживает внимание и продаёт.

Правила:
- Первая сцена — хук на 1.5–3 секунды: интрига, боль, цифра или смелое обещание. Без «Привет, сегодня расскажу».
- Одна сцена = одна мысль. Текст на экране короткий и разговорный: до 6–7 слов, без хэштегов.
- В тексте на экране выделяй 1–2 ключевых слова звёздочками: «Вы теряете *половину* клиентов» — они будут жёлтыми.
- Картинка меняется каждые 2–3 секунды: длинную мысль разбивай на несколько сцен с разными кадрами.
- Используй только material_id из списка. Пустой material_id = текстовая карточка на цветном фоне (для хука, вывода или призыва).
- Выбирай самые выразительные кадры, порядок — по логике истории (проблема → решение → результат → призыв).
- У видео ты видишь несколько кадров с таймкодами. Выбирай самый сильный момент и ставь его секунду в start;
  сцена не должна выходить за конец ролика (start + duration ≤ длительность). Один ролик можно брать
  в нескольких сценах с разных моментов. Для фото и карточек start = 0.
- Для обложки выбери самый выразительный кадр (cover_material_id) — он должен цеплять в сетке профиля.
- Последняя сцена — понятный призыв к действию. Хорошо, если финал перекликается с хуком и ролик
  хочется пересмотреть («закольцованный» рилс).
- Суммарная длительность — близко к целевой.
- Подпись к посту: живая, 300–900 символов, с абзацами и эмодзи по делу, в конце призыв. Хэштеги — отдельным списком, 5–12 штук, каждый начинается с «#».
- Пиши на языке, на котором написан бриф (по умолчанию — русский).

Озвучка (если в брифе сказано, что она включена):
- В поле voice — живая разговорная фраза диктора для сцены: одно короткое предложение (2–4 секунды),
  без хэштегов, эмодзи, списков и звёздочек.
- Озвучка дополняет текст на экране, а не дублирует его слово в слово; на экране — короткий тезис, голосом — мысль целиком.
- Длительность сцены ставь под фразу: примерно 14 символов фразы в секунду плюс полсекунды.
- Числа и сокращения пиши так, как их надо произнести («двадцать процентов», а не «20%»).
Если озвучка выключена — оставляй voice пустой строкой."""


ENGINE_LABELS = {
    "api": "Claude API",
    "claude-code": "Claude Code (подписка)",
    "draft": "без ИИ",
}


def _claude_code_logged_in() -> bool:
    """Есть официальный CLI Claude Code и вход в него: токен `claude setup-token` или сохранённый логин."""
    if not shutil.which(config.CLAUDE_CODE_BIN):
        return False
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    return (Path.home() / ".claude" / ".credentials.json").exists()


def engine() -> str:
    """Чем писать сценарии: 'api' (ключ Anthropic), 'claude-code' (подписка через CLI) или 'draft'.

    AI_ENGINE=auto (по умолчанию) выбирает первое доступное в этом порядке.
    """
    wanted = os.getenv("AI_ENGINE", "auto").strip().lower()
    has_api = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    if wanted == "api":
        return "api" if has_api else "draft"
    if wanted in ("claude-code", "claude_code", "cli"):
        return "claude-code" if _claude_code_logged_in() else "draft"
    if wanted in ("off", "draft", "none"):
        return "draft"
    if has_api:
        return "api"
    return "claude-code" if _claude_code_logged_in() else "draft"


def ai_available() -> bool:
    return engine() != "draft"


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _brief_text(project: dict, options: dict) -> str:
    brief = project.get("brief") or {}
    lines = [f"Проект: {project['title']}"]
    labels = {
        "topic": "Тема / продукт",
        "goal": "Цель рилса",
        "audience": "Аудитория",
        "tone": "Тон",
        "cta": "Призыв к действию",
    }
    for key, label in labels.items():
        if brief.get(key):
            lines.append(f"{label}: {brief[key]}")
    lines.append(f"Целевая длительность: {options.get('duration', 30)} секунд")
    lines.append("Озвучка: " + ("включена — напиши фразы диктора" if options.get("voiceover") else "выключена"))
    if options.get("wishes"):
        lines.append(f"Пожелания к этому рилсу: {options['wishes']}")
    return "\n".join(lines)


def build_messages(project: dict, options: dict) -> list[dict]:
    pdir = storage.project_dir(project["id"])
    content: list[dict] = [{"type": "text", "text": "БРИФ\n" + _brief_text(project, options)}]

    visual = [m for m in project["materials"] if m["kind"] in ("image", "video")]
    texts = [m for m in project["materials"] if m["kind"] == "text"]
    audio = [m for m in project["materials"] if m["kind"] == "audio"]

    content.append({"type": "text", "text": f"\nВИЗУАЛЬНЫЕ МАТЕРИАЛЫ ({len(visual)} шт.):"})
    videos = sum(1 for m in visual if m["kind"] == "video")
    photos = len(visual) - videos
    per_video = FRAMES_PER_VIDEO
    if videos and photos + videos * per_video > MAX_IMAGES_FOR_AI:
        per_video = max(1, (MAX_IMAGES_FOR_AI - photos) // videos)
    budget = MAX_IMAGES_FOR_AI
    for m in visual:
        desc = f"material_id={m['id']} | тип: {'фото' if m['kind'] == 'image' else 'видео'}"
        if m["kind"] == "video" and m.get("duration"):
            desc += f" | длительность {m['duration']:.1f} c"
        if m.get("note"):
            desc += f" | комментарий автора: {m['note']}"
        content.append({"type": "text", "text": desc})
        if budget <= 0:
            continue
        if m["kind"] == "video":
            for t, frame in video_frames(pdir, m, min(per_video, budget)):
                content.append({"type": "text", "text": f"кадр на {t:.1f} с:"})
                content.append(_image_block(frame))
                budget -= 1
        else:
            thumb = pdir / "thumbs" / f"{m['id']}.jpg"
            if thumb.exists():
                content.append(_image_block(thumb))
                budget -= 1

    for m in texts:
        content.append({"type": "text", "text": f"\nТЕКСТОВЫЙ МАТЕРИАЛ «{m.get('name') or 'заметка'}»:\n{m.get('text', '')}"})

    if audio:
        content.append({"type": "text", "text": "\nЕсть фоновая музыка — её наложат автоматически."})

    content.append({"type": "text", "text": "\nСоставь раскадровку рилса по правилам."})
    return [{"role": "user", "content": content}]


def video_frames(pdir: Path, m: dict, count: int) -> list[tuple[float, Path]]:
    """Кадры видео через равные промежутки с таймкодами (кешируются рядом с превью)."""
    duration = float(m.get("duration") or 0)
    frames: list[tuple[float, Path]] = []
    if count > 1 and duration >= 1.5:
        for k in range(count):
            t = round(duration * (k + 0.5) / count, 1)
            path = pdir / "thumbs" / f"{m['id']}_t{int(t * 10):05d}.jpg"
            if not path.exists():
                try:
                    media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(pdir / "materials" / m["file"]),
                               "-frames:v", "1", "-vf", "scale=480:480:force_original_aspect_ratio=decrease",
                               str(path)], timeout=60)
                except RuntimeError:
                    continue
            if path.exists():
                frames.append((t, path))
    if not frames:  # короткое видео или кадры не извлеклись — обычное превью (снято на ~1 с)
        thumb = pdir / "thumbs" / f"{m['id']}.jpg"
        if thumb.exists():
            frames.append((min(1.0, duration / 2), thumb))
    return frames


def generate_with_claude(project: dict, options: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=build_messages(project, options),
        thinking={"type": "adaptive"},
        output_config={
            "effort": config.ANTHROPIC_EFFORT,
            "format": {"type": "json_schema", "schema": SCRIPT_SCHEMA},
        },
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude отказался обрабатывать эти материалы. Попробуйте изменить бриф или материалы.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Ответ Claude обрезан по лимиту токенов, попробуйте ещё раз.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


def generate_with_claude_code(project: dict, options: dict) -> dict:
    """Сценарий через официальный Claude Code CLI в неинтерактивном режиме (`claude -p`).

    Работает по подписке Claude (вход через `claude setup-token` → CLAUDE_CODE_OAUTH_TOKEN или
    сохранённый логин). Запрос тот же, что для API: бриф, тексты и превью кадров картинками прямо
    в сообщении (--input-format stream-json), ответ — строго по JSON-схеме (--json-schema).
    Инструменты отключены (--tools ""), настройки и MCP пользователя не подгружаются: CLI здесь
    только «мозг», он не читает файлы и не запускает команды.
    """
    message = {"type": "user", "message": {"role": "user", "content": build_messages(project, options)[0]["content"]}}
    cmd = [
        config.CLAUDE_CODE_BIN, "-p",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(SCRIPT_SCHEMA, ensure_ascii=False),
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if config.CLAUDE_CODE_MODEL:
        cmd += ["--model", config.CLAUDE_CODE_MODEL]
    env = dict(os.environ)
    # ключ API у CLI в приоритете над подпиской — в этом режиме он не нужен
    env.pop("ANTHROPIC_API_KEY", None)
    with tempfile.TemporaryDirectory(prefix="reels-cc-") as workdir:
        try:
            proc = subprocess.run(cmd, input=json.dumps(message, ensure_ascii=False) + "\n", capture_output=True,
                                  text=True, timeout=config.CLAUDE_CODE_TIMEOUT, cwd=workdir, env=env)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude Code не ответил за {config.CLAUDE_CODE_TIMEOUT} с") from exc

    result = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
    if result is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"Claude Code завершился без результата (код {proc.returncode}): {tail}")
    if result.get("is_error") or result.get("subtype") != "success":
        detail = result.get("result") or result.get("subtype") or "неизвестная ошибка"
        raise RuntimeError(f"Claude Code: {detail}")
    script = result.get("structured_output")
    if not isinstance(script, dict):
        script = json.loads(result.get("result") or "{}")
    return script


def _clip(text: str, limit: int) -> str:
    """Обрезать по границе слова."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return (cut or text[:limit]) + "…"


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text)
    return [p.strip(" -•\t") for p in parts if len(p.strip(" -•\t")) > 3]


def generate_fallback(project: dict, options: dict) -> dict:
    """Черновой сценарий без ИИ: хук → кадры с подписями → призыв."""
    brief = project.get("brief") or {}
    visual = [m for m in project["materials"] if m["kind"] in ("image", "video")]
    lines: list[str] = []
    for m in project["materials"]:
        if m["kind"] == "text":
            lines.extend(_sentences(m.get("text", "")))

    target = float(options.get("duration", 30))
    speak = bool(options.get("voiceover"))
    hook = brief.get("topic") or project["title"]
    scenes = [{"material_id": "", "text": hook, "voice": hook if speak else "", "duration": 2.5}]
    count = max(1, min(len(visual) or len(lines) or 1, MAX_SCENES - 2))
    per_scene = max(2.0, min(6.0, (target - 5.5) / count))
    for i in range(count):
        m = visual[i] if i < len(visual) else None
        text = (m or {}).get("note") or (lines[i] if i < len(lines) else "")
        duration = per_scene
        if m and m["kind"] == "video" and m.get("duration"):
            duration = min(duration, m["duration"])
        if m or text:
            scenes.append({"material_id": m["id"] if m else "", "text": _clip(text, 120),
                           "voice": text if speak else "", "duration": round(duration, 1)})
    cta = brief.get("cta") or "Подписывайтесь, чтобы не пропустить новое"
    scenes.append({"material_id": "", "text": cta, "voice": cta if speak else "", "duration": 3})

    caption = brief.get("topic") or project["title"]
    if lines:
        caption += "\n\n" + " ".join(lines[:5])
    if brief.get("cta"):
        caption += f"\n\n👉 {brief['cta']}"
    return {
        "title": project["title"],
        "cover_text": _clip(brief.get("topic") or project["title"], 40),
        "scenes": scenes,
        "caption": caption,
        "hashtags": ["#reels", "#инстаграм"],
    }


def normalize_script(script: dict, project: dict) -> dict:
    """Проверить и подровнять сценарий: существующие материалы, разумные длительности."""
    materials = {m["id"]: m for m in project["materials"] if m["kind"] in ("image", "video")}
    scenes = []
    for s in script.get("scenes", [])[:MAX_SCENES]:
        mid = (s.get("material_id") or "").strip()
        if mid not in materials:
            mid = ""
        try:
            duration = float(s.get("duration") or 3)
        except (TypeError, ValueError):
            duration = 3.0
        duration = max(1.0, min(15.0, duration))
        m = materials.get(mid)
        try:
            start = max(0.0, float(s.get("start") or 0))
        except (TypeError, ValueError):
            start = 0.0
        if m and m["kind"] == "video" and m.get("duration"):
            start = min(start, max(0.0, m["duration"] - 1.0))
            duration = min(duration, max(1.0, m["duration"] - start))
        text = str(s.get("text") or "").strip()
        voice = str(s.get("voice") or "").strip()
        if not mid and not text and not voice:
            continue
        scene = {"material_id": mid, "text": text, "voice": voice, "duration": round(duration, 2)}
        if m and m["kind"] == "video" and start > 0:
            scene["start"] = round(start, 2)
        scenes.append(scene)
    if not scenes:
        raise ValueError("В сценарии нет ни одной сцены")
    cover_id = str(script.get("cover_material_id") or "").strip()
    hashtags = []
    for tag in script.get("hashtags") or []:
        tag = str(tag).strip().replace(" ", "")
        if tag:
            hashtags.append(tag if tag.startswith("#") else f"#{tag}")
    return {
        "title": str(script.get("title") or project["title"]),
        "cover_text": str(script.get("cover_text") or ""),
        "cover_material_id": cover_id if cover_id in materials else "",
        "scenes": scenes,
        "caption": str(script.get("caption") or ""),
        "hashtags": hashtags,
    }


def generate_script(project: dict, options: dict) -> tuple[dict, str]:
    """Вернуть (сценарий, источник): 'claude' (API), 'claude-code' (подписка) или 'draft'."""
    which = engine()
    if which == "api":
        return normalize_script(generate_with_claude(project, options), project), "claude"
    if which == "claude-code":
        return normalize_script(generate_with_claude_code(project, options), project), "claude-code"
    return normalize_script(generate_fallback(project, options), project), "draft"
