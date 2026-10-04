"""Сборка вертикального видео 1080×1920 из сценария с помощью Pillow + ffmpeg."""
from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config, subtitles
from .media import open_image, probe, run

W, H = config.WIDTH, config.HEIGHT
SUPER = 2  # фото готовим в 2× разрешении, чтобы движение камеры было плавным

# Безопасная зона Reels: сверху ~220px, снизу ~420px перекрывает интерфейс.
SAFE_TOP = 240
SAFE_BOTTOM = H - 440
KARAOKE_TOP = SAFE_BOTTOM - 260  # караоке-субтитры занимают ~2 строки над нижней безопасной зоной

WHITE = (255, 255, 255, 255)
ACCENT = (255, 225, 0, 255)  # ключевые слова в титрах — тем же жёлтым, что и текущее слово караоке

PALETTES = [
    ((235, 64, 82), (255, 128, 72)),
    ((67, 97, 238), (114, 9, 183)),
    ((17, 153, 142), (56, 239, 125)),
    ((247, 37, 133), (114, 9, 183)),
    ((20, 30, 48), (36, 59, 85)),
    ((232, 120, 30), (205, 52, 52)),
]

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",  # пакет fonts-montserrat
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
    """Имя шрифта для libass. У начертаний вроде ExtraBold в файле своё семейство («Montserrat ExtraBold»)."""
    family, style = ImageFont.truetype(find_font(), 20).getname()
    return family if style in ("Regular", "Bold", "Book") else f"{family} {style}"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(find_font(), size)


# ---------- текст: ключевые слова *звёздочками* ----------

MARK = re.compile(r"\*([^*\n]+)\*")

Word = tuple[str, bool]  # (слово, выделено цветом)


def parse_marked(text: str) -> list[Word]:
    """«Это *очень важно*» → [("Это", False), ("очень", True), ("важно", True)]."""
    words: list[Word] = []
    pos = 0
    for m in MARK.finditer(text):
        words += [(w, False) for w in text[pos:m.start()].split()]
        words += [(w, True) for w in m.group(1).split()]
        pos = m.end()
    words += [(w, False) for w in text[pos:].split()]
    return words


def strip_marks(text: str) -> str:
    return MARK.sub(r"\1", text)


def _join(line: list[Word]) -> str:
    return " ".join(w for w, _ in line)


