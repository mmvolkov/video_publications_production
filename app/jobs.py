"""Фоновые задачи: сценарий от Claude → рендер → уведомление в n8n."""
from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import ai, config, render, storage, tts

log = logging.getLogger("reels")
# Рендер грузит процессор, поэтому собираем рилсы по одному.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reel")


def submit(project_id: str, reel_id: str, with_script: bool) -> None:
    _executor.submit(_run, project_id, reel_id, with_script)


def _run(project_id: str, reel_id: str, with_script: bool) -> None:
    try:
        project = storage.get_project(project_id)
        reel = storage.find(project["reels"], reel_id)
        if with_script:
            storage.update_reel(project_id, reel_id, status="scripting", progress=0.02,
                                stage="Claude пишет сценарий", error=None)
            script, source = ai.generate_script(project, reel.get("options") or {})
            storage.update_reel(project_id, reel_id, script=script, script_source=source)
            project = storage.get_project(project_id)
            reel = storage.find(project["reels"], reel_id)
        render_reel(project, reel)
        project = storage.get_project(project_id)
        notify_n8n(project, storage.find(project["reels"], reel_id))
    except Exception as exc:  # статус ошибки показываем в интерфейсе
        log.error("Reel %s/%s failed: %s", project_id, reel_id, traceback.format_exc())
        try:
            storage.update_reel(project_id, reel_id, status="error", error=str(exc), stage="Ошибка")
        except KeyError:
            pass  # проект удалили во время рендера


def render_reel(project: dict, reel: dict) -> None:
    project_id, reel_id = project["id"], reel["id"]
    storage.update_reel(project_id, reel_id, status="rendering", progress=0.05, stage="Подготовка", error=None)
    pdir = storage.project_dir(project_id)
    materials = {m["id"]: m for m in project["materials"]}

    options = reel.get("options") or {}
    music_id = options.get("music_id")
    music = next((m for m in project["materials"] if m["kind"] == "audio" and (not music_id or m["id"] == music_id)), None)

    def progress(fraction: float, stage: str) -> None:
        storage.update_reel(project_id, reel_id, progress=round(fraction, 3), stage=stage)

    result = render.render_reel(
        reel["script"],
        materials,
        pdir / "materials",
        pdir / "reels" / reel_id,
        music=pdir / "materials" / music["file"] if music else None,
        progress=progress,
        voiceover=voiceover_settings(options),
        karaoke=bool(options.get("voiceover") and options.get("karaoke", True)),
    )
    # сцены могли удлиниться под озвучку — сохраняем реальные длительности в сценарий
    script = dict(reel["script"])
    script["scenes"] = [dict(scene, duration=d) for scene, d in zip(script["scenes"], result["durations"])]
    storage.update_reel(project_id, reel_id, status="done", progress=1.0, stage="Готово", script=script,
                        voice_note="; ".join(result.get("voice_notes") or []),
                        duration=result["duration"], finished_at=storage.now_iso(),
                        version=int(reel.get("version") or 0) + 1)


def voiceover_settings(options: dict) -> dict | None:
    if not options.get("voiceover"):
        return None
    return {
        "provider": options.get("tts_provider") or tts.default_provider(),
        "voice": options.get("tts_voice") or "",
        "speed": options.get("tts_speed") or 1.0,
        "instruct": options.get("tts_instruct"),
    }


def public_url(path: str) -> str:
    return f"{config.PUBLIC_BASE_URL}{path}" if config.PUBLIC_BASE_URL else path


def notify_n8n(project: dict, reel: dict) -> None:
    """Отправить готовый рилс в n8n (например, для автопубликации или отправки в Telegram)."""
    if not config.N8N_WEBHOOK_URL:
        return
    base = f"/api/projects/{project['id']}/reels/{reel['id']}"
    script = reel.get("script") or {}
    payload = {
        "event": "reel.ready",
        "project_id": project["id"],
        "project_title": project["title"],
        "reel_id": reel["id"],
        "title": script.get("title"),
        "duration": reel.get("duration"),
        "video_url": public_url(f"{base}/video"),
        "cover_url": public_url(f"{base}/cover"),
        "caption": script.get("caption", ""),
        "hashtags": script.get("hashtags", []),
        "caption_full": (script.get("caption", "") + "\n\n" + " ".join(script.get("hashtags", []))).strip(),
    }
    try:
        httpx.post(config.N8N_WEBHOOK_URL, json=payload, timeout=15)
    except httpx.HTTPError as exc:
        log.warning("n8n webhook failed: %s", exc)
