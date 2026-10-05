"""Озвучка голосом-образцом через Fun-CosyVoice3 (zero-shot клонирование) — локально, без сервера.

Вызов модели повторяет tts/app/synth.py: маркер <|endofprompt|> в затравке и выключенный
нормализатор текста (он не знает кириллицу). Запускать в окружении с CosyVoice:

    PYTHONPATH=cosyvoice:cosyvoice/third_party/Matcha-TTS \
    python tools/clone_voice.py --model MODEL_DIR --sample voice.wav --prompt "расшифровка образца" \
        --lines lines.json --out OUT_DIR

lines.json — {"ключ": "текст", ...}; результат — OUT_DIR/<ключ>_<хеш>.wav (24 кГц) и OUT_DIR/manifest.json
с путями. Уже готовые файлы не пересинтезируются, поэтому правка одной фразы пересобирает только её.

Паузы: с выключенным нормализатором вся реплика уходит в модель одним куском, и предложения
сливаются (особенно если образец сам говорит быстро). Поэтому реплика режется на предложения,
каждое синтезируется отдельно, тишина по краям обрезается, между ними ставится пауза --gap
(после «?» и «!» — чуть длиннее). Совсем короткие предложения («Нет!») приклеиваются к следующему:
на слишком коротком тексте модель звучит хуже.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

MIN_SENTENCE = 12  # короче — приклеиваем к следующему предложению
ENDOFPROMPT = "<|endofprompt|>"
SYSTEM_PREFIX = "You are a helpful assistant."


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    merged: list[str] = []
    carry = ""
    for part in parts:
        part = f"{carry} {part}".strip() if carry else part
        if len(part) < MIN_SENTENCE and part is not parts[-1]:
            carry = part
            continue
        merged.append(part)
        carry = ""
    if carry:
        merged.append(carry)
    return merged


def trim(audio, sr: int, threshold_db: float = -42.0, margin: float = 0.04):
    """Обрезать тишину по краям куска (порог относительно пика)."""
    import torch

    frame = int(sr * 0.01)
    mono = audio.abs().mean(dim=0)
    n = mono.shape[0] // frame
    if n == 0:
        return audio
    energy = mono[: n * frame].reshape(n, frame).amax(dim=1)
    limit = energy.max() * (10 ** (threshold_db / 20))
    loud = torch.nonzero(energy > limit).flatten()
    if loud.numel() == 0:
        return audio
    start = max(0, int(loud[0]) * frame - int(margin * sr))
    end = min(audio.shape[1], (int(loud[-1]) + 1) * frame + int(margin * sr))
    return audio[:, start:end]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--lines", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1986)
    ap.add_argument("--gap", type=float, default=0.3, help="пауза между предложениями, с")
    args = ap.parse_args()

    import torch
    import torchaudio
    from cosyvoice.cli.cosyvoice import AutoModel
    from cosyvoice.utils.common import set_all_random_seed

    torch.set_num_threads(max(1, torch.get_num_threads()))
    lines = json.loads(Path(args.lines).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model = AutoModel(model_dir=args.model, fp16=False)
    prompt = SYSTEM_PREFIX + ENDOFPROMPT + args.prompt

    manifest = {}
    sr = model.sample_rate
    for key, text in lines.items():
        # имя файла зависит от текста и настроек — правка фразы даёт новый файл, старый не мешает
        digest = hashlib.sha1(f"{text}|{args.speed}|{args.seed}|{args.gap}|split".encode()).hexdigest()[:8]
        target = out / f"{key}_{digest}.wav"
        manifest[key] = str(target)
        if target.exists():
            print(f"{key}: cached", flush=True)
            continue
        started = time.time()
        pieces, last_char = [], "."
        for i, sentence in enumerate(sentences(text)):
            set_all_random_seed(args.seed + i)  # повторяемый результат
            chunks = model.inference_zero_shot(sentence, prompt, args.sample, stream=False,
                                               speed=args.speed, text_frontend=False)
            piece = trim(torch.cat([c["tts_speech"] for c in chunks], dim=1), sr)
            if pieces:
                pause = args.gap * (1.25 if last_char in "!?" else 1.0)
                pieces.append(torch.zeros(1, int(pause * sr)))
            pieces.append(piece)
            last_char = sentence.rstrip()[-1:] or "."
        audio = torch.cat(pieces, dim=1)
        torchaudio.save(str(target), audio, sr)
        print(f"{key}: {audio.shape[1] / sr:.2f}s audio, {len(sentences(text))} sentences, "
              f"{time.time() - started:.0f}s", flush=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("CLONE_OK", flush=True)


if __name__ == "__main__":
    main()