def wrap_words(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[list[Word]]:
    lines: list[list[Word]] = []
    for paragraph in text.split("\n"):
        words = parse_marked(paragraph)
        if not words:
            continue
        line = [words[0]]
        for word in words[1:]:
            if font.getlength(_join(line + [word])) <= max_width:
                line.append(word)
            else:
                lines.append(line)
                line = [word]
        lines.append(line)
    return lines


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    return [_join(line) for line in wrap_words(text, font, max_width)]


def _fit(text: str, max_width: int, sizes: range, max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[list[Word]]]:
    font, lines = None, []
    for size in sizes:
        font = _font(size)
        lines = wrap_words(text, font, max_width)
        if len(lines) <= max_lines and all(font.getlength(_join(l)) <= max_width for l in lines):
            break
    return font, lines


def _draw_line(draw: ImageDraw.ImageDraw, line: list[Word], font: ImageFont.FreeTypeFont, y: float,
               shadow: bool = False) -> None:
    space = font.getlength(" ")
    x = (W - font.getlength(_join(line))) / 2
    for word, accent in line:
        if shadow:
            draw.text((x + 4, y + 5), word, font=font, fill=(0, 0, 0, 150))
        draw.text((x, y), word, font=font, fill=ACCENT if accent else WHITE)
        x += font.getlength(word) + space


def text_layer(text: str, style: str, karaoke: bool = False) -> Image.Image:
    """Прозрачный слой 1080×1920 с текстом сцены.

    style='caption' — плашка в нижней трети поверх фото/видео;
    style='card' — крупный текст по центру (для карточек без материала).
    karaoke=True — низ кадра занят субтитрами: плашка уходит наверх, карточка — выше центра.
    Слова в *звёздочках* выделяются жёлтым.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text = text.strip()
    if not text:
        return img
    draw = ImageDraw.Draw(img)
    if style == "card":
        font, lines = _fit(text, W - 160, range(104, 47, -6), 8)
        line_h = int(font.size * 1.25)
        total = line_h * len(lines)
        bottom = KARAOKE_TOP - 40 if karaoke else SAFE_BOTTOM
        y = max(SAFE_TOP, (SAFE_TOP + bottom) // 2 - total // 2)
        for line in lines:
            _draw_line(draw, line, font, y, shadow=True)
            y += line_h
    else:
        pad_x, pad_y = 44, 30
        font, lines = _fit(text, W - 120 - 2 * pad_x, range(66, 37, -4), 5)
        line_h = int(font.size * 1.28)
        box_w = int(max(font.getlength(_join(l)) for l in lines)) + 2 * pad_x
        box_h = line_h * len(lines) + 2 * pad_y
        x0 = (W - box_w) // 2
        y0 = SAFE_TOP + 20 if karaoke else SAFE_BOTTOM - box_h
        draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=32, fill=(10, 10, 14, 175))
        y = y0 + pad_y
        for line in lines:
            _draw_line(draw, line, font, y)
            y += line_h
    return img


def text_overlay(text: str, style: str, path: Path, karaoke: bool = False) -> None:
    text_layer(text, style, karaoke).save(path)


@dataclasses.dataclass
class TextAnim:
    """Кадры появления титра («поп» с лёгким увеличением) и где их накладывать."""
    pattern: Path  # text_XX_%02d.png
    x: int
    y: int


# (масштаб, прозрачность) по кадрам: ~0.23 с при 30 fps — титр «выпрыгивает» и чуть отскакивает
POP = [(0.80, 0.0), (0.90, 0.35), (0.99, 0.7), (1.05, 0.95), (1.06, 1.0), (1.03, 1.0), (1.0, 1.0)]


def text_animation(text: str, style: str, work: Path, name: str, karaoke: bool = False) -> TextAnim:
    layer = text_layer(text, style, karaoke)
    bbox = layer.getbbox()
    if not bbox:
        Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(work / f"{name}_00.png")
        return TextAnim(work / f"{name}_%02d.png", 0, 0)
    crop = layer.crop(bbox)
    w, h = crop.size
    cw, ch = int(w * 1.08) + 4, int(h * 1.08) + 4
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for i, (scale, alpha) in enumerate(POP):
        frame = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
        scaled = crop.resize((sw, sh), Image.LANCZOS) if (sw, sh) != (w, h) else crop.copy()
        if alpha < 1:
            scaled.putalpha(scaled.getchannel("A").point(lambda v, a=alpha: int(v * a)))
        frame.alpha_composite(scaled, ((cw - sw) // 2, (ch - sh) // 2))
        frame.save(work / f"{name}_{i:02d}.png")
    return TextAnim(work / f"{name}_%02d.png", round(cx - cw / 2), round(cy - ch / 2))


def _text_input(anim: TextAnim) -> list[str]:
    return ["-framerate", str(config.FPS), "-i", str(anim.pattern)]


def _text_graph(anim: TextAnim, length: float, delay: float, base: str, subs: str) -> str:
    """Титр появляется с задержкой delay (после перехода) и держится до конца сегмента."""
    return (f"[1:v]format=rgba,tpad=start_duration={delay:.3f}:color=black@0:"
            f"stop_mode=clone:stop_duration={length + 1:.2f}[t];"
            f"{base}[t]overlay={anim.x}:{anim.y}:format=auto{subs},format=yuv420p[v]")


# ---------- кадры ----------

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


def video_still(clip: Path, at: float, out: Path) -> None:
    """Кадр из видео (без титров), разложенный как фото, — для обложки."""
    raw = out.with_suffix(".raw.jpg")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1", str(raw)])
    compose_still(raw, out)
    raw.unlink(missing_ok=True)


# промежуточные сегменты — почти без потерь, итог кодируется один раз
ENCODE_TMP = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(config.FPS)]
ENCODE_FINAL = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-r", str(config.FPS)]

# движение камеры по фото: наезд, отъезд и панорамы в разные стороны
MOVES = ["in", "right", "out", "up", "left", "down"]


def _motion(move: str, frames: int) -> tuple[str, str, str]:
    p = f"on/{frames}"
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if move == "in":
        return f"1+0.10*{p}", cx, cy
    if move == "out":
        return f"1.10-0.10*{p}", cx, cy
    pan = {"right": (f"(iw-iw/zoom)*{p}", cy), "left": (f"(iw-iw/zoom)*(1-{p})", cy),
           "down": (cx, f"(ih-ih/zoom)*{p}"), "up": (cx, f"(ih-ih/zoom)*(1-{p})")}[move]
    return "1.12", *pan


def render_image_segment(still: Path, text: TextAnim, length: float, out: Path, move: str = "in",
                         subs: str = "", text_delay: float = 0.0) -> None:
    frames = max(1, round(length * config.FPS))
    z, x, y = _motion(move, frames)
    graph = (f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={config.FPS},setsar=1[bg];"
             + _text_graph(text, length, text_delay, "[bg]", subs))
    run(["ffmpeg", "-y", "-v", "error", "-i", str(still), *_text_input(text),
         "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), "-an", *ENCODE_TMP, str(out)])


MAX_SLOWDOWN = 1.6  # дольше — ролик не замедляем, а зацикливаем


def clip_plan(length: float, start: float, clip_duration: float) -> tuple[float, bool]:
    """Как растянуть видео на сцену без «замороженного» кадра: (замедление, зациклить)."""
    available = clip_duration - start
    if clip_duration <= 0 or available <= 0.1 or length <= available + 0.04:
        return 1.0, False
    slow = length / available
    return (slow, False) if slow <= MAX_SLOWDOWN else (1.0, True)


def render_video_segment(clip: Path, text: TextAnim, length: float, start: float, out: Path,
                         subs: str = "", text_delay: float = 0.0, clip_duration: float = 0.0) -> None:
    frames = max(1, round(length * config.FPS))
    slow, loop = clip_plan(length, start, clip_duration)
    pts = f"setpts={slow:.4f}*PTS," if slow > 1 else ""
    graph = (
        f"[0:v]{pts}fps={config.FPS},split[a][b];"
        f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
        f"gblur=sigma=12,eq=brightness=-0.12,scale={W}:{H}[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,tpad=stop_mode=clone:stop_duration={length:.2f},setsar=1[base];"
        + _text_graph(text, length, text_delay, "[base]", subs)
    )
    run(["ffmpeg", "-y", "-v", "error", *(["-stream_loop", "-1"] if loop else []), "-ss", f"{start:.2f}",
         "-i", str(clip), *_text_input(text),
         "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), "-an", *ENCODE_TMP, str(out)])


def make_cover(still: Path, text: str, out: Path) -> None:
    img = Image.open(still).convert("RGB").resize((W, H), Image.LANCZOS)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shade)
    for y in range(H):
        alpha = int(150 * abs(y - H / 2) / (H / 2) * 0.4 + 70)
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), shade)
    if text.strip():
        img = Image.alpha_composite(img, text_layer(text, "card"))
    img.convert("RGB").save(out, "JPEG", quality=90)


# ---------- переходы ----------

TRANSITION = 0.3  # с; соседние сцены перекрываются на это время
# первый переход после хука — «наезд», в призыв — мягкое затемнение, между ними — по кругу
TRANSITIONS = ["smoothleft", "fade", "smoothup", "fadewhite", "slideleft", "circleopen"]


def transition_names(count: int) -> list[str]:
    names = [TRANSITIONS[i % len(TRANSITIONS)] for i in range(count)]
    if count:
        names[0] = "zoomin"
    if count > 1:
        names[-1] = "fade"
    return names


def join_segments(segments: list[Path], durations: list[float], out: Path) -> None:
    """Склеить сегменты с переходами. Сегмент i (кроме последнего) длиннее сцены на TRANSITION,
    поэтому сцена i начинается ровно в sum(durations[:i]) — звук совпадает с картинкой."""
    inputs = sum((["-i", str(s)] for s in segments), [])
    graph, prev, offset = [], "[0:v]", 0.0
    for i, name in enumerate(transition_names(len(segments) - 1)):
        offset += durations[i]
        label = f"[x{i}]"
        graph.append(f"{prev}[{i + 1}:v]xfade=transition={name}:duration={TRANSITION}:offset={offset:.3f}{label}")
        prev = label
    if not graph:
        graph.append("[0:v]null[x]")
        prev = "[x]"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", prev,
         "-t", f"{sum(durations):.3f}", *ENCODE_FINAL, str(out)])


# ---------- звук ----------

VOICE_LEAD = 0.2   # пауза перед фразой диктора в сцене
VOICE_TAIL = 0.35  # пауза после фразы
SILENCE_DB = -45   # тише этого в начале и конце фразы — тишина, её срезаем
MUSIC_LEVEL = 0.8            # музыка без голоса
MUSIC_UNDER_VOICE = 0.4      # музыка, когда есть голос: дальше её прижимает sidechain, пока диктор говорит
CLIP_UNDER_VOICE = 0.3       # родной звук ролика под голосом диктора
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"  # громкость как у рилсов в ленте


def _frames_exact(duration: float) -> float:
    """Длительность, кратная кадру, — чтобы звук и видео не расходились по сценам."""
    return max(1, round(duration * config.FPS)) / config.FPS


def trim_silence(src: Path, dst: Path) -> tuple[float, float] | None:
    """Срезать тишину в начале и конце фразы. Возвращает (срезано в начале, новая длительность) или None."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
                           "-af", f"silencedetect=noise={SILENCE_DB}dB:d=0.05", "-f", "null", "-"],
                          capture_output=True, text=True)
    total = probe(src).get("duration") or 0
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    head = ends[0] if starts and starts[0] <= 0.01 and ends else 0.0
    tail = total
    if starts and starts[-1] > head + 0.05 and (len(ends) < len(starts) or ends[-1] >= total - 0.02):
        tail = starts[-1]
    head, tail = max(0.0, head - 0.03), min(total, tail + 0.08)
    if total <= 0 or tail - head < 0.2 or (head < 0.02 and tail > total - 0.02):
        return None
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", f"atrim={head:.3f}:{tail:.3f},asetpts=PTS-STARTPTS",
         str(dst)])
    return head, tail - head


