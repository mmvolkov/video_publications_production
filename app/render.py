"""Сборка вертикального видео 1080×1920 из сценария с помощью Pillow + ffmpeg."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config, subtitles
from .media import open_image, run

W, H = config.WIDTH, config.HEIGHT
SUPER = 2  # фото готовим в 2× разрешении, чтобы зум был плавным

# Безопасная зона Reels: сверху ~220px, снизу ~420px перекрывает интерфейс.
SAFE_TOP = 240
SAFE_BOTTOM = H - 440
KARAOKE_TOP = SAFE_BOTTOM - 260  # караоке-субтитры занимают ~2 строки над нижней безопасной зоной

PALETTES = [
    ((235, 64, 82), (255, 128, 72)),
    ((67, 97, 238), (114, 9, 183)),
    ((17, 153, 142), (56, 239, 125)),
    ((247, 37, 133), (114, 9, 183)),
    ((20, 30, 48), (36, 59, 85)),
    ((232, 120, 30), (205, 52, 52)),
]

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

Progress = Callable[[float, str], None]


def find_font() -> str:
    for candidate in [config.FONT_PATH, *FONT_CANDIDATES]:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Не найден шрифт с кириллицей. Установите fonts-dejavu или задайте FONT_PATH.")


def font_family() -> str:
    return ImageFont.truetype(find_font(), 20).getname()[0]


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(find_font(), size)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if font.getlength(candidate) <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _fit(text: str, max_width: int, sizes: range, max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    font, lines = None, []
    for size in sizes:
        font = _font(size)
        lines = wrap_text(text, font, max_width)
        if len(lines) <= max_lines and all(font.getlength(l) <= max_width for l in lines):
            break
    return font, lines


def text_overlay(text: str, style: str, path: Path, karaoke: bool = False) -> None:
    """Прозрачный PNG 1080×1920 с текстом сцены.

    style='caption' — плашка в нижней трети поверх фото/видео;
    style='card' — крупный текст по центру (для карточек без материала).
    karaoke=True — низ кадра занят субтитрами: плашка уходит наверх, карточка — выше центра.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text = text.strip()
    if text:
        draw = ImageDraw.Draw(img)
        if style == "card":
            font, lines = _fit(text, W - 160, range(104, 47, -6), 8)
            line_h = int(font.size * 1.25)
            total = line_h * len(lines)
            bottom = KARAOKE_TOP - 40 if karaoke else SAFE_BOTTOM
            y = max(SAFE_TOP, (SAFE_TOP + bottom) // 2 - total // 2)
            for line in lines:
                x = (W - font.getlength(line)) / 2
                draw.text((x + 4, y + 5), line, font=font, fill=(0, 0, 0, 150))
                draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
                y += line_h
        else:
            pad_x, pad_y = 44, 30
            font, lines = _fit(text, W - 120 - 2 * pad_x, range(66, 37, -4), 5)
            line_h = int(font.size * 1.28)
            box_w = int(max(font.getlength(l) for l in lines)) + 2 * pad_x
            box_h = line_h * len(lines) + 2 * pad_y
            x0 = (W - box_w) // 2
            y0 = SAFE_TOP + 20 if karaoke else SAFE_BOTTOM - box_h
            draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=32, fill=(10, 10, 14, 175))
            y = y0 + pad_y
            for line in lines:
                x = (W - font.getlength(line)) / 2
                draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
                y += line_h
    img.save(path)


def gradient(index: int, size: tuple[int, int] = (W, H)) -> Image.Image:
    top, bottom = PALETTES[index % len(PALETTES)]
    w, h = size
    column = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        column.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return column.resize((w, h))


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    resized = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))), Image.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def compose_still(src: Path | None, out: Path, palette_index: int = 0) -> None:
    """Подготовить кадр 2160×3840: фото на размытом фоне (или градиент для карточки)."""
    cw, ch = W * SUPER, H * SUPER
    if src is None:
        gradient(palette_index, (cw, ch)).save(out, "JPEG", quality=92)
        return
    img = open_image(src)
    ratio = img.width / img.height
    if 0.5 <= ratio <= 0.62:  # почти 9:16 — просто кадрируем
        canvas = _cover(img, cw, ch)
    else:
        small = _cover(img, cw // 8, ch // 8).filter(ImageFilter.GaussianBlur(6))
        canvas = small.resize((cw, ch), Image.BILINEAR)
        canvas = Image.blend(canvas, Image.new("RGB", (cw, ch), (0, 0, 0)), 0.25)
        scale = min(cw / img.width, ch / img.height)
        fg = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        canvas.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2))
    canvas.save(out, "JPEG", quality=92)


ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(config.FPS)]
TEXT_IN = "[1:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1[t]"


