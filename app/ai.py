"""Сценарий рилса: Claude смотрит материалы и пишет раскадровку.

Если ключ Anthropic не задан, строится простой черновой сценарий без ИИ,
чтобы сайт работал «из коробки».
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from . import config, storage

MAX_SCENES = 12
MAX_IMAGES_FOR_AI = 20

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Рабочее название рилса"},
        "cover_text": {"type": "string", "description": "Короткий текст для обложки (до 40 символов)"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "material_id": {
                        "type": "string",
                        "description": "id фото/видео из материалов или пустая строка для текстовой карточки",
                    },
                    "text": {"type": "string", "description": "Текст на экране, до 90 символов"},
                    "voice": {"type": "string", "description": "Фраза диктора для этой сцены или пустая строка, если озвучка выключена"},
                    "duration": {"type": "number", "description": "Длительность сцены в секундах (1.5–8)"},
                },
                "required": ["material_id", "text", "voice", "duration"],
                "additionalProperties": False,
            },
        },
        "caption": {"type": "string", "description": "Подпись к посту в Instagram"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "cover_text", "scenes", "caption", "hashtags"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты — сильный SMM-продюсер и сценарист вертикальных видео (Instagram Reels).
Из материалов пользователя ты собираешь раскадровку рилса, который удерживает внимание и продаёт.

Правила:
- Первая сцена — хук на 1.5–3 секунды: интрига, боль, цифра или смелое обещание. Без «Привет, сегодня расскажу».
- Одна сцена = одна мысль. Текст на экране короткий и разговорный, до 90 символов, без хэштегов.
- Используй только material_id из списка. Пустой material_id = текстовая карточка на цветном фоне (для хука, вывода или призыва).
- Выбирай самые выразительные кадры, порядок — по логике истории (проблема → решение → результат → призыв).
- Видео-материал можно использовать не дольше его длительности.
- Последняя сцена — понятный призыв к действию.
- Суммарная длительность — близко к целевой.
- Подпись к посту: живая, 300–900 символов, с абзацами и эмодзи по делу, в конце призыв. Хэштеги — отдельным списком, 5–12 штук, каждый начинается с «#».
- Пиши на языке, на котором написан бриф (по умолчанию — русский).

Озвучка (если в брифе сказано, что она включена):
- В поле voice — живая разговорная фраза диктора для сцены: 1–2 коротких предложения, без хэштегов, эмодзи и списков.
- Озвучка дополняет текст на экране, а не дублирует его слово в слово; на экране — короткий тезис, голосом — мысль целиком.
- Длительность сцены ставь под фразу: примерно 14 символов фразы в секунду плюс полсекунды.
- Числа и сокращения пиши так, как их надо произнести («двадцать процентов», а не «20%»).
Если озвучка выключена — оставляй voice пустой строкой."""


def ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


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
    for i, m in enumerate(visual):
        desc = f"material_id={m['id']} | тип: {'фото' if m['kind'] == 'image' else 'видео'}"
        if m["kind"] == "video" and m.get("duration"):
            desc += f" | длительность {m['duration']:.1f} c"
        if m.get("note"):
            desc += f" | комментарий автора: {m['note']}"
        content.append({"type": "text", "text": desc})
        thumb = pdir / "thumbs" / f"{m['id']}.jpg"
        if i < MAX_IMAGES_FOR_AI and thumb.exists():
            content.append(_image_block(thumb))

    for m in texts:
        content.append({"type": "text", "text": f"\nТЕКСТОВЫЙ МАТЕРИАЛ «{m.get('name') or 'заметка'}»:\n{m.get('text', '')}"})

    if audio:
        content.append({"type": "text", "text": "\nЕсть фоновая музыка — её наложат автоматически."})

    content.append({"type": "text", "text": "\nСоставь раскадровку рилса по правилам."})
    return [{"role": "user", "content": content}]


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
        if m and m["kind"] == "video" and m.get("duration"):
            duration = min(duration, max(1.0, m["duration"]))
        text = str(s.get("text") or "").strip()
        voice = str(s.get("voice") or "").strip()
        if not mid and not text and not voice:
            continue
        scene = {"material_id": mid, "text": text, "voice": voice, "duration": round(duration, 2)}
        start = float(s.get("start") or 0)
        if m and m["kind"] == "video" and start > 0:
            scene["start"] = min(start, max(0.0, (m.get("duration") or 0) - 0.5))
        scenes.append(scene)
    if not scenes:
        raise ValueError("В сценарии нет ни одной сцены")
    hashtags = []
    for tag in script.get("hashtags") or []:
        tag = str(tag).strip().replace(" ", "")
        if tag:
            hashtags.append(tag if tag.startswith("#") else f"#{tag}")
    return {
        "title": str(script.get("title") or project["title"]),
        "cover_text": str(script.get("cover_text") or ""),
        "scenes": scenes,
        "caption": str(script.get("caption") or ""),
        "hashtags": hashtags,
    }


def generate_script(project: dict, options: dict) -> tuple[dict, str]:
    """Вернуть (сценарий, источник): источник — 'claude' или 'draft'."""
    if ai_available():
        return normalize_script(generate_with_claude(project, options), project), "claude"
    return normalize_script(generate_fallback(project, options), project), "draft"
