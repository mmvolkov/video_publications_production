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
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import subprocess

import httpx

from . import config
from .media import probe, run


class TTSError(RuntimeError):
    pass


@dataclass
class Speech:
    path: Path
    duration: float
    words: list[dict]  # [{"text", "start", "end"}] в секундах от начала файла
    timed: bool        # True — реальные тайминги от сервиса, False — разложены пропорционально
    fallback: str = ""  # не пусто — фраза озвучена запасным голосом (пояснение для интерфейса)


@dataclass
class Provider:
    id: str
    name: str
    voices: list[tuple[str, str]]  # (id, подпись)
    note: str = ""
    ext: str = "wav"
    env_keys: list[str] = field(default_factory=list)
    supports_instruct: bool = False  # сервис понимает инструкцию по подаче (тон, темп, эмоция)

    def available(self) -> bool:
        return all(_env(k) for k in self.env_keys)

    def voice_list(self) -> list[dict]:
        """Голоса для интерфейса: [{"id", "name", "instruct"}]."""
        return [{"id": vid, "name": label, "instruct": ""} for vid, label in self.voices]

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> list[dict] | None:
        """Записать аудио в out. Может вернуть тайминги слов, если сервис их отдаёт."""
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
            supports_instruct=True,
        )
        self._catalog: list[dict] | None = None
        self._catalog_at = 0.0

    @property
    def base_url(self) -> str:
        return _env("CORP_TTS_BASE_URL", "TTS_BASE_URL", default="https://tts.cloudsmasters.ru/v1").rstrip("/")

    @property
    def api_key(self) -> str:
        return _env("CORP_TTS_API_KEY", "TTS_API_KEY")

    def available(self) -> bool:
        return bool(self.api_key)

    def voice_list(self) -> list[dict]:
        """Голоса с сервера (GET /v1/voices): новый пресет в voices/ появляется на сайте сам.

        Список кешируется на минуту; если сервер недоступен — берём CORP_TTS_VOICES / встроенный.
        """
        if _env("CORP_TTS_VOICES") or not self.available():
            return super().voice_list()
        if self._catalog is None or time.time() - self._catalog_at > CATALOG_TTL:
            self._catalog_at = time.time()
            try:
                resp = httpx.get(f"{self.base_url}/voices", timeout=4,
                                 headers={"Authorization": f"Bearer {self.api_key}"})
                resp.raise_for_status()
                self._catalog = [
                    {"id": v["name"], "name": v.get("title") or v["name"], "instruct": v.get("instruct") or ""}
                    for v in resp.json().get("voices", []) if v.get("name")
                ] or None
            except (httpx.HTTPError, ValueError, KeyError, AttributeError):
                self._catalog = None
        return self._catalog or super().voice_list()

    def synthesize(self, text: str, voice: str, speed: float, out: Path, instruct: str | None = None) -> None:
        # httpx кодирует JSON в UTF-8 — кириллица в теле не ломается.
        body = {"model": "tts-1", "voice": voice, "input": text, "response_format": "wav", "speed": speed}
        # Нет поля — сервер берёт instruct из пресета голоса; пустая строка — без инструкции.
        if instruct is not None:
            body["instruct"] = instruct
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

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> list[dict]:
        import edge_tts

        # Отрицательный rate сервис сейчас отвергает — замедление делаем через ffmpeg atempo.
        rate = f"+{round((max(speed, 1.0) - 1) * 100)}%"

        async def stream() -> list[dict]:
            # boundary="WordBoundary" — реальные тайминги каждого слова (по ним строятся субтитры)
            communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
            words = []
            with out.open("wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        start = chunk["offset"] / 1e7  # единицы — 100 нс
                        end = start + chunk["duration"] / 1e7
                        words.append({"text": chunk["text"], "start": round(start, 3), "end": round(end, 3)})
            return words

        last_error: Exception | None = None
        for attempt in range(5):  # длинные фрагменты иногда обрываются — повторяем с паузой
            try:
                words = asyncio.run(stream())
                if out.exists() and out.stat().st_size > 0:
                    break
                raise TTSError("пустой ответ")
            except Exception as exc:  # noqa: BLE001 — сеть/сервис, повторяем
                last_error = exc
                if getattr(exc, "status", None) in (401, 403):
                    # Отказ сервиса (например, Microsoft не пускает IP дата-центра) — повторы не помогут
                    raise TTSError(f"Edge TTS: доступ запрещён (HTTP {exc.status})") from exc
                time.sleep(1.5 * (attempt + 1))
        else:
            raise TTSError(f"Edge TTS не ответил после 5 попыток: {last_error}")
        if speed < 1.0:
            _atempo(out, speed)
            words = [{**w, "start": round(w["start"] / speed, 3), "end": round(w["end"] / speed, 3)} for w in words]
        return words


# ---------- Yandex SpeechKit ----------

class YandexProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            id="yandex",
            name="Yandex SpeechKit",
            voices=_parse_voices(os.getenv("YANDEX_TTS_VOICES", ""), [
                ("alena", "Алёна"),
                ("alena+good", "Алёна — эмоциональная"),
                ("filipp", "Филипп"),
                ("jane", "Джейн"),
                ("jane+good", "Джейн — эмоциональная"),
                ("ermil", "Ермил"),
                ("ermil+good", "Ермил — эмоциональный"),
                ("zahar", "Захар"),
                ("zahar+good", "Захар — эмоциональный"),
                ("omazh", "Омаж"),
            ]),
            note="Оплата в рублях через Yandex Cloud",
            env_keys=["YANDEX_API_KEY"],
        )

    def synthesize(self, text: str, voice: str, speed: float, out: Path) -> None:
        # «alena+good» — голос и эмоция (good — радостная, evil — раздражённая; есть не у всех голосов)
        voice, _, emotion = voice.partition("+")
        data = {"text": text, "lang": "ru-RU", "voice": voice, "speed": f"{speed:.2f}",
                "format": "lpcm", "sampleRateHertz": "48000"}
        if emotion:
            data["emotion"] = emotion
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


