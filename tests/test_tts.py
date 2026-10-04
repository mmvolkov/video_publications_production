import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main, render, storage, tts
from app.media import probe


class FakeProvider(tts.Provider):
    """Генерирует тон длительностью 0.05 c на символ — как будто диктор читает текст."""

    def __init__(self):
        super().__init__(id="fake", name="Fake", voices=[("v1", "Голос 1")])
        self.calls = []

    def synthesize(self, text, voice, speed, out):
        self.calls.append((text, voice, speed))
        seconds = max(0.3, len(text) * 0.05 / speed)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
                        "-f", "wav", str(out)], check=True)


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = FakeProvider()
    monkeypatch.setitem(tts.PROVIDERS, "fake", provider)
    return provider


def mean_volume(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True)
    line = next(l for l in proc.stderr.splitlines() if "mean_volume" in l)
    return float(line.split("mean_volume:")[1].split()[0])


def test_synthesize_is_cached(fake):
    s1 = tts.synthesize("Привет, мир", "fake", "v1", 1.0)
    s2 = tts.synthesize("Привет,   мир", "fake", "v1", 1.0)
    assert s1.path == s2.path and s1.duration == pytest.approx(s2.duration)
    assert len(fake.calls) == 1
    # сервис без таймингов — слова разложены пропорционально по длительности речи
    assert not s1.timed and [w["text"] for w in s1.words] == ["Привет,", "мир"]
    assert s1.words[-1]["end"] <= s1.duration + 0.01
    tts.synthesize("Привет, мир", "fake", "v1", 1.2)
    assert len(fake.calls) == 2


def test_unconfigured_provider_raises(fake, monkeypatch):
    monkeypatch.delenv("CORP_TTS_API_KEY", raising=False)
    monkeypatch.delenv("TTS_API_KEY", raising=False)
    with pytest.raises(tts.TTSError, match="не настроен"):
        tts.synthesize("текст", "corp")


def test_render_with_voiceover(fake, tmp_path):
    src = tmp_path / "m"
    src.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "mandelbrot=size=600x800",
                    "-frames:v", "1", str(src / "p.jpg")], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=800:duration=2",
                    str(src / "music.mp3")], check=True)
    script = {"cover_text": "", "scenes": [
        {"material_id": "", "text": "Хук", "voice": "Короткая фраза.", "duration": 4},
        {"material_id": "p", "text": "Без голоса", "voice": "", "duration": 2},
        {"material_id": "p", "text": "Длинная", "voice": "Очень длинная фраза диктора, которая не влезает в две секунды сцены.", "duration": 2},
    ]}
    result = render.render_reel(script, {"p": {"kind": "image", "file": "p.jpg"}}, src, tmp_path / "out",
                                music=src / "music.mp3", voiceover={"provider": "fake", "voice": "v1", "speed": 1.0})
    d = result["durations"]
    assert d[0] == pytest.approx(max(1.5, 15 * 0.05 + 0.55), abs=0.05)  # подогнано под голос, а не 4 с
    assert d[1] == pytest.approx(2.0)
    assert d[2] > 3.5
    video = tmp_path / "out" / "reel.mp4"
    info = probe(video)
    assert info["has_audio"]
    assert info["duration"] == pytest.approx(sum(d), abs=0.1)
    assert mean_volume(video) > -40


def test_api_reel_with_voiceover(fake):
    with TestClient(main.app) as client:
        assert any(p["id"] == "fake" and p["available"] for p in client.get("/api/tts").json()["providers"])
        r = client.post("/api/tts/preview", json={"provider": "fake", "voice": "v1", "text": "Проверка"})
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        assert client.post("/api/tts/preview", json={"provider": "nope", "text": "x"}).status_code == 400

        pid = client.post("/api/projects", json={"title": "Голос", "brief": {"topic": "Кофейня", "cta": "Заходите"}}).json()["id"]
        client.post(f"/api/projects/{pid}/notes", json={"text": "Варим кофе с семи утра."})
        bad = client.post(f"/api/projects/{pid}/reels", json={"voiceover": True, "tts_provider": "nope"})
        assert bad.status_code == 400
        reel = client.post(f"/api/projects/{pid}/reels", json={"duration": 15, "voiceover": True,
                                                               "tts_provider": "fake", "tts_voice": "v1"}).json()
        deadline = time.time() + 120
        while time.time() < deadline:
            reel = storage.find(storage.get_project(pid)["reels"], reel["id"])
            if reel["status"] in ("done", "error"):
                break
            time.sleep(0.3)
        assert reel["status"] == "done", reel.get("error")
        assert all(s["voice"] for s in reel["script"]["scenes"])
        assert {c[0] for c in fake.calls} >= {"Кофейня", "Заходите"}
        assert reel["duration"] == pytest.approx(sum(s["duration"] for s in reel["script"]["scenes"]), abs=0.05)

        # в редакторе выключаем озвучку — видео пересобирается без голоса
        script = dict(reel["script"], voice_options={"voiceover": False})
        assert client.put(f"/api/projects/{pid}/reels/{reel['id']}/script", json=script).status_code == 200
        time.sleep(0.5)
        while storage.find(storage.get_project(pid)["reels"], reel["id"])["status"] != "done":
            time.sleep(0.3)
        assert storage.find(storage.get_project(pid)["reels"], reel["id"])["options"]["voiceover"] is False


