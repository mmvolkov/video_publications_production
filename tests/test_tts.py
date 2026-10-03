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
        def __init__(self, text, voice, rate="+0%", boundary="SentenceBoundary"):
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

    tts.PROVIDERS["elevenlabs"].synthesize("Привет", "voice123", 1.5, tmp_path / "e.mp3")
    assert seen[1]["url"].startswith("https://api.elevenlabs.io/v1/text-to-speech/voice123")
    assert seen[1]["headers"]["xi-api-key"] == "el"
    assert seen[1]["json"]["voice_settings"]["speed"] == 1.2  # ElevenLabs принимает 0.7–1.2
