"""Работа с загруженными файлами: тип, длительность, превью."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
TEXT_EXT = {".txt", ".md", ".csv"}


def detect_kind(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TEXT_EXT:
        return "text"
    return None


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"Команда {cmd[0]} завершилась с ошибкой:\n{tail}")
    return proc


def probe(path: Path) -> dict:
    """Длительность, размеры и наличие аудио (для видео и аудио)."""
    proc = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=60)
    data = json.loads(proc.stdout or "{}")
    info: dict = {"duration": float(data.get("format", {}).get("duration") or 0)}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "width" not in info:
            w, h = int(stream.get("width", 0)), int(stream.get("height", 0))
            rotation = 0
            for side in stream.get("side_data_list", []) or []:
                rotation = int(side.get("rotation", 0) or 0)
            rotation = rotation or int((stream.get("tags") or {}).get("rotate", 0) or 0)
            if abs(rotation) in (90, 270):
                w, h = h, w
            info["width"], info["height"] = w, h
        if stream.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def open_image(path: Path) -> Image.Image:
    """Открыть картинку с учётом EXIF-поворота, в RGB."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        background = Image.new("RGB", img.size, (0, 0, 0))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    return img


def make_thumbnail(kind: str, src: Path, dst: Path, size: int = 480) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "image":
            img = open_image(src)
        elif kind == "video":
            frame = dst.with_suffix(".frame.jpg")
            duration = probe(src).get("duration", 0)
            at = min(1.0, duration / 2) if duration else 0
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src),
                 "-frames:v", "1", str(frame)], timeout=60)
            img = open_image(frame)
            frame.unlink(missing_ok=True)
        else:
            return False
        img.thumbnail((size, size))
        img.save(dst, "JPEG", quality=82)
        return True
    except Exception:  # превью не критично — материал всё равно сохраняем
        return False
