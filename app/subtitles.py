"""Караоке-субтитры: слова по таймингам озвучки, текущее слово подсвечено (формат ASS для libass)."""
from __future__ import annotations

import re
from pathlib import Path

from . import config

WHITE = "&H00FFFFFF"
HIGHLIGHT = "&H0000E1FF"  # жёлтый (ASS хранит цвет как BBGGRR)
SENTENCE_END = re.compile(r"[.!?…]$")
PAUSE = re.compile(r"[,;:—–]$")


def chunk_words(words: list[dict], max_words: int = 3, max_chars: int = 18) -> list[list[dict]]:
    """Разбить фразу на короткие группы, которые помещаются в 1–2 строки."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        length = sum(len(w["text"]) + 1 for w in current) + len(word["text"])
        if current and (len(current) >= max_words or length > max_chars):
            chunks.append(current)
            current = []
        current.append(word)
        text = word["text"]
        if SENTENCE_END.search(text) or (PAUSE.search(text) and len(current) >= 2):
            chunks.append(current)
            current = []
    if current:
        # одинокое последнее слово присоединяем к предыдущей группе, если влезает
        if len(current) == 1 and chunks and len(chunks[-1]) < max_words \
                and sum(len(w["text"]) + 1 for w in chunks[-1]) + len(current[0]["text"]) <= max_chars + 6:
            chunks[-1].extend(current)
        else:
            chunks.append(current)
    return chunks


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words: list[dict], offset: float, scene_end: float, font_name: str, out: Path) -> int:
    """Записать ASS-файл для одной сцены. offset — где в сцене начинается фраза. Возвращает число событий."""
    events = []
    chunks = chunk_words([w for w in words if w["text"].strip()])
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1][0]["start"] + offset if ci + 1 < len(chunks) else scene_end
        hold_until = min(next_start, chunk[-1]["end"] + offset + 0.6, scene_end)
        for wi, word in enumerate(chunk):
            start = word["start"] + offset
            end = chunk[wi + 1]["start"] + offset if wi + 1 < len(chunk) else hold_until
            if end - start < 0.02:
                continue
            text = " ".join(
                f"{{\\c{HIGHLIGHT}&}}{_escape(w['text'])}{{\\c{WHITE}&}}" if j == wi else _escape(w["text"])
                for j, w in enumerate(chunk)
            )
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Karaoke,,0,0,0,,{text}")

    out.write_text("\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {config.WIDTH}",
        f"PlayResY: {config.HEIGHT}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        # снизу ровно над интерфейсом Reels (440 px), по бокам поля 90 px
        f"Style: Karaoke,{font_name},78,{WHITE}&,{WHITE}&,&H00000000&,&H96000000&,-1,0,0,0,100,100,0,0,1,7,3,2,90,90,440,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
        "",
    ]), encoding="utf-8")
    return len(events)


def ass_filter(path: Path, fonts_dir: str) -> str:
    def q(value: str) -> str:
        return value.replace("\\", "/").replace("'", r"\'")
    return f"ass=filename='{q(str(path))}':fontsdir='{q(fonts_dir)}'"