CATALOG_TTL = 60
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def check_instruct(instruct: str | None) -> str | None:
    """CosyVoice выполняет инструкцию только на английском (или китайском): русскую он произносит вслух."""
    if instruct is None:
        return None
    instruct = " ".join(instruct.split())
    if CYRILLIC.search(instruct):
        raise TTSError("Инструкцию по подаче пишите по-английски: русскую модель зачитывает вслух. "
                       "Например: speak energetically, upbeat tone")
    if len(instruct) > 300:
        raise TTSError("Инструкция по подаче слишком длинная (до 300 символов)")
    return instruct


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
            {"id": p.id, "name": p.name, "available": p.available(),
             "note": p.note + (". Если Microsoft не ответит — озвучим клоном этого голоса на своём TTS"
                               if p.id == "edge" and edge_fallback("") else ""),
             "instruct": p.supports_instruct, "voices": p.voice_list()}
            for p in PROVIDERS.values()
        ],
    }


def cache_dir() -> Path:
    d = config.DATA_DIR / "tts_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm(token: str) -> str:
    return re.sub(r"[^\w]+", "", token.lower())


def display_tokens(text: str) -> list[str]:
    """Слова для субтитров: знаки препинания («—», «…») приклеиваем к соседнему слову."""
    tokens: list[str] = []
    for token in text.split():
        if _norm(token) or not tokens:
            tokens.append(token)
        else:
            tokens[-1] += " " + token
    return tokens


def attach_display_text(words: list[dict], text: str) -> list[dict]:
    """Сервис отдаёт слова без пунктуации — подставляем исходное написание, если слова сошлись."""
    tokens = display_tokens(text)
    if len(tokens) == len(words):
        return [{**w, "text": t} for w, t in zip(words, tokens)]
    return words


def speech_bounds(path: Path, duration: float) -> tuple[float, float]:
    """Где в файле начинается и заканчивается речь (без тишины по краям)."""
    proc = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "silencedetect=n=-40dB:d=0.08",
                           "-f", "null", "-"], capture_output=True, text=True)
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    begin, finish = 0.0, duration
    if starts and starts[0] <= 0.01 and ends:
        begin = ends[0]
    if starts and starts[-1] > begin and (len(ends) < len(starts) or ends[-1] >= duration - 0.05):
        finish = starts[-1]
    if finish - begin < 0.2:
        return 0.0, duration
    return begin, finish


def proportional_words(text: str, begin: float, end: float) -> list[dict]:
    """Разложить слова по времени пропорционально длине (для сервисов без таймингов)."""
    tokens = display_tokens(text)
    weights = [len(_norm(t)) + 1 for t in tokens]
    total = sum(weights) or 1
    words, t = [], begin
    for token, weight in zip(tokens, weights):
        length = (end - begin) * weight / total
        words.append({"text": token, "start": round(t, 3), "end": round(t + length, 3)})
        t += length
    return words