def test_corp_request_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("CORP_TTS_API_KEY", "secret-key")
    monkeypatch.setenv("CORP_TTS_BASE_URL", "http://tts:8000/v1/")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, **_):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, content=b"RIFFxxxx")

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    out = tmp_path / "a.wav"
    tts.PROVIDERS["corp"].synthesize("Проверка синтеза речи.", "anastasiya", 1.1, out)
    assert seen["url"] == "http://tts:8000/v1/audio/speech"
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    assert seen["json"] == {"model": "tts-1", "voice": "anastasiya", "input": "Проверка синтеза речи.",
                            "response_format": "wav", "speed": 1.1}
    # httpx отправит тело в UTF-8
    body = httpx.Request("POST", seen["url"], json=seen["json"]).content
    assert "Проверка".encode("utf-8") in body or b"\\u041f" in body
    assert json.loads(body)["input"] == "Проверка синтеза речи."
    assert out.read_bytes() == b"RIFFxxxx"


def test_corp_falls_back_to_studio_env(monkeypatch):
    monkeypatch.delenv("CORP_TTS_API_KEY", raising=False)
    monkeypatch.delenv("CORP_TTS_BASE_URL", raising=False)
    monkeypatch.setenv("TTS_API_KEY", "k")
    monkeypatch.setenv("TTS_BASE_URL", "http://tts:8000/v1")
    corp = tts.PROVIDERS["corp"]
    assert corp.available() and corp.api_key == "k" and corp.base_url == "http://tts:8000/v1"


def test_corp_http_error_is_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("CORP_TTS_API_KEY", "k")
    monkeypatch.setattr(tts.httpx, "post", lambda *a, **k: httpx.Response(400, json={"detail": "unknown voice"}))
    with pytest.raises(tts.TTSError, match="HTTP 400"):
        tts.PROVIDERS["corp"].synthesize("т", "nobody", 1.0, tmp_path / "x.wav")


def test_edge_rate_retry_and_slowdown(monkeypatch, tmp_path):
    import edge_tts

    calls = []

    tone = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(tone)], check=True)

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", boundary="SentenceBoundary", proxy=None):
            calls.append(rate)

        async def stream(self):
            if len(calls) == 1:
                raise ConnectionError("обрыв")
            yield {"type": "WordBoundary", "offset": 0, "duration": 8_000_000, "text": "Привет"}
            yield {"type": "audio", "data": tone.read_bytes()}

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    out = tmp_path / "e.mp3"
    tts.PROVIDERS["edge"].synthesize("Привет", "ru-RU-SvetlanaNeural", 1.2, out)
    assert calls == ["+20%", "+20%"]  # первая попытка оборвалась, вторая успешна

    calls.clear()
    calls.append("skip-failure")  # следующая попытка сразу успешна
    slow = tmp_path / "slow.mp3"
    words = tts.PROVIDERS["edge"].synthesize("Привет", "ru-RU-SvetlanaNeural", 0.8, slow)
    assert calls[-1] == "+0%"  # отрицательный rate не отправляем
    assert probe(slow)["duration"] == pytest.approx(1 / 0.8, abs=0.1)  # замедлено через atempo
    assert words[0]["end"] == pytest.approx(0.8 / 0.8)  # тайминги растянуты вместе со звуком