def _pcm(duration: float) -> list[str]:
    return ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", "-t", f"{duration:.4f}"]


def _audio_segment(voice: Path | None, duration: float, out: Path) -> None:
    if voice:
        delay = int(VOICE_LEAD * 1000)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(voice),
             "-af", f"aresample=44100,adelay={delay}:all=1,apad", *_pcm(duration), str(out)])
    else:
        _silence(duration, out)


def _silence(duration: float, out: Path) -> None:
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         *_pcm(duration), str(out)])


def _clip_audio_segment(clip: Path, start: float, duration: float, volume: float, out: Path,
                        clip_duration: float) -> None:
    slow, loop = clip_plan(duration, start, clip_duration)
    tempo = f"atempo={1 / slow:.4f}," if slow > 1 else ""
    fade_out = max(0.0, duration - 0.25)
    run(["ffmpeg", "-y", "-v", "error", *(["-stream_loop", "-1"] if loop else []), "-ss", f"{start:.2f}",
         "-i", str(clip), "-vn", "-af",
         f"{tempo}aresample=44100,aformat=channel_layouts=stereo,apad,afade=t=in:d=0.08,"
         f"afade=t=out:st={fade_out:.3f}:d=0.25,volume={volume}", *_pcm(duration), str(out)])


def _concat_audio(parts: list[Path], out: Path) -> None:
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)])