# Запасной голос для edge-tts: клоны Светланы и Дмитрия на своём TTS.
EDGE_FALLBACK_VOICES = {"ru-RU-SvetlanaNeural": "svetlana", "ru-RU-DmitryNeural": "dmitry"}
EDGE_COOLDOWN = 600  # после отказа edge столько секунд сразу идём в запасной голос
_edge_down_until = 0.0
_edge_down_reason = ""


def edge_fallback(voice: str) -> tuple[str, str] | None:
    """(провайдер, голос) для замены edge-tts или None, если замена выключена/невозможна."""
    if _env("EDGE_FALLBACK", default="corp").lower() in ("", "0", "off", "false", "no"):
        return None
    corp = PROVIDERS.get("corp")
    if corp is None or not corp.available():
        return None
    return "corp", EDGE_FALLBACK_VOICES.get(voice, _env("EDGE_FALLBACK_VOICE", default="svetlana"))


def synthesize(text: str, provider: str, voice: str = "", speed: float = 1.0,
               instruct: str | None = None) -> Speech:
    """Озвучить фразу (с кешем): аудио, длительность и тайминги слов.

    Если edge-tts недоступен, фраза озвучивается клоном того же голоса на своём TTS
    (ru-RU-SvetlanaNeural → svetlana), а в Speech.fallback пишется пояснение.
    """
    global _edge_down_until, _edge_down_reason
    if provider != "edge":
        return _synthesize(text, provider, voice, speed, instruct)

    voice = voice or PROVIDERS["edge"].voice_list()[0]["id"]
    fallback = edge_fallback(voice)
    reason = ""
    if fallback is None or time.time() >= _edge_down_until:
        try:
            speech = _synthesize(text, provider, voice, speed, instruct)
            _edge_down_until = 0.0
            return speech
        except TTSError as exc:
            if fallback is None:
                raise
            reason = str(exc).removeprefix("Edge TTS: ")
            _edge_down_until, _edge_down_reason = time.time() + EDGE_COOLDOWN, reason
    speech = _synthesize(text, fallback[0], fallback[1], speed, None)
    speech.fallback = (f"Edge TTS недоступен ({reason or _edge_down_reason}) — "
                       f"озвучено голосом «{fallback[1]}» своего TTS")
    return speech


def _synthesize(text: str, provider: str, voice: str = "", speed: float = 1.0,
                instruct: str | None = None) -> Speech:
    """Озвучить фразу (с кешем): аудио, длительность и тайминги слов."""
    text = " ".join(text.split())
    if not text:
        raise TTSError("Пустой текст для озвучки")
    p = PROVIDERS.get(provider)
    if p is None:
        raise TTSError(f"Неизвестный провайдер озвучки: {provider}")
    if not p.available():
        raise TTSError(f"Провайдер «{p.name}» не настроен — проверьте ключи в .env")
    voice = voice or p.voice_list()[0]["id"]
    speed = max(0.5, min(2.0, float(speed or 1.0)))
    instruct = check_instruct(instruct) if p.supports_instruct else None
    # None (инструкция из пресета) и "" (без инструкции) — разные записи кеша
    mode = "preset" if instruct is None else f"instruct:{instruct}"
    key = hashlib.sha256(f"{p.id}|{voice}|{speed:.2f}|{mode}|{text}".encode()).hexdigest()[:24]
    out = cache_dir() / f"{p.id}_{key}.{p.ext}"
    meta = out.with_suffix(".words.json")
    if not out.exists() or out.stat().st_size == 0:
        tmp = out.with_name(out.stem + ".part." + p.ext)
        try:
            if p.supports_instruct:
                words = p.synthesize(text, voice, speed, tmp, instruct=instruct)
            else:
                words = p.synthesize(text, voice, speed, tmp)
        except TTSError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # сетевые и прочие ошибки — понятным сообщением
            tmp.unlink(missing_ok=True)
            raise TTSError(f"{p.name}: {exc}") from exc
        tmp.replace(out)
        meta.unlink(missing_ok=True)
        if words:
            meta.write_text(json.dumps(attach_display_text(words, text), ensure_ascii=False), encoding="utf-8")
    duration = probe(out).get("duration", 0.0)
    if duration <= 0:
        out.unlink(missing_ok=True)
        raise TTSError(f"{p.name} вернул пустое аудио")
    if meta.exists():
        return Speech(out, duration, json.loads(meta.read_text(encoding="utf-8")), True)
    begin, end = speech_bounds(out, duration)
    return Speech(out, duration, proportional_words(text, begin, end), False)