def test_yandex_and_elevenlabs_request_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_API_KEY", "ya")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el")
    seen = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None, **_):
        seen.append({"url": url, "data": data, "json": json, "headers": headers})
        if "yandex" in url:
            return httpx.Response(200, content=b"\x00\x00" * 48000)  # 1 c тишины lpcm 48 кГц
        return httpx.Response(200, content=b"ID3")

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    ya_out = tmp_path / "y.wav"
    tts.PROVIDERS["yandex"].synthesize("Привет", "alena", 1.0, ya_out)
    assert seen[0]["headers"]["Authorization"] == "Api-Key ya"
    assert seen[0]["data"]["voice"] == "alena" and seen[0]["data"]["format"] == "lpcm"
    assert probe(ya_out)["duration"] == pytest.approx(1.0, abs=0.05)
    assert "emotion" not in seen[0]["data"]
    tts.PROVIDERS["yandex"].synthesize("Привет", "alena+good", 1.0, tmp_path / "y2.wav")
    assert seen[1]["data"]["voice"] == "alena" and seen[1]["data"]["emotion"] == "good"
    seen.pop(1)

    tts.PROVIDERS["elevenlabs"].synthesize("Привет", "voice123", 1.5, tmp_path / "e.mp3")
    assert seen[1]["url"].startswith("https://api.elevenlabs.io/v1/text-to-speech/voice123")
    assert seen[1]["headers"]["xi-api-key"] == "el"
    assert seen[1]["json"]["voice_settings"]["speed"] == 1.2  # ElevenLabs принимает 0.7–1.2


# ---------- голоса с сервера и instruct ----------

@pytest.fixture()
def corp(monkeypatch):
    monkeypatch.setenv("CORP_TTS_API_KEY", "k")
    monkeypatch.setenv("CORP_TTS_BASE_URL", "http://tts:8000/v1")
    monkeypatch.delenv("CORP_TTS_VOICES", raising=False)
    provider = tts.CorpProvider()
    monkeypatch.setitem(tts.PROVIDERS, "corp", provider)
    return provider


