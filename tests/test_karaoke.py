import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import config, render, subtitles, tts


def words_for(text, step=0.4):
    return [{"text": t, "start": i * step, "end": i * step + step * 0.8} for i, t in enumerate(text.split())]


def test_chunks_break_on_sentences_and_length():
    chunks = subtitles.chunk_words(words_for("Хотите кофе, который реально будит? Смотрите до конца!"))
    assert [" ".join(w["text"] for w in c) for c in chunks] == \
        ["Хотите кофе,", "который реально", "будит?", "Смотрите до конца!"]


def test_lonely_last_word_is_merged():
    chunks = subtitles.chunk_words(words_for("Это наш кофе дня вкусно"))
    assert [len(c) for c in chunks] == [3, 2]


def test_ass_events_follow_word_timings(tmp_path):
    out = tmp_path / "s.ass"
    words = words_for("Раз два три. Четыре пять")
    n = subtitles.build_ass(words, offset=0.2, scene_end=3.0, font_name="DejaVu Sans", out=out)
    text = out.read_text(encoding="utf-8")
    assert n == 5
    events = [l for l in text.splitlines() if l.startswith("Dialogue")]
    # первое слово подсвечено с 0.20 до начала второго (0.60)
    assert events[0].startswith("Dialogue: 0,0:00:00.20,0:00:00.60,Karaoke")
    assert "{\\c&H0000E1FF&}Раз{\\c&H00FFFFFF&} два три." in events[0]
    # последнее слово группы держится до начала следующей группы
    assert ",0:00:01.00,0:00:01.40," in events[2]
    # последнее слово сцены не выходит за конец сцены
    assert events[-1].split(",")[2] <= "0:00:03.00"
    assert "PlayResY: 1920" in text


def test_ass_escapes_braces(tmp_path):
    out = tmp_path / "s.ass"
    subtitles.build_ass([{"text": "{bad}", "start": 0, "end": 1}], 0, 2, "DejaVu Sans", out)
    assert "(bad)" in out.read_text(encoding="utf-8")


def test_display_tokens_glue_punctuation():
    assert tts.display_tokens("Своя обжарка — всегда свежо !") == ["Своя", "обжарка —", "всегда", "свежо !"]
    words = [{"text": "Своя", "start": 0, "end": 1}, {"text": "обжарка", "start": 1, "end": 2}]
    assert [w["text"] for w in tts.attach_display_text(words, "Своя обжарка!")] == ["Своя", "обжарка!"]


def test_proportional_words_and_speech_bounds(tmp_path):
    wav = tmp_path / "s.wav"
    # 0.5 c тишины, 2 c тона, 0.5 c тишины
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,0.5,2.5),0.5*sin(2*PI*300*t),0)':d=3", str(wav)], check=True)
    begin, end = tts.speech_bounds(wav, 3.0)
    assert begin == pytest.approx(0.5, abs=0.05) and end == pytest.approx(2.5, abs=0.05)
    words = tts.proportional_words("а бббб", begin, end)
    assert words[0]["start"] == pytest.approx(begin, abs=0.01)
    assert words[1]["end"] == pytest.approx(end, abs=0.01)
    assert (words[1]["end"] - words[1]["start"]) > (words[0]["end"] - words[0]["start"])


def test_edge_returns_real_word_timings(monkeypatch, tmp_path):
    import edge_tts

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", boundary="SentenceBoundary", proxy=None):
            assert boundary == "WordBoundary"

        async def stream(self):
            yield {"type": "WordBoundary", "offset": 1_000_000, "duration": 4_000_000, "text": "Привет"}
            yield {"type": "WordBoundary", "offset": 6_000_000, "duration": 3_000_000, "text": "мир"}
            yield {"type": "audio", "data": b"\xff\xf3" * 10}

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    words = tts.PROVIDERS["edge"].synthesize("Привет, мир!", "ru-RU-SvetlanaNeural", 1.0, tmp_path / "a.mp3")
    assert words == [{"text": "Привет", "start": 0.1, "end": 0.5}, {"text": "мир", "start": 0.6, "end": 0.9}]
    assert [w["text"] for w in tts.attach_display_text(words, "Привет, мир!")] == ["Привет,", "мир!"]


class TimedFake(tts.Provider):
    """Как edge-tts: отдаёт тайминги слов."""

    def __init__(self):
        super().__init__(id="timed", name="Timed", voices=[("v", "v")])

    def synthesize(self, text, voice, speed, out):
        words, t = [], 0.0
        for w in text.split():
            words.append({"text": w, "start": t, "end": t + 0.5})
            t += 0.6
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=duration={t}", "-f", "wav",
                        str(out)], check=True)
        return words


def yellow_pixels(img: Image.Image) -> int:
    px = img.convert("RGB").resize((270, 480)).load()
    return sum(1 for x in range(270) for y in range(300, 400)
               if px[x, y][0] > 200 and px[x, y][1] > 180 and px[x, y][2] < 80)


@pytest.mark.parametrize("karaoke", [True, False])
def test_render_burns_karaoke(monkeypatch, tmp_path, karaoke):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setitem(tts.PROVIDERS, "timed", TimedFake())
    script = {"cover_text": "", "scenes": [{"material_id": "", "text": "Карточка", "voice": "Раз два три четыре", "duration": 2}]}
    render.render_reel(script, {}, tmp_path, tmp_path / "out", voiceover={"provider": "timed"}, karaoke=karaoke)
    frame = tmp_path / "f.png"
    # 0.2 с паузы + середина второго слова (0.6–1.1)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.05", "-i", str(tmp_path / "out" / "reel.mp4"),
                    "-frames:v", "1", str(frame)], check=True)
    found = yellow_pixels(Image.open(frame))
    assert (found > 20) is karaoke