def mix_audio(voice: Path | None, clip: Path | None, music: Path | None, total: float, out: Path) -> None:
    """Голос + звук роликов + музыка: музыка проседает, пока звучит голос (sidechain), итог — −14 LUFS."""
    inputs: list[str] = []
    graph: list[str] = []
    mix: list[str] = []
    keys: list[str] = []
    for name, path in (("vo", voice), ("cl", clip)):
        if path:
            inputs += ["-i", str(path)]
            graph.append(f"[{len(inputs) // 2 - 1}:a]asplit=2[{name}][{name}k]")
            mix.append(f"[{name}]")
            keys.append(f"[{name}k]")
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        level = MUSIC_UNDER_VOICE if keys else MUSIC_LEVEL
        fade_start = max(0.0, total - 1.5)
        graph.append(f"[{inputs.count('-i') - 1}:a]aresample=44100,aformat=channel_layouts=stereo,"
                     f"atrim=0:{total:.3f},afade=t=in:st=0:d=0.5,afade=t=out:st={fade_start:.2f}:d=1.5,"
                     f"volume={level}[mus]")
        if keys:
            key = keys[0]
            if len(keys) > 1:
                graph.append(f"{''.join(keys)}amix=inputs={len(keys)}:normalize=0[key]")
                key = "[key]"
            graph.append(f"[mus]{key}sidechaincompress=threshold=0.02:ratio=8:attack=15:release=350[duck]")
            mix.append("[duck]")
        else:
            mix.append("[mus]")
    elif keys:
        graph += [f"{k}anullsink" for k in keys]
    if not mix:
        _silence(total, out)
        return
    joined = f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=longest," if len(mix) > 1 else mix[0]
    graph.append(f"{joined}{LOUDNESS},aresample=44100[aout]")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[aout]",
         *_pcm(total), str(out)])