def render_image_segment(still: Path, overlay: Path, duration: float, out: Path, zoom_in: bool,
                         subs: str = "") -> None:
    frames = max(1, round(duration * config.FPS))
    z = f"1+0.08*on/{frames}" if zoom_in else f"1.08-0.08*on/{frames}"
    graph = (
        f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={W}x{H}:fps={config.FPS},setsar=1[bg];"
        f"{TEXT_IN};[bg][t]overlay=0:0:shortest=1{subs},format=yuv420p[v]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", str(still), "-loop", "1", "-i", str(overlay),
         "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), "-an", *ENCODE, str(out)])


def render_video_segment(clip: Path, overlay: Path, duration: float, start: float, out: Path,
                         subs: str = "") -> None:
    frames = max(1, round(duration * config.FPS))
    graph = (
        f"[0:v]fps={config.FPS},split[a][b];"
        f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
        f"gblur=sigma=12,eq=brightness=-0.12,scale={W}:{H}[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,tpad=stop_mode=clone:stop_duration={duration:.2f},setsar=1[base];"
        f"{TEXT_IN};[base][t]overlay=0:0:shortest=1{subs},format=yuv420p[v]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", str(clip), "-loop", "1", "-i", str(overlay),
         "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), "-an", *ENCODE, str(out)])


def make_cover(first_still: Path, text: str, out: Path) -> None:
    img = Image.open(first_still).convert("RGB").resize((W, H), Image.LANCZOS)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shade)
    for y in range(H):
        alpha = int(150 * abs(y - H / 2) / (H / 2) * 0.4 + 70)
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), shade)
    if text.strip():
        overlay_path = out.with_suffix(".text.png")
        text_overlay(text, "card", overlay_path)
        img = Image.alpha_composite(img, Image.open(overlay_path))
        overlay_path.unlink(missing_ok=True)
    img.convert("RGB").save(out, "JPEG", quality=90)


VOICE_LEAD = 0.2   # пауза перед фразой диктора в сцене
VOICE_TAIL = 0.35  # пауза после фразы
MUSIC_UNDER_VOICE = 0.18


def _frames_exact(duration: float) -> float:
    """Длительность, кратная кадру, — чтобы звук и видео не расходились по сценам."""
    return max(1, round(duration * config.FPS)) / config.FPS


def _audio_segment(voice: Path | None, duration: float, out: Path) -> None:
    fmt = ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", "-t", f"{duration:.4f}"]
    if voice:
        delay = int(VOICE_LEAD * 1000)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(voice),
             "-af", f"aresample=44100,adelay={delay}:all=1,apad", *fmt, str(out)])
    else:
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             *fmt, str(out)])


