"""Рилс proj001 v2: компиляция видео Gemini + иллюстраций, озвучка клоном голоса из ролика Gemini.

Входы:
  in/proj001/gemini_generated_video_0883c3dd.mp4 — заставка (титр с цифрами → вихрь → вспышка), без речи
  in/proj001/gemini_generated_video_bcfd18b7.mp4 — анимации вихря, сети агентов, Lean, взрыва + голос
  in/proj001/*.png — 8 иллюстраций 9:16, tools/proj001_v2_voice.json — объединённый текст

Шаги: голос отделяется от музыки (Demucs) → образец + расшифровка → CosyVoice3 озвучивает новый текст
этим голосом (tools/clone_voice.py) → монтаж кадров по фразам → звук: музыка заставки, родные звуки
роликов под их кадрами, синтезированная подложка, голос сверху → out/proj001/reel_v2.mp4

    CV_PYTHON=/home/user/cv/venv/bin/python CV_ROOT=/home/user/cv/cosyvoice CV_MODEL=/home/user/cv/model \
        python tools/build_proj001_v2.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app import tts  # noqa: E402
from app.media import probe  # noqa: E402
from build_proj001 import FPS, H, W, music_bed, run, scene_clip  # noqa: E402

SRC = ROOT / "in" / "proj001"
OUT = ROOT / "out" / "proj001"
WORK = OUT / "work_v2"
CACHE = OUT / "voice_v2"  # синтезированные фразы — переживают пересборку
VIDEO_A = SRC / "gemini_generated_video_0883c3dd.mp4"
VIDEO_B = SRC / "gemini_generated_video_bcfd18b7.mp4"
# Что говорит голос в ролике B (Yandex STT + расставленная пунктуация) — затравка для клонирования
SAMPLE_TEXT = ("ИИ решил уравнение Навье — Стокса, задачу тысячелетия. Жидкость может взрываться, "
               "образуя сингулярность. Это доказали агенты ИИ. Прорыв века в науке.")

CV_PYTHON = os.getenv("CV_PYTHON", "/home/user/cv/venv/bin/python")
CV_ROOT = Path(os.getenv("CV_ROOT", "/home/user/cv/cosyvoice"))
CV_MODEL = os.getenv("CV_MODEL", "/home/user/cv/model")

LEAD, TAIL = 0.25, 0.3

# Сцены = фраза диктора + кадры. Кадр: {"img": n, ...движение} или {"video": "A"/"B", "from": c, "to": c}.
# Видеокадры имеют свою длину; последний кадр-картинка растягивается под фразу.
# "cut_word" — переключиться на следующий кадр на этом слове.
SCENES = [
    dict(line="intro", lead=0.7, shots=[dict(video="A", start=0.0, end=8.2, fill=True)],
         trans=("zoomin", 0.35), audio="A"),
    dict(line="water", lead=0.12, tail=0.08, shots=[dict(img=2, zoom=(1.10, 1.10, (0.44, 0.5), (0.56, 0.5)))],
         trans=("smoothleft", 0.25)),
    dict(line="plane", lead=0.12, tail=0.08, shots=[dict(img=3, zoom=(1.0, 1.10, (0.5, 0.45), (0.56, 0.4)))],
         trans=("smoothleft", 0.25)),
    dict(line="blood", lead=0.12, tail=1.0, shots=[dict(img=4, zoom=(1.0, 1.09, (0.5, 0.5), (0.5, 0.53)))],
         trans=("fadeblack", 0.5)),
    # «Нет! Жидкость может взрываться…» — живой закручивающийся вихрь, на «взрываться» — взрыв линий
    dict(line="burst", lead=0.35, cut_word="взрываться",
         shots=[dict(video="B", start=0.4, end=4.6), dict(video="B", start=5.0, end=7.0, fill=True)],
         trans=("fadewhite", 0.3), audio="B"),
    # подписи «Скорость → ∞ / Энергия конечна» читаются, затем наезд на нить (зона без надписей)
    dict(line="vortex", lead=0.15, tail=0.5,
         shots=[dict(img=5, keys="dive", sharpen=True, target=(3.0, 0.5, 0.30), dive_last=2.4)], trans=("fade", 0.3)),
    dict(line="agents", lead=0.15, shots=[dict(video="B", start=7.0, end=8.3),
                                          dict(img=6, keys="dive", target=(2.3, 0.5, 0.465), dive_last=1.0)],
         trans=("circleopen", 0.4), audio="B"),
    dict(line="lean", lead=0.15, shots=[dict(video="B", start=8.3, end=10.6),
                                        dict(img=7, zoom=(1.0, 1.12, (0.5, 0.5), (0.5, 0.53)))],
         trans=("fade", 0.3), audio="B"),
    dict(line="breakthrough", lead=0.2, tail=0.2, shots=[dict(video="B", start=14.6, end=17.6, fill=True)],
         trans=("fadewhite", 0.4), audio="B"),
    dict(line="final", lead=0.3, tail=1.0, shots=[dict(img=8, zoom=(1.0, 1.10, (0.5, 0.5), (0.5, 0.56)))],
         trans=None),
]
INNER = ("fade", 0.15)  # переход между кадрами внутри сцены


def clean_voice_sample() -> Path:
    """Голос из ролика B без музыки (Demucs) → моно 24 кГц без тишины по краям."""
    sample = CACHE / "sample_voice.wav"
    if sample.exists():
        return sample
    sep = WORK / "demucs"
    full = WORK / "b_audio.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(VIDEO_B), "-vn", "-ac", "2", "-ar", "44100", str(full)])
    subprocess.run([CV_PYTHON, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", str(sep), str(full)],
                   check=True)
    vocals = next(sep.rglob("vocals.wav"))
    shutil.copy(next(sep.rglob("no_vocals.wav")), CACHE / "b_music.wav")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(vocals), "-ac", "1", "-ar", "24000",
         "-af", "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                "silenceremove=start_periods=1:start_threshold=-45dB,areverse,loudnorm=I=-18",
         str(sample)])
    return sample


def synth_voice(lines: dict[str, str]) -> dict[str, Path]:
    sample = clean_voice_sample()
    lines_file = WORK / "lines.json"
    lines_file.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=f"{CV_ROOT}:{CV_ROOT / 'third_party' / 'Matcha-TTS'}")
    subprocess.run([CV_PYTHON, str(ROOT / "tools" / "clone_voice.py"), "--model", CV_MODEL, "--sample", str(sample),
                    "--prompt", SAMPLE_TEXT, "--lines", str(lines_file), "--out", str(CACHE)], check=True, env=env)
    manifest = json.loads((CACHE / "manifest.json").read_text(encoding="utf-8"))
    return {key: Path(manifest[key]) for key in lines}


def video_shot(shot: dict, duration: float, out: Path) -> None:
    """Фрагмент видео Gemini → 1080×1920@30; если фраза длиннее — плавно замедляем (до 1.5×), дальше стоп-кадр."""
    src = VIDEO_A if shot["video"] == "A" else VIDEO_B
    avail = shot["end"] - shot["start"]
    slow = min(1.5, max(1.0, duration / avail))
    vf = (f"setpts={slow:.4f}*PTS,scale={W}:{H}:flags=lanczos,fps={FPS},"
          f"tpad=stop_mode=clone:stop_duration={duration:.2f},setsar=1,format=yuv420p")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{shot['start']:.3f}", "-t", f"{avail:.3f}", "-i", str(src),
         "-vf", vf, "-frames:v", str(max(1, round(duration * FPS))), "-an",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-r", str(FPS), str(out)])
    shot["slow"] = slow


def dive_keys(duration: float, target: tuple, dive_last: float) -> list[tuple]:
    """Кадр почти стоит (читаются надписи, пока диктор их произносит), наезд — последние `dive_last` секунд."""
    z, cx, cy = target
    hold = max(0.5, duration - dive_last)
    return [(0, 1.0, 0.5, 0.5), (hold, 1.03, 0.5, 0.5), (duration + 0.4, z, cx, cy)]


def main() -> None:
    cfg = json.loads((ROOT / "tools" / "proj001_v2_voice.json").read_text(encoding="utf-8"))
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    voices = synth_voice(cfg["lines"])

    # 1. Раскладка: сцена = lead + речь + tail; кадры внутри сцены
    t = 0.0
    shots, voice_parts = [], []
    for s in SCENES:
        path = voices[s["line"]]
        dur_audio = probe(path)["duration"]
        begin, end = tts.speech_bounds(path, dur_audio)
        lead, tail = s.get("lead", LEAD), s.get("tail", TAIL)
        dur = lead + (end - begin) + tail
        if s["shots"][0].get("video") == "A":  # заставка: титр должен успеть прочитаться
            dur = max(dur, 5.6)
        s.update(start=t, dur=dur)
        voice_parts.append((path, begin, end, t + lead))

        # длины кадров внутри сцены
        rest = dur
        lens = []
        if s.get("cut_word"):
            words = tts.proportional_words(cfg["lines"][s["line"]], begin, end)
            w = next(w for w in words if s["cut_word"] in w["text"])
            first = lead + (w["start"] - begin) - 0.05
            lens = [first, dur - first]
        else:
            fixed = [sh["end"] - sh["start"] if "video" in sh and not sh.get("fill") else None for sh in s["shots"]]
            free = dur - sum(f for f in fixed if f)
            n_free = sum(1 for f in fixed if f is None)
            lens = [f if f else max(1.0, free / n_free) for f in fixed]
            rest = dur - sum(lens[:-1])
            lens[-1] = rest
        for sh, length in zip(s["shots"], lens):
            shots.append(dict(sh, scene=s, dur=length))
        print(f"  {s['line']:12s} {t:6.2f} → {t + dur:6.2f}  кадры: {[round(x, 2) for x in lens]}")
        t += dur
    total = t

    # 2. Переходы: между сценами — по сцене, внутри сцены — короткий наплыв
    starts, acc = [], 0.0
    for sh in shots:
        starts.append(acc)
        acc += sh["dur"]
    trans = []
    for i, sh in enumerate(shots[:-1]):
        nxt = shots[i + 1]
        trans.append(sh["scene"]["trans"] if nxt["scene"] is not sh["scene"] else INNER)

    clips = []
    for i, sh in enumerate(shots):
        d_in = trans[i - 1][1] / 2 if i else 0
        d_out = trans[i][1] / 2 if i < len(trans) else 0
        length = sh["dur"] + d_in + d_out
        clip = WORK / f"shot{i:02d}.mp4"
        if "video" in sh:
            video_shot(sh, length, clip)
        else:
            spec = dict(sh)
            if sh.get("keys") == "dive":
                spec["keys"] = dive_keys(sh["dur"], sh["target"], sh["dive_last"])
            scene_clip(spec, length, clip, shift=d_in)
        clips.append(clip)

    inputs = sum((["-i", str(c)] for c in clips), [])
    graph, prev = [], "[0:v]"
    for i, (name, d) in enumerate(trans):
        label = f"[x{i}]"
        graph.append(f"{prev}[{i + 1}:v]xfade=transition={name}:duration={d}:offset={starts[i + 1] - d / 2:.3f}{label}")
        prev = label
    video = WORK / "video.mp4"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", prev,
         "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(video)])

    # 3. Голос
    v_inputs, v_graph = [], []
    for i, (path, begin, end, at) in enumerate(voice_parts):
        v_inputs += ["-i", str(path)]
        v_graph.append(f"[{i}:a]atrim={begin:.3f}:{end + 0.05:.3f},asetpts=PTS-STARTPTS,aresample=44100,"
                       f"aformat=channel_layouts=stereo,adelay={int(at * 1000)}:all=1[v{i}]")
    n = len(voice_parts)
    voice = WORK / "voice.wav"
    run(["ffmpeg", "-y", "-v", "error", *v_inputs, "-filter_complex",
         ";".join(v_graph) + ";" + "".join(f"[v{i}]" for i in range(n)) +
         f"amix=inputs={n}:normalize=0,apad=whole_dur={total:.3f}[out]", "-map", "[out]",
         "-t", f"{total:.3f}", str(voice)])

    # 4. Фон: заставка со своей музыкой, родные звуки роликов под их кадрами, подложка — везде после заставки
    intro = SCENES[0]
    burst = next(s for s in SCENES if s["line"] == "burst")
    vortex = next(s for s in SCENES if s["line"] == "vortex")
    bed = WORK / "bed.wav"
    music_bed(total, [s["start"] for s in SCENES[1:4]], (burst["start"], vortex["start"] + vortex["dur"]), bed)
    layers = [f"[0:a]volume='if(lt(t,{intro['dur'] - 1.2:.2f}),0,min(1,(t-{intro['dur'] - 1.2:.2f})/1.2))'"
              f":eval=frame[bed]"]
    inputs = ["-i", str(bed), "-i", str(VIDEO_A)]
    layers.append(f"[1:a]atrim=0:{intro['dur'] + 0.6:.2f},afade=t=out:st={intro['dur'] - 0.6:.2f}:d=1.2,"
                  f"aresample=44100,aformat=channel_layouts=stereo,volume=0.9[a_intro]")
    labels = ["[bed]", "[a_intro]"]
    b_music = CACHE / "b_music.wav"
    k = 2
    for sh, st in zip(shots, starts):
        if sh.get("video") != "B":
            continue
        length = sh["dur"]
        inputs += ["-i", str(b_music)]
        layers.append(f"[{k}:a]atrim={sh['start']:.2f}:{sh['start'] + length / sh.get('slow', 1):.2f},"
                      f"asetpts=PTS-STARTPTS,atempo={1 / sh.get('slow', 1):.4f},afade=t=in:d=0.15,"
                      f"afade=t=out:st={max(0, length - 0.3):.2f}:d=0.3,volume=0.55,"
                      f"adelay={int(st * 1000)}:all=1[b{k}]")
        labels.append(f"[b{k}]")
        k += 1
    background = WORK / "background.wav"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex",
         ";".join(layers) + ";" + "".join(labels) + f"amix=inputs={len(labels)}:normalize=0[out]",
         "-map", "[out]", "-t", f"{total:.3f}", "-ar", "44100", "-ac", "2", str(background)])

    mix = WORK / "mix.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(voice), "-i", str(background), "-filter_complex",
         "[0:a]asplit=2[vo][key];[1:a][key]sidechaincompress=threshold=0.03:ratio=5:attack=20:release=400[duck];"
         "[vo][duck]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[out]",
         "-map", "[out]", "-ar", "44100", str(mix)])

    OUT.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(mix), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
         str(OUT / "reel_v2.mp4")])
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"Готово: {OUT / 'reel_v2.mp4'} ({total:.1f} c)")


if __name__ == "__main__":
    main()