# ---------- сборка ----------

def render_reel(
    script: dict,
    materials: dict[str, dict],
    materials_dir: Path,
    out_dir: Path,
    music: Path | None = None,
    progress: Progress | None = None,
    voiceover: dict | None = None,
    karaoke: bool = False,
    clip_audio: bool = False,
) -> dict:
    """Собрать reel.mp4 и cover.jpg в out_dir.

    voiceover = {"provider", "voice", "speed"} — озвучить поле scene["voice"] каждой сцены;
    длительность такой сцены подстраивается под фразу диктора.
    karaoke=True — поверх сцен с озвучкой пословные субтитры с подсветкой текущего слова.
    clip_audio=True — оставить родной звук видео-материалов (под голосом диктора — тише).
    Возвращает {duration, durations, video, cover}.
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
            if speech.fallback and speech.fallback not in voice_notes:
                voice_notes.append(speech.fallback)
            # тишина по краям фразы растягивает сцену и сбивает темп — срезаем, тайминги слов сдвигаем
            trimmed = work / f"voice_trim_{i:02d}.wav"
            cut = trim_silence(speech.path, trimmed)
            if cut:
                head, length = cut
                words = [{**w, "start": round(max(0.0, w["start"] - head), 3),
                          "end": round(min(length, max(0.0, w["end"] - head)), 3)} for w in speech.words]
                speech = dataclasses.replace(speech, path=trimmed, duration=length, words=words)
            voices[i], speeches[i] = speech.path, speech
            # сцена с озвучкой длится ровно столько, сколько говорит диктор (+ паузы)
            durations[i] = max(1.5, speech.duration + VOICE_LEAD + VOICE_TAIL)
    durations = [_frames_exact(d) for d in durations]

    segments: list[Path] = []
    stills: list[Path] = []
    last = len(scenes) - 1
    for i, scene in enumerate(scenes):
        progress(0.15 + 0.65 * i / len(scenes), f"Сцена {i + 1} из {len(scenes)}")
        material = materials.get(scene.get("material_id") or "")
        duration = durations[i]
        length = duration + (TRANSITION if i < last else 0)  # хвост уходит в переход к следующей сцене
        delay = TRANSITION / 2 if i else 0.0  # титр появляется, когда переход почти закончился
        segment = work / f"seg_{i:02d}.mp4"
        still = work / f"still_{i:02d}.jpg"
        subs = ""
        with_subs = karaoke and speeches[i] is not None and bool(speeches[i].words)
        if with_subs:
            ass = work / f"subs_{i:02d}.ass"
            if subtitles.build_ass(speeches[i].words, VOICE_LEAD, duration, font_family(), ass):
                subs = "," + subtitles.ass_filter(ass, str(Path(find_font()).parent))

        style = "caption" if material else "card"
        text = text_animation(scene.get("text", ""), style, work, f"text_{i:02d}", karaoke=with_subs)
        if material and material["kind"] == "video":
            src = materials_dir / material["file"]
            start = float(scene.get("start") or 0)
            render_video_segment(src, text, length, start, segment, subs, delay,
                                 clip_duration=float(material.get("duration") or 0))
            video_still(src, start + min(0.5, duration / 2), still)
        else:
            src = materials_dir / material["file"] if material else None
            compose_still(src, still, palette_index=i)
            render_image_segment(still, text, length, segment, move=MOVES[i % len(MOVES)], subs=subs,
                                 text_delay=delay)
        segments.append(segment)
        stills.append(still)
    total = sum(durations)

    progress(0.82, "Склейка и переходы")
    video_only = work / "video.mp4"
    join_segments(segments, durations, video_only)

    progress(0.94, "Звук")
    voice_track = clip_track = None
    if any(voices):
        parts = []
        for i, (voice, duration) in enumerate(zip(voices, durations)):
            part = work / f"voice_{i:02d}.wav"
            _audio_segment(voice, duration, part)
            parts.append(part)
        voice_track = work / "voice.wav"
        _concat_audio(parts, voice_track)
    if clip_audio:
        parts, any_clip = [], False
        for i, (scene, duration) in enumerate(zip(scenes, durations)):
            material = materials.get(scene.get("material_id") or "")
            part = work / f"clip_{i:02d}.wav"
            if material and material["kind"] == "video" and material.get("has_audio"):
                volume = CLIP_UNDER_VOICE if voices[i] else 1.0
                _clip_audio_segment(materials_dir / material["file"], float(scene.get("start") or 0), duration,
                                    volume, part, float(material.get("duration") or 0))
                any_clip = True
            else:
                _silence(duration, part)
            parts.append(part)
        if any_clip:
            clip_track = work / "clip.wav"
            _concat_audio(parts, clip_track)
    audio = work / "mix.wav"
    mix_audio(voice_track, clip_track, music if music and music.exists() else None, total, audio)

    video = out_dir / "reel.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video_only), "-i", str(audio), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-t", f"{total:.3f}",
         "-movflags", "+faststart", str(video)])

    cover = out_dir / "cover.jpg"
    make_cover(_cover_still(script, scenes, stills, materials, materials_dir, work), script.get("cover_text") or "",
               cover)
    shutil.rmtree(work, ignore_errors=True)
    progress(1.0, "Готово")
    return {"duration": round(total, 2), "durations": [round(d, 2) for d in durations], "voice_notes": voice_notes,
            "video": video.name, "cover": cover.name}


def _cover_still(script: dict, scenes: list[dict], stills: list[Path], materials: dict[str, dict],
                 materials_dir: Path, work: Path) -> Path:
    """Обложка — с кадра, который выбрал сценарист (cover_material_id), иначе с первой сцены."""
    cover_id = script.get("cover_material_id") or ""
    for scene, still in zip(scenes, stills):
        if cover_id and scene.get("material_id") == cover_id:
            return still
    material = materials.get(cover_id)
    if material and material["kind"] in ("image", "video"):
        still = work / "cover_src.jpg"
        src = materials_dir / material["file"]
        if material["kind"] == "image":
            compose_still(src, still)
        else:
            video_still(src, min(1.0, float(material.get("duration") or 0) / 2), still)
        return still
    return stills[0]
