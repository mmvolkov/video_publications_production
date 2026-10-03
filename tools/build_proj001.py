"""Рилс proj001: «ИИ решил задачу тысячелетия?» — ручная режиссура поверх готовых иллюстраций.

Входы:  in/proj001/*.png (8 кадров 9:16 с уже нарисованными заголовками), tools/proj001_voice.json
Выход:  out/proj001/reel.mp4, cover.jpg, caption.md

Запуск (нужны ключи провайдера озвучки, по умолчанию Yandex SpeechKit):
    YANDEX_API_KEY=... YANDEX_FOLDER_ID=... python tools/build_proj001.py
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import tts  # noqa: E402

SRC = ROOT / "in" / "proj001"
OUT = ROOT / "out" / "proj001"
WORK = OUT / "work"
W, H, FPS = 1080, 1920, 30
LEAD = 0.25   # пауза перед фразой в сцене
TAIL = 0.35   # пауза после фразы

# Сцены по ТЗ: кадр, фраза диктора, минимальная длительность, движение камеры, переход В следующую сцену.
# zoom: (z0, z1, (cx0, cy0), (cx1, cy1)) — центр в долях кадра, плавный разгон/торможение на всю сцену;
# keys: [(t, z, cx, cy), ...] — ключевые точки по времени сцены (для двухфазных движений).
SCENES = [
    dict(img=1, line="hook", slot=7.0, zoom=(1.0, 1.08, (0.5, 0.5), (0.5, 0.52)), punch=True,
         trans=("zoomin", 0.35)),
    dict(img=2, line="water", slot=0, zoom=(1.10, 1.10, (0.44, 0.5), (0.56, 0.5)), tail=0.08,
         trans=("smoothleft", 0.25)),
    dict(img=3, line="plane", slot=0, zoom=(1.0, 1.10, (0.5, 0.45), (0.56, 0.4)), tail=0.08,
         trans=("smoothleft", 0.25)),
    # пауза после вопроса «остаётся гладким?» — драматическое затишье перед сингулярностью
    dict(img=4, line="blood", slot=0, end_at=18.4, zoom=(1.0, 1.09, (0.5, 0.5), (0.5, 0.53)),
         trans=("fadeblack", 0.6)),
    # ТЗ: медленное приближение к оранжевой нити
    # Сначала кадр почти стоит (читаются заголовок и «скорость → ∞»), затем наезд на верх нити —
    # единственная зона без надписей (между заголовком и подписями), подписи уходят за край целиком.
    dict(img=5, line="vortex", slot=14.0, keys=[(0, 1.0, 0.5, 0.5), (4.6, 1.03, 0.5, 0.5), (14.2, 3.0, 0.5, 0.30)],
         lead=0.6, flash_word="взорваться", sharpen=True, trans=("fadewhite", 0.35)),
    # ТЗ: плавный наезд на центральный узел — после того, как прозвучит «88 часов»
    dict(img=6, line="agents", slot=6.0, keys=[(0, 1.0, 0.5, 0.5), (4.5, 1.04, 0.5, 0.49), (6.2, 2.3, 0.5, 0.465)],
         trans=("circleopen", 0.4)),
    dict(img=7, line="lean", slot=6.0, zoom=(1.0, 1.12, (0.5, 0.5), (0.5, 0.53)), trans=("fade", 0.45)),
    dict(img=8, line="final", slot=6.0, zoom=(1.0, 1.10, (0.5, 0.5), (0.5, 0.56)), tail=0.9, trans=None),
]


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{proc.stderr[-2000:]}")
    return proc.stderr


def image_path(n: int) -> Path:
    return next(p for p in SRC.glob("*.png") if re.search(rf"-{n}\.png$", p.name))


def ease(p: str) -> str:
    """Плавный разгон и торможение (cos ease-in-out) для выражений ffmpeg."""
    return f"(0.5-0.5*cos(PI*{p}))"


def keyframes(keys: list[tuple], shift: float) -> tuple[str, str, str]:
    """Кусочно-плавная интерполяция ключевых точек → выражения z, cx, cy для zoompan."""
    t = f"(on/{FPS}-{shift:.3f})"
    exprs = []
    for idx in (1, 2, 3):
        expr = str(keys[-1][idx])
        for (ta, *a), (tb, *b) in reversed(list(zip(keys, keys[1:]))):
            va, vb = a[idx - 1], b[idx - 1]
            e = ease(f"min(max(({t}-{ta})/{tb - ta},0),1)")
            expr = f"if(lt({t},{tb}),{va}+({vb}-{va})*{e},{expr})"
        exprs.append(f"({expr})")
    return exprs[0], exprs[1], exprs[2]


def scene_clip(scene: dict, duration: float, out: Path, shift: float = 0.0) -> None:
    """Кадр → клип с движением камеры (zoompan по картинке в 2× разрешении — без дрожания).

    shift — на сколько клип начинается раньше склейки (половина входящего перехода).
    """
    frames = max(1, round(duration * FPS))
    if "keys" in scene:
        z, cx, cy = keyframes(scene["keys"], shift)
        return _render_clip(scene, duration, frames, z, cx, cy, out)
    z0, z1, (cx0, cy0), (cx1, cy1) = scene["zoom"]
    p = f"min(on/{frames - 1},1)"
    e = ease(p)
    z = f"({z0}+({z1}-{z0})*{e})"
    if scene.get("punch"):  # хук: резкий «удар» 1.18 → 1.0 за полсекунды, дальше медленный наезд
        k = f"min(on/{int(FPS * 0.5)},1)"
        z = f"({z}+0.18*(1-{k})*(1-{k}))"
    cx = f"({cx0}+({cx1}-{cx0})*{e})"
    cy = f"({cy0}+({cy1}-{cy0})*{e})"
    _render_clip(scene, duration, frames, z, cx, cy, out)


def _render_clip(scene: dict, duration: float, frames: int, z: str, cx: str, cy: str, out: Path) -> None:
    x = f"max(0,min(iw-iw/zoom,{cx}*iw-iw/zoom/2))"
    y = f"max(0,min(ih-ih/zoom,{cy}*ih-ih/zoom/2))"
    vf = [f"scale={W * 2}:{H * 2}:flags=lanczos",
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}"]
    if scene.get("flash_at") is not None:  # вспышка на слове «взорваться»
        t = scene["flash_at"]
        vf.append(f"eq=eval=frame:brightness='0.35*exp(-pow((t-{t:.2f})/0.09,2))'"
                  f":saturation='1+0.25*t/{duration:.2f}'")
    if scene.get("sharpen"):  # на сильном приближении картинка мягче — чуть подчёркиваем детали
        vf.append("unsharp=5:5:0.7:5:5:0")
    vf += ["setsar=1", "format=yuv420p"]
    run(["ffmpeg", "-y", "-v", "error", "-i", str(image_path(scene["img"])), "-vf", ",".join(vf),
         "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-r", str(FPS), str(out)])


def music_bed(total: float, cuts: list[float], vortex: tuple[float, float], out: Path) -> None:
    """Синтезированная подложка: дрон, пульс (ускоряется в вихре), райзер, удары и свисты на склейках."""
    v0, v1 = vortex
    span = v1 - v0
    layers = {
        # низкий дрон A1/E2/A2 с медленным «дыханием», громче к сингулярности
        "drone": (f"(0.30*sin(2*PI*55*t)+0.18*sin(2*PI*82.41*t)+0.10*sin(2*PI*110.2*t)+0.05*sin(2*PI*164.8*t))"
                  f"*(0.65+0.35*sin(2*PI*0.12*t))*(0.55+0.45*min(1,max(0,(t-{v0 - 3})/{span})))"),
        # пульс 120 bpm до паузы
        "pulse": f"if(lt(t,{v0 - 2.2}),0.55*sin(2*PI*52*t)*exp(-14*mod(t,0.5)),0)",
        # в вихре пульс ускоряется: частота 1 → 6 Гц — «скорость уходит в бесконечность»
        "pulse_v": (f"if(between(t,{v0 + 0.8},{v1 - 0.3}),0.6*sin(2*PI*48*t)"
                    f"*exp(-10*mod((t-{v0})+pow(t-{v0},3)/({span}*{span}*0.6),1)"
                    f"/(1+5*pow((t-{v0})/{span},2))),0)"),
        # удар-«бум» на входе в вихрь и на белой вспышке
        "boom": (f"if(between(t,{v0},{v0 + 2}),0.9*sin(2*PI*(95*(t-{v0})-14*pow(t-{v0},2)))*exp(-2.2*(t-{v0})),0)"
                 f"+if(between(t,{v1},{v1 + 1.2}),0.5*sin(2*PI*(80*(t-{v1})-12*pow(t-{v1},2)))*exp(-3*(t-{v1})),0)"),
    }
    inputs, labels = [], []
    for i, (name, expr) in enumerate(layers.items()):
        inputs += ["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={total:.3f}"]
        labels.append(f"[{i}:a]")
    n = len(layers)
    # райзер: шум, нарастающий 2.2 с перед вихрем
    inputs += ["-f", "lavfi", "-i", f"anoisesrc=d={total:.3f}:c=pink:r=44100:a=0.5"]
    riser = (f"[{n}:a]bandpass=f=1800:width_type=o:w=2,"
             f"volume='if(between(t,{v0 - 2.2},{v0}),pow((t-{v0 - 2.2})/2.2,2.5),0)':eval=frame[riser]")
    # свисты на быстрых склейках
    whoosh_expr = "+".join(f"if(between(t,{c - 0.18:.2f},{c + 0.18:.2f}),sin(PI*(t-{c - 0.18:.2f})/0.36),0)"
                           for c in cuts)
    inputs += ["-f", "lavfi", "-i", f"anoisesrc=d={total:.3f}:c=white:r=44100:a=0.35"]
    whoosh = f"[{n + 1}:a]highpass=f=2500,volume='{whoosh_expr}':eval=frame[whoosh]"
    graph = (f"{riser};{whoosh};{''.join(labels)}[riser][whoosh]amix=inputs={n + 2}:normalize=0,"
             f"lowpass=f=7000,afade=t=in:d=0.3,afade=t=out:st={total - 1.2:.2f}:d=1.2,volume=0.35[bed]")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[bed]",
         "-ac", "2", "-ar", "44100", str(out)])


def main() -> None:
    cfg = json.loads((ROOT / "tools" / "proj001_voice.json").read_text(encoding="utf-8"))
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # 1. Озвучка и раскладка по времени: сцена длится не меньше слота из ТЗ и не меньше фразы
    t = 0.0
    voice_parts = []
    for s in SCENES:
        speech = tts.synthesize(cfg["lines"][s["line"]], cfg["provider"], cfg["voice"], cfg["speed"])
        begin, end = tts.speech_bounds(speech.path, speech.duration)
        lead = s.get("lead", LEAD if s["slot"] else 0.12)
        need = lead + (end - begin) + s.get("tail", TAIL)
        dur = max(s["slot"], need)
        if s.get("end_at"):
            dur = max(dur, s["end_at"] - t)
        s.update(start=t, dur=dur)
        voice_parts.append((speech.path, begin, end, t + lead))
        if s.get("flash_word"):
            word = next(w for w in speech.words if s["flash_word"] in w["text"])
            s["flash_at"] = lead + word["start"] - begin + 0.1
        print(f"  {s['line']:7s} {t:6.2f} → {t + dur:6.2f}  ({dur:.2f} c)")
        t += dur
    total = t

    # 2. Клипы сцен (с запасом на переход) и склейка xfade
    clips = []
    for i, s in enumerate(SCENES):
        d_out = s["trans"][1] if s["trans"] else 0
        d_in = SCENES[i - 1]["trans"][1] / 2 if i else 0  # клип начинается раньше склейки на полперехода
        if s.get("flash_at") is not None:
            s["flash_at"] += d_in
        clip = WORK / f"scene{i + 1}.mp4"
        scene_clip(s, s["dur"] + d_out / 2 + d_in, clip, shift=d_in)
        clips.append(clip)
    inputs = sum((["-i", str(c)] for c in clips), [])
    graph, prev = [], "[0:v]"
    for i, s in enumerate(SCENES[:-1]):
        name, d = s["trans"]
        offset = SCENES[i + 1]["start"] - d / 2
        label = f"[x{i}]"
        graph.append(f"{prev}[{i + 1}:v]xfade=transition={name}:duration={d}:offset={offset:.3f}{label}")
        prev = label
    video = WORK / "video.mp4"
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", prev,
         "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(video)])

    # 3. Голос: каждая фраза без тишины по краям, на своём месте
    v_inputs, v_graph = [], []
    for i, (path, begin, end, at) in enumerate(voice_parts):
        v_inputs += ["-i", str(path)]
        v_graph.append(f"[{i}:a]atrim={begin:.3f}:{end + 0.05:.3f},asetpts=PTS-STARTPTS,aresample=44100,"
                       f"aformat=channel_layouts=stereo,adelay={int(at * 1000)}:all=1[v{i}]")
    n = len(voice_parts)
    voice = WORK / "voice.wav"
    run(["ffmpeg", "-y", "-v", "error", *v_inputs, "-filter_complex",
         ";".join(v_graph) + ";" + "".join(f"[v{i}]" for i in range(n)) +
         f"amix=inputs={n}:normalize=0,apad=whole_dur={total:.3f}[out]",
         "-map", "[out]", "-t", f"{total:.3f}", str(voice)])

    # 4. Музыка + сведение (голос сверху, подложка приглушается под речью) + громкость для Reels
    vortex = next(s for s in SCENES if s["line"] == "vortex")
    cuts = [SCENES[i]["start"] for i in (1, 2, 3)]
    bed = WORK / "bed.wav"
    music_bed(total, cuts, (vortex["start"], vortex["start"] + vortex["dur"]), bed)
    mix = WORK / "mix.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(voice), "-i", str(bed), "-filter_complex",
         "[0:a]asplit=2[vo][key];[1:a][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[duck];"
         "[vo][duck]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[out]",
         "-map", "[out]", "-ar", "44100", str(mix)])

    OUT.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(mix), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
         str(OUT / "reel.mp4")])
    # обложка — первый кадр-иллюстрация (заголовок уже на нём)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(image_path(1)), "-vf", f"scale={W}:{H}:flags=lanczos",
         "-q:v", "2", str(OUT / "cover.jpg")])
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"Готово: {OUT / 'reel.mp4'} ({total:.1f} c)")


if __name__ == "__main__":
    main()
