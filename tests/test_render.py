"""Качество рилса: выделение слов, переходы, растягивание видео, обрезка тишины, звук роликов, кадры для ИИ."""
import subprocess

import pytest

from app import ai, config, render, storage, tts
from app.media import probe

FF = ["ffmpeg", "-y", "-v", "error"]


def test_keywords_in_stars():
    assert render.parse_marked("Вы теряете *половину* клиентов") == [
        ("Вы", False), ("теряете", False), ("половину", True), ("клиентов", False)]
    assert render.parse_marked("*Два слова* и всё") == [("Два", True), ("слова", True), ("и", False), ("всё", False)]
    assert render.strip_marks("Как *удвоить* продажи") == "Как удвоить продажи"
    font = render._font(60)
    assert render.wrap_text("Очень *длинный* текст " * 6, font, 500)[0].count("*") == 0


def test_text_animation_pops(tmp_path):
    anim = render.text_animation("Решение за *5 минут*", "caption", tmp_path, "t")
    frames = sorted(tmp_path.glob("t_*.png"))
    assert len(frames) == len(render.POP)
    from PIL import Image
    first, last = Image.open(frames[0]), Image.open(frames[-1])
    assert first.getchannel("A").getextrema()[1] == 0  # первый кадр прозрачный
    assert last.getchannel("A").getextrema()[1] == 255
    assert 0 < anim.y < config.HEIGHT and 0 <= anim.x < config.WIDTH
    empty = render.text_animation("", "card", tmp_path, "e")
    assert (empty.x, empty.y) == (0, 0) and (tmp_path / "e_00.png").exists()


def test_transitions_and_clip_plan():
    assert render.transition_names(0) == []
    assert render.transition_names(1) == ["zoomin"]
    names = render.transition_names(5)
    assert names[0] == "zoomin" and names[-1] == "fade" and len(names) == 5
    assert render.clip_plan(3, 0, 10) == (1.0, False)          # хватает ролика
    slow, loop = render.clip_plan(4.5, 1, 4)                   # не хватает 1.5 с — замедляем
    assert slow == pytest.approx(1.5) and not loop
    assert render.clip_plan(9, 0, 3) == (1.0, True)            # втрое длиннее — зацикливаем
    assert render.clip_plan(9, 0, 0) == (1.0, False)           # длительность неизвестна


def test_trim_silence(tmp_path):
    src = tmp_path / "v.wav"
    subprocess.run([*FF, "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono:d=0.5", "-f", "lavfi",
                    "-i", "sine=frequency=300:duration=1:sample_rate=24000", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono:d=0.7",
                    "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1", str(src)], check=True)
    head, length = render.trim_silence(src, tmp_path / "t.wav")
    assert head == pytest.approx(0.47, abs=0.05)
    assert length == pytest.approx(1.11, abs=0.06)
    assert probe(tmp_path / "t.wav")["duration"] == pytest.approx(length, abs=0.02)
    tone = tmp_path / "tone.wav"
    subprocess.run([*FF, "-f", "lavfi", "-i", "sine=frequency=300:duration=1", str(tone)], check=True)
    assert render.trim_silence(tone, tmp_path / "x.wav") is None  # тишины нет — файл не трогаем


def test_render_with_clip_audio_and_short_video(tmp_path):
    src = tmp_path / "m"
    src.mkdir()
    subprocess.run([*FF, "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
                    "-i", "sine=frequency=500:duration=2", "-t", "2", "-shortest", str(src / "c.mp4")], check=True)
    mats = {"c": {"kind": "video", "file": "c.mp4", **probe(src / "c.mp4")}}
    script = {"cover_text": "*Обложка*", "cover_material_id": "c", "scenes": [
        {"material_id": "c", "text": "Ролик длиннее исходника", "voice": "", "duration": 4.5, "start": 0.5},
        {"material_id": "", "text": "Финал", "voice": "", "duration": 1.5},
    ]}
    result = render.render_reel(script, mats, src, tmp_path / "out", clip_audio=True)
    assert result["durations"] == [4.5, 1.5]
    video = tmp_path / "out" / "reel.mp4"
    info = probe(video)
    assert info["has_audio"] and info["duration"] == pytest.approx(6.0, abs=0.1)
    proc = subprocess.run(["ffmpeg", "-nostats", "-i", str(video), "-af", "ebur128", "-f", "null", "-"],
                          capture_output=True, text=True)
    integrated = float(proc.stderr.rsplit("I:", 1)[1].split("LUFS")[0])
    assert integrated == pytest.approx(-14, abs=2)  # звук ролика слышен и нормализован
    assert (tmp_path / "out" / "cover.jpg").exists()


def test_edge_no_audio_is_not_retried(monkeypatch, tmp_path):
    import edge_tts

    attempts = []

    class NoAudioReceived(Exception):
        pass

    class FakeCommunicate:
        def __init__(self, *a, **k):
            attempts.append(1)

        async def stream(self):
            raise NoAudioReceived("No audio was received")
            yield  # noqa: unreachable — делает метод асинхронным генератором

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    monkeypatch.setattr(tts.time, "sleep", lambda s: pytest.fail("не должно быть пауз между повторами"))
    with pytest.raises(tts.TTSError, match="NoAudioReceived"):
        tts.EdgeProvider().synthesize("Привет", "ru-RU-SvetlanaNeural", 1.0, tmp_path / "a.mp3")
    assert len(attempts) == 1


def test_ai_sees_video_frames_with_timecodes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    p = storage.create_project("Видео", {})
    pdir = storage.project_dir(p["id"])
    (pdir / "materials").mkdir(parents=True, exist_ok=True)
    (pdir / "thumbs").mkdir(parents=True, exist_ok=True)
    subprocess.run([*FF, "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "8",
                    str(pdir / "materials" / "c.mp4")], check=True)
    project = {**p, "materials": [{"id": "c", "kind": "video", "file": "c.mp4", "duration": 8.0}]}
    content = ai.build_messages(project, {"duration": 15})[0]["content"]
    labels = [b["text"] for b in content if b["type"] == "text" and b["text"].startswith("кадр на")]
    assert labels == ["кадр на 1.0 с:", "кадр на 3.0 с:", "кадр на 5.0 с:", "кадр на 7.0 с:"]
    assert sum(b["type"] == "image" for b in content) == 4
    assert "start" in ai.SCRIPT_SCHEMA["properties"]["scenes"]["items"]["required"]

    script = ai.normalize_script({"cover_material_id": "c", "scenes": [
        {"material_id": "c", "text": "a", "voice": "", "duration": 6, "start": 5},
        {"material_id": "c", "text": "b", "voice": "", "duration": 3, "start": 50},
    ]}, project)
    assert script["cover_material_id"] == "c"
    assert script["scenes"][0] == {"material_id": "c", "text": "a", "voice": "", "duration": 3.0, "start": 5.0}
    assert script["scenes"][1]["start"] == 7.0 and script["scenes"][1]["duration"] == 1.0
    assert ai.normalize_script({"cover_material_id": "nope", "scenes": [{"text": "x"}]}, project)["cover_material_id"] == ""