def test_corp_voices_come_from_server(corp, monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        return httpx.Response(200, request=httpx.Request("GET", url), json={"voices": [
            {"name": "anastasiya", "title": "Анастасия", "instruct": "speak calmly", "prompt_text": "…"},
            {"name": "irina", "title": "Ирина — новый пресет", "instruct": ""},
        ]})

    monkeypatch.setattr(tts.httpx, "get", fake_get)
    voices = corp.voice_list()
    assert voices == [{"id": "anastasiya", "name": "Анастасия", "instruct": "speak calmly"},
                      {"id": "irina", "name": "Ирина — новый пресет", "instruct": ""}]
    assert calls == [("http://tts:8000/v1/voices", {"Authorization": "Bearer k"})]
    corp.voice_list()
    assert len(calls) == 1  # кеш на минуту
    described = next(p for p in tts.describe()["providers"] if p["id"] == "corp")
    assert described["instruct"] is True and described["voices"][1]["id"] == "irina"


def test_corp_voices_fallback_when_server_down(corp, monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("нет связи")

    monkeypatch.setattr(tts.httpx, "get", down)
    assert [v["id"] for v in corp.voice_list()] == ["anastasiya", "dmitry", "svetlana"]


def test_corp_voices_env_override_skips_server(corp, monkeypatch):
    monkeypatch.setenv("CORP_TTS_VOICES", "x:Икс")
    monkeypatch.setattr(tts.httpx, "get", lambda *a, **k: pytest.fail("не должен ходить на сервер"))
    corp.voices = tts._parse_voices("x:Икс", [])
    assert corp.voice_list() == [{"id": "x", "name": "Икс", "instruct": ""}]


@pytest.mark.parametrize("instruct, expected", [
    (None, "absent"),  # из пресета голоса
    ("", ""),          # явно без инструкции
    ("speak energetically", "speak energetically"),
])
def test_corp_sends_instruct(corp, monkeypatch, tmp_path, instruct, expected):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, **_):
        seen.update(json)
        return httpx.Response(200, content=b"RIFF")

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    corp.synthesize("Привет", "anastasiya", 1.0, tmp_path / "a.wav", instruct=instruct)
    assert seen.get("instruct", "absent") == expected


def test_instruct_must_be_english():
    with pytest.raises(tts.TTSError, match="по-английски"):
        tts.check_instruct("говори бодро")
    assert tts.check_instruct("  speak   calmly ") == "speak calmly"
    assert tts.check_instruct(None) is None and tts.check_instruct("") == ""


def test_instruct_is_part_of_cache_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    seen = []

    class InstructFake(FakeProvider):
        def __init__(self):
            super().__init__()
            self.id, self.supports_instruct = "ifake", True

        def synthesize(self, text, voice, speed, out, instruct=None):
            seen.append(instruct)
            return super().synthesize(text, voice, speed, out)

    monkeypatch.setitem(tts.PROVIDERS, "ifake", InstructFake())
    for instruct in (None, "", "speak calmly", None):
        tts.synthesize("Привет", "ifake", "v1", 1.0, instruct)
    assert seen == [None, "", "speak calmly"]  # повтор None взят из кеша
    # провайдер без поддержки instruct просто игнорирует его
    monkeypatch.setitem(tts.PROVIDERS, "fake", FakeProvider())
    tts.synthesize("Привет", "fake", "v1", 1.0, "speak calmly")


def test_api_rejects_russian_instruct(fake):
    with TestClient(main.app) as client:
        pid = client.post("/api/projects", json={"title": "x"}).json()["id"]
        client.post(f"/api/projects/{pid}/notes", json={"text": "Текст"})
        r = client.post(f"/api/projects/{pid}/reels", json={"voiceover": True, "tts_provider": "fake",
                                                             "tts_instruct": "бодро"})
        assert r.status_code == 400 and "по-английски" in r.json()["detail"]


# ---------- замена edge-tts на клон своего TTS ----------

class FailingEdge(tts.Provider):
    def __init__(self):
        super().__init__(id="edge", name="Edge TTS (Microsoft)", voices=[("ru-RU-SvetlanaNeural", "Светлана")])
        self.calls = 0

    def synthesize(self, text, voice, speed, out):
        self.calls += 1
        raise tts.TTSError("Edge TTS: доступ запрещён (HTTP 403)")


class FakeCorp(FakeProvider):
    def __init__(self):
        super().__init__()
        self.id, self.supports_instruct = "corp", True

    def synthesize(self, text, voice, speed, out, instruct=None):
        self.calls.append((text, voice, speed, instruct))
        _tone(out)


def _tone(out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.8", "-f", "wav", str(out)],
                   check=True)


@pytest.fixture()
def edge_down(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(tts, "_edge_down_until", 0.0)
    monkeypatch.delenv("EDGE_FALLBACK", raising=False)
    edge, corp = FailingEdge(), FakeCorp()
    monkeypatch.setitem(tts.PROVIDERS, "edge", edge)
    monkeypatch.setitem(tts.PROVIDERS, "corp", corp)
    return edge, corp


def test_edge_falls_back_to_corp_clone(edge_down):
    edge, corp = edge_down
    speech = tts.synthesize("Привет", "edge", "ru-RU-SvetlanaNeural", 1.1, "speak calmly")
    assert corp.calls == [("Привет", "svetlana", 1.1, None)]  # инструкции у edge нет — пресет клона
    assert "svetlana" in speech.fallback and "403" in speech.fallback
    assert "Edge TTS недоступен (доступ запрещён" in speech.fallback
    # следующая фраза в течение 10 минут сразу идёт в запасной голос, без попытки edge
    tts.synthesize("Ещё фраза", "edge", "ru-RU-DmitryNeural", 1.0)
    assert edge.calls == 1
    assert corp.calls[-1][1] == "dmitry"


def test_edge_retried_after_cooldown(edge_down, monkeypatch):
    edge, _ = edge_down
    tts.synthesize("Раз", "edge")
    monkeypatch.setattr(tts, "_edge_down_until", 0.0)  # прошло 10 минут
    tts.synthesize("Два", "edge")
    assert edge.calls == 2


def test_edge_fallback_can_be_disabled(edge_down, monkeypatch):
    monkeypatch.setenv("EDGE_FALLBACK", "off")
    with pytest.raises(tts.TTSError, match="403"):
        tts.synthesize("Привет", "edge")


def test_edge_fallback_needs_corp_key(edge_down, monkeypatch):
    _, corp = edge_down
    monkeypatch.setattr(corp, "available", lambda: False)
    with pytest.raises(tts.TTSError, match="403"):
        tts.synthesize("Привет", "edge")


def test_edge_403_is_not_retried(monkeypatch, tmp_path):
    import edge_tts

    attempts = []

    class Forbidden(Exception):
        status = 403

    class FakeCommunicate:
        def __init__(self, *a, **k):
            attempts.append(1)

        async def stream(self):
            raise Forbidden("Invalid response status")
            yield  # noqa: unreachable — делает метод асинхронным генератором

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    monkeypatch.setattr(tts.time, "sleep", lambda s: pytest.fail("не должно быть пауз между повторами"))
    with pytest.raises(tts.TTSError, match="HTTP 403"):
        tts.EdgeProvider().synthesize("Привет", "ru-RU-SvetlanaNeural", 1.0, tmp_path / "a.mp3")
    assert len(attempts) == 1


def test_fallback_note_reaches_reel_and_preview(edge_down):
    with TestClient(main.app) as client:
        r = client.post("/api/tts/preview", json={"provider": "edge", "voice": "ru-RU-SvetlanaNeural"})
        assert r.status_code == 200
        from urllib.parse import unquote
        assert "svetlana" in unquote(r.headers["X-TTS-Fallback"])

        pid = client.post("/api/projects", json={"title": "x", "brief": {"topic": "Кофе"}}).json()["id"]
        client.post(f"/api/projects/{pid}/notes", json={"text": "Варим кофе."})
        reel = client.post(f"/api/projects/{pid}/reels", json={"duration": 15, "voiceover": True,
                                                               "tts_provider": "edge"}).json()
        deadline = time.time() + 120
        while time.time() < deadline:
            reel = storage.find(storage.get_project(pid)["reels"], reel["id"])
            if reel["status"] in ("done", "error"):
                break
            time.sleep(0.3)
        assert reel["status"] == "done", reel.get("error")
        assert "svetlana" in reel["voice_note"]


# ---------- свой TTS: разбивка на предложения с паузами ----------

def _tone_bytes(seconds: float) -> bytes:
    """WAV: 0.2 c тишины + тон + 0.2 c тишины — как ответ TTS с полями тишины."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"aevalsrc='if(between(t,0.2,{0.2 + seconds}),0.5*sin(2*PI*300*t),0)':s=24000:d={seconds + 0.4}",
         "-f", "wav", "-"], capture_output=True, check=True).stdout


def test_split_sentences_rules():
    assert tts.split_sentences("Нет! Жидкость может взрываться!") == ["Нет! Жидкость может взрываться!"]
    assert tts.split_sentences("Первое предложение. Второе? Третье, длинное!") == \
        ["Первое предложение.", "Второе? Третье, длинное!"]
    assert tts.split_sentences("Без точки") == ["Без точки"]


def test_corp_splits_sentences_with_pauses(corp, monkeypatch, tmp_path):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None, **_):
        sent.append(json["input"])
        return httpx.Response(200, content=_tone_bytes(1.0))

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    monkeypatch.delenv("CORP_TTS_SENTENCE_GAP", raising=False)
    out = tmp_path / "a.wav"
    words = corp.synthesize("Это доказали ИИ-агенты! Десять тысяч агентов за восемьдесят восемь часов!",
                            "gemini", 1.0, out, instruct=None)
    assert sent == ["Это доказали ИИ-агенты!", "Десять тысяч агентов за восемьдесят восемь часов!"]
    # два куска речи по ~1.08 c (тишина по краям срезана до 0.04 c) + пауза 0.35×1.25 после «!»
    assert probe(out)["duration"] == pytest.approx(2 * 1.08 + 0.4375, abs=0.08)
    assert [w["text"] for w in words][:3] == ["Это", "доказали", "ИИ-агенты!"]
    second = next(w for w in words if w["text"] == "Десять")
    assert second["start"] == pytest.approx(1.08 + 0.4375 + 0.04, abs=0.08)
    assert not list(tmp_path.glob("a.s*.wav"))  # временные куски удалены


def test_corp_single_sentence_or_disabled_is_one_request(corp, monkeypatch, tmp_path):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None, **_):
        sent.append(json["input"])
        return httpx.Response(200, content=_tone_bytes(1.0))

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    assert corp.synthesize("Одно предложение.", "gemini", 1.0, tmp_path / "a.wav") is None
    monkeypatch.setenv("CORP_TTS_SENTENCE_GAP", "0")
    corp.synthesize("Раз предложение. Два предложение.", "gemini", 1.0, tmp_path / "b.wav")
    assert sent == ["Одно предложение.", "Раз предложение. Два предложение."]


def test_sentence_gap_is_part_of_cache_key(corp, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    calls = []
    monkeypatch.setattr(tts.httpx, "post", lambda *a, **k: calls.append(1) or httpx.Response(200, content=_tone_bytes(0.5)))
    tts.synthesize("Фраза номер один. Фраза номер два.", "corp", "gemini")
    tts.synthesize("Фраза номер один. Фраза номер два.", "corp", "gemini")
    assert len(calls) == 2  # второй раз — из кеша
    monkeypatch.setenv("CORP_TTS_SENTENCE_GAP", "0.5")
    speech = tts.synthesize("Фраза номер один. Фраза номер два.", "corp", "gemini")
    assert len(calls) == 4 and speech.timed  # другая пауза — заново; тайминги по предложениям сохранены