def render_reel(
    script: dict,
    materials: dict[str, dict],
    materials_dir: Path,
    out_dir: Path,
    music: Path | None = None,
    progress: Progress | None = None,
    voiceover: dict | None = None,
    karaoke: bool = False,
) -> dict:
    """Собрать reel.mp4 и cover.jpg в out_dir.

    voiceover = {"provider", "voice", "speed"} — озвучить поле scene["voice"] каждой сцены;
    длительность такой сцены подстраивается под фразу диктора.
    karaoke=True — поверх сцен с озвучкой пословные субтитры с подсветкой текущего слова. Возвращает {duration, durations, video, cover}.
    """
    progress = progress or (lambda *_: None)
    work = out_dir / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    scenes = script["scenes"]
    voices: list[Path | None] = [None] * len(scenes)
    speeches: list = [None] * len(scenes)
    voice_notes: list[str] = []
    durations = [float(s["duration"]) for s in scenes]
    if voiceover:
        from . import tts

        for i, scene in enumerate(scenes):
            text = (scene.get("voice") or "").strip()
            if not text:
                continue
            progress(0.02 + 0.13 * i / len(scenes), f"Озвучка {i + 1} из {len(scenes)}")
            speech = tts.synthesize(text, voiceover.get("provider", ""), voiceover.get("voice", ""),
                                    float(voiceover.get("speed") or 1.0), voiceover.get("instruct"))
            voices[i], speeches[i] = speech.path, speech
            if speech.fallback and speech.fallback not in voice_notes:
                voice_notes.append(speech.fallback)
            # сцена с озвучкой длится ровно столько, сколько говорит диктор (+ паузы)
            durations[i] = max(1.5, speech.duration + VOICE_LEAD + VOICE_TAIL)
    durations = [_frames_exact(d) for d in durations]

    segments: list[Path] = []
    stills: list[Path] = []
    for i, scene in enumerate(scenes):
        progress(0.15 + 0.7 * i / len(scenes), f"Сцена {i + 1} из {len(scenes)}")
        material = materials.get(scene.get("material_id") or "")
        duration = durations[i]
        overlay = work / f"text_{i:02d}.png"
        segment = work / f"seg_{i:02d}.mp4"
        still = work / f"still_{i:02d}.jpg"
        subs = ""
        with_subs = karaoke and speeches[i] is not None and bool(speeches[i].words)
        if with_subs:
            ass = work / f"subs_{i:02d}.ass"
            if subtitles.build_ass(speeches[i].words, VOICE_LEAD, duration, font_family(), ass):
                subs = "," + subtitles.ass_filter(ass, str(Path(find_font()).parent))

        if material and material["kind"] == "video":
            src = materials_dir / material["file"]
            text_overlay(scene.get("text", ""), "caption", overlay, karaoke=with_subs)
            render_video_segment(src, overlay, duration, float(scene.get("start") or 0), segment, subs)
            run(["ffmpeg", "-y", "-v", "error", "-i", str(segment), "-frames:v", "1", str(still)])
        else:
            src = materials_dir / material["file"] if material else None
            compose_still(src, still, palette_index=i)
            text_overlay(scene.get("text", ""), "caption" if material else "card", overlay, karaoke=with_subs)
            render_image_segment(still, overlay, duration, segment, zoom_in=i % 2 == 0, subs=subs)
        segments.append(segment)
        stills.append(still)
    total = sum(durations)

    progress(0.88, "Склейка")
    concat_list = work / "list.txt"
    concat_list.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    silent = work / "video.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(silent)])

    progress(0.94, "Звук")
    inputs = ["-i", str(silent)]
    graph: list[str] = []
    voice_label = music_label = None
    if any(voices):
        parts = []
        for i, (voice, duration) in enumerate(zip(voices, durations)):
            part = work / f"voice_{i:02d}.wav"
            _audio_segment(voice, duration, part)
            parts.append(part)
        voice_list = work / "voice.txt"
        voice_list.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        voice_track = work / "voice.wav"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(voice_list),
             "-c", "copy", str(voice_track)])
        inputs += ["-i", str(voice_track)]
        voice_label = f"[{inputs.count('-i') - 1}:a]"
    if music and music.exists():
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        idx = inputs.count("-i") - 1
        level = MUSIC_UNDER_VOICE if voice_label else 0.85
        fade_start = max(0.0, total - 1.5)
        graph.append(f"[{idx}:a]aresample=44100,afade=t=in:st=0:d=0.5,"
                     f"afade=t=out:st={fade_start:.2f}:d=1.5,volume={level}[music]")
        music_label = "[music]"
    if voice_label and music_label:
        graph.append(f"{voice_label}{music_label}amix=inputs=2:duration=first:normalize=0[aout]")
    elif voice_label:
        graph.append(f"{voice_label}anull[aout]")
    elif music_label:
        graph.append(f"{music_label}anull[aout]")
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        graph.append("[1:a]anull[aout]")

    video = out_dir / "reel.mp4"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph),
         "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-t", f"{total:.3f}",
         "-movflags", "+faststart", str(video)])

    cover = out_dir / "cover.jpg"
    make_cover(stills[0], script.get("cover_text") or "", cover)
    shutil.rmtree(work, ignore_errors=True)
    progress(1.0, "Готово")
    return {"duration": round(total, 2), "durations": [round(d, 2) for d in durations], "voice_notes": voice_notes,
            "video": video.name, "cover": cover.name}
