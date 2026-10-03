"""Озвучка: несколько провайдеров с общим интерфейсом и кешем.

Провайдеры:
- corp       — свой TTS (OpenAI-совместимый, Fun-CosyVoice3): anastasiya / dmitry / svetlana;
- edge       — живой Microsoft edge-tts (ru-RU-SvetlanaNeural и др.), бесплатно, текст уходит в Microsoft;
- yandex     — Yandex SpeechKit (API v1, ключ сервисного аккаунта);
- elevenlabs — ElevenLabs (multilingual).

Каждый вызов кешируется по (провайдер, голос, скорость, текст), поэтому пересборка после
правок сценария озвучивает заново только изменённые фразы.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config
from .media import probe, run


class TTSError(RuntimeError):
    pass


@dataclass
class Provider:
    id: str
    name: str
    voices: list[tuple[str, str]]  # (id, подпись)
    note: str = ""
    ext: str = "wav"
    env_keys: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return all(_env(k) for k in self.env_keys)

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> None:
        raise NotImplementedError


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _parse_voices(raw: str, fallback: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """'id:Подпись,id2:Подпись2' → [(id, подпись)]."""
    voices = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        vid, _, label = item.partition(":")
        voices.append((vid.strip(), (label or vid).strip()))
    return voices or fallback


def _check(resp: httpx.Response, who: str) -> None:
    if resp.status_code != 200:
        raise TTSError(f"{who}: HTTP {resp.status_code} — {resp.text[:300]}")


# ---------- свой TTS (OpenAI-совместимый) ----------

class CorpProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            id="corp",
            name="Свой TTS (CosyVoice3)",
            voices=_parse_voices(os.getenv("CORP_TTS_VOICES", ""), [
                ("anastasiya", "Анастасия — деловая диктовка"),
                ("dmitry", "Дмитрий — спокойный, обучающий"),
                ("svetlana", "Светлана — нейтральный деловой (клон)"),
            ]),
            note="Собственный сервер, OpenAI-совместимый /v1/audio/speech",
            env_keys=[],
        )

    @property
    def base_url(self) -> str:
        return _env("CORP_TTS_BASE_URL", "TTS_BASE_URL", default="https://tts.cloudsmasters.ru/v1").rstrip("/")

    @property
    def api_key(self) -> str:
        return _env("CORP_TTS_API_KEY", "TTS_API_KEY")

    def available(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> None:
        # httpx кодирует JSON в UTF-8 — кириллица в теле не ломается.
        body = {"model": "tts-1", "voice": voice, "input": text, "response_format": "wav", "speed": speed}
        resp = httpx.post(f"{self.base_url}/audio/speech", json=body, timeout=120,
                          headers={"Authorization": f"Bearer {self.api_key}"})
        _check(resp, "Свой TTS")
        out.write_bytes(resp.content)


# ---------- edge-tts (живой сервис Microsoft) ----------

class EdgeProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            id="edge",
            name="Edge TTS (Microsoft)",
            voices=_parse_voices(os.getenv("EDGE_TTS_VOICES", ""), [
                ("ru-RU-SvetlanaNeural", "Светлана — женский"),
                ("ru-RU-DmitryNeural", "Дмитрий — мужской"),
            ]),
            note="Бесплатно, без ключа. Текст реплик отправляется на серверы Microsoft",
            ext="mp3",
        )

    def available(self) -> bool:
        return _env("EDGE_TTS_ENABLED", default="true").lower() not in ("0", "false", "no")

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> None:
        import edge_tts

        # Отрицательный rate сервис сейчас отвергает — замедление делаем через ffmpeg atempo.
        rate = f"+{round((max(speed, 1.0) - 1) * 100)}%"
        last_error: Exception | None = None
        for attempt in range(5):  # длинные фрагменты иногда обрываются — повторяем с паузой
            try:
                asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out)))
                if out.exists() and out.stat().st_size > 0:
                    break
                raise TTSError("пустой ответ")
            except Exception as exc:  # noqa: BLE001 — сеть/сервис, повторяем
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        else:
            raise TTSError(f"Edge TTS не ответил после 5 попыток: {last_error}")
        if speed < 1.0:
            _atempo(out, speed)


# ---------- Yandex SpeechKit ----------

class YandexProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            id="yandex",
            name="Yandex SpeechKit",
            voices=_parse_voices(os.getenv("YANDEX_TTS_VOICES", ""), [
                ("alena", "Алёна"),
                ("filipp", "Филипп"),
                ("jane", "Джейн"),
                ("ermil", "Ермил"),
                ("zahar", "Захар"),
                ("omazh", "Омаж"),
            ]),
            note="Оплата в рублях через Yandex Cloud",
            env_keys=["YANDEX_API_KEY"],
        )

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> None:
        data = {"text": text, "lang": "ru-RU", "voice": voice, "speed": f"{speed:.2f}",
                "format": "lpcm", "sampleRateHertz": "48000"}
        if _env("YANDEX_FOLDER_ID"):
            data["folderId"] = _env("YANDEX_FOLDER_ID")
        resp = httpx.post("https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize", data=data, timeout=120,
                          headers={"Authorization": f"Api-Key {_env('YANDEX_API_KEY')}"})
        _check(resp, "Yandex SpeechKit")
        raw = out.with_suffix(".pcm")
        raw.write_bytes(resp.content)
        run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", str(raw), str(out)])
        raw.unlink(missing_ok=True)


# ---------- ElevenLabs ----------

class ElevenLabsProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            id="elevenlabs",
            name="ElevenLabs",
            voices=_parse_voices(os.getenv("ELEVENLABS_VOICES", ""), [("21m00Tcm4TlvDq8ikWAM", "Rachel")]),
            note="Голоса из вашей библиотеки ElevenLabs (ELEVENLABS_VOICES=id:Имя,…)",
            ext="mp3",
            env_keys=["ELEVENLABS_API_KEY"],
        )

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> None:
        body = {
            "text": text,
            "model_id": _env("ELEVENLABS_MODEL", default="eleven_multilingual_v2"),
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": max(0.7, min(1.2, speed))},
        }
        resp = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                          json=body, timeout=120, headers={"xi-api-key": _env("ELEVENLABS_API_KEY")})
        _check(resp, "ElevenLabs")
        out.write_bytes(resp.content)


def _atempo(path: Path, speed: float) -> None:
    tmp = path.with_name(path.stem + ".tempo" + path.suffix)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-filter:a", f"atempo={max(0.5, speed):.3f}", str(tmp)])
    tmp.replace(path)


PROVIDERS: dict[str, Provider] = {p.id: p for p in (CorpProvider(), EdgeProvider(), YandexProvider(), ElevenLabsProvider())}


def default_provider() -> str:
    wanted = _env("TTS_DEFAULT_PROVIDER", default="corp")
    if wanted in PROVIDERS and PROVIDERS[wanted].available():
        return wanted
    return next((pid for pid, p in PROVIDERS.items() if p.available()), wanted)


def describe() -> dict:
    return {
        "default": default_provider(),
        "providers": [
            {"id": p.id, "name": p.name, "available": p.available(), "note": p.note,
             "voices": [{"id": vid, "name": label} for vid, label in p.voices]}
            for p in PROVIDERS.values()
        ],
    }


def cache_dir() -> Path:
    d = config.DATA_DIR / "tts_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def synthesize(text: str, provider: str, voice: str = "", speed: float = 1.0) -> tuple[Path, float]:
    """Озвучить фразу (с кешем). Возвращает (путь к файлу, длительность в секундах)."""
    text = " ".join(text.split())
    if not text:
        raise TTSError("Пустой текст для озвучки")
    p = PROVIDERS.get(provider)
    if p is None:
        raise TTSError(f"Неизвестный провайдер озвучки: {provider}")
    if not p.available():
        raise TTSError(f"Провайдер «{p.name}» не настроен — проверьте ключи в .env")
    voice = voice or p.voices[0][0]
    speed = max(0.5, min(2.0, float(speed or 1.0)))
    key = hashlib.sha256(f"{p.id}|{voice}|{speed:.2f}|{text}".encode()).hexdigest()[:24]
    out = cache_dir() / f"{p.id}_{key}.{p.ext}"
    if not out.exists() or out.stat().st_size == 0:
        tmp = out.with_name(out.stem + ".part." + p.ext)
        try:
            p.synthesize(text, voice, speed, tmp)
        except TTSError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # сетевые и прочие ошибки — понятным сообщением
            tmp.unlink(missing_ok=True)
            raise TTSError(f"{p.name}: {exc}") from exc
        tmp.replace(out)
    duration = probe(out).get("duration", 0.0)
    if duration <= 0:
        out.unlink(missing_ok=True)
        raise TTSError(f"{p.name} вернул пустое аудио")
    return out, duration
