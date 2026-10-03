"""Веб-сервер: материалы → сценарий от Claude → готовый рилс."""
from __future__ import annotations

import base64
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, config, jobs, media, passwords, storage, tts

STATIC_DIR = Path(__file__).resolve().parent / "static"



@asynccontextmanager
async def lifespan(_: FastAPI):
    recover_interrupted()
    yield


app = FastAPI(title="Reels Studio", lifespan=lifespan)


# ---------- доступ по паролю ----------

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if (config.APP_PASSWORD or config.APP_PASSWORD_HASH) and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                if config.APP_PASSWORD:
                    good = secrets.compare_digest(password, config.APP_PASSWORD)
                else:
                    good = passwords.verify_password(password, config.APP_PASSWORD_HASH)
                ok = secrets.compare_digest(user, config.APP_USER) and good
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Reels Studio"'})
    return await call_next(request)


def recover_interrupted() -> None:
    """Рилсы, которые собирались в момент перезапуска сервера, помечаем ошибкой."""
    for project in storage.list_projects():
        for reel in project["reels"]:
            if reel.get("status") in ("queued", "scripting", "rendering"):
                storage.update_reel(project["id"], reel["id"], status="error",
                                    error="Сборка прервана перезапуском сервера — запустите ещё раз.")


# ---------- модели запросов ----------

class Brief(BaseModel):
    topic: str = ""
    goal: str = ""
    audience: str = ""
    tone: str = ""
    cta: str = ""


class ProjectIn(BaseModel):
    title: str = Field("", max_length=200)
    brief: Brief = Brief()


class ProjectPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    brief: Optional[Brief] = None


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    name: str = ""


class MaterialPatch(BaseModel):
    note: Optional[str] = Field(None, max_length=1000)
    text: Optional[str] = Field(None, max_length=20000)
    name: Optional[str] = Field(None, max_length=200)


class VoiceOptions(BaseModel):
    voiceover: bool = False
    tts_provider: str = Field("", max_length=40)
    tts_voice: str = Field("", max_length=120)
    tts_speed: float = Field(1.0, ge=0.5, le=2.0)
    # Подача голоса (только свой TTS): None — из пресета голоса, "" — без инструкции.
    tts_instruct: Optional[str] = Field(None, max_length=300)
    karaoke: bool = True


class ReelIn(VoiceOptions):
    duration: int = Field(30, ge=7, le=90)
    wishes: str = Field("", max_length=2000)
    music_id: str = ""


class Scene(BaseModel):
    material_id: str = ""
    text: str = Field("", max_length=300)
    voice: str = Field("", max_length=600)
    duration: float = Field(3, ge=0.5, le=30)
    start: float = Field(0, ge=0)


class ScriptIn(BaseModel):
    title: str = ""
    cover_text: str = ""
    scenes: list[Scene] = Field(..., min_length=1, max_length=30)
    caption: str = ""
    hashtags: list[str] = []
    voice_options: Optional[VoiceOptions] = None


class PreviewIn(BaseModel):
    provider: str = Field(..., max_length=40)
    voice: str = Field("", max_length=120)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    instruct: Optional[str] = Field(None, max_length=300)
    text: str = Field("Привет! Так будет звучать озвучка вашего рилса.", min_length=1, max_length=300)


# ---------- helpers ----------

def _project_or_404(project_id: str) -> dict:
    try:
        return storage.get_project(project_id)
    except KeyError:
        raise HTTPException(404, "Проект не найден")


def _check_voice(opts: VoiceOptions) -> None:
    if not opts.voiceover:
        return
    provider = tts.PROVIDERS.get(opts.tts_provider or tts.default_provider())
    if provider is None:
        raise HTTPException(400, "Неизвестный провайдер озвучки")
    if not provider.available():
        raise HTTPException(400, f"Провайдер «{provider.name}» не настроен — добавьте ключ в .env")
    try:
        tts.check_instruct(opts.tts_instruct)
    except tts.TTSError as exc:
        raise HTTPException(400, str(exc))


def _item_or_404(items: list[dict], item_id: str, what: str) -> dict:
    try:
        return storage.find(items, item_id)
    except KeyError:
        raise HTTPException(404, f"{what} не найден")


# ---------- API ----------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/status")
def status() -> dict:
    return {"ai": ai.ai_available(), "model": config.ANTHROPIC_MODEL, "n8n": bool(config.N8N_WEBHOOK_URL)}


@app.get("/api/tts")
def tts_providers() -> dict:
    return tts.describe()


@app.post("/api/tts/preview")
def tts_preview(body: PreviewIn) -> FileResponse:
    """Прослушать голос до сборки рилса."""
    try:
        speech = tts.synthesize(body.text, body.provider, body.voice, body.speed, body.instruct)
    except tts.TTSError as exc:
        raise HTTPException(400, str(exc))
    # Пояснение о запасном голосе — в заголовке (URL-кодировано: в заголовках только ASCII)
    headers = {"X-TTS-Fallback": quote(speech.fallback)} if speech.fallback else None
    return FileResponse(speech.path, media_type="audio/mpeg" if speech.path.suffix == ".mp3" else "audio/wav",
                        headers=headers)


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return [
        {
            "id": p["id"],
            "title": p["title"],
            "created_at": p["created_at"],
            "materials": len(p["materials"]),
            "reels": len(p["reels"]),
            "cover": next((f"/api/projects/{p['id']}/reels/{r['id']}/cover?v={r.get('version', 0)}"
                           for r in reversed(p["reels"]) if r.get("status") == "done"), None),
        }
        for p in storage.list_projects()
    ]


@app.post("/api/projects")
def create_project(body: ProjectIn) -> dict:
    return storage.create_project(body.title, body.brief.model_dump())


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    return _project_or_404(project_id)


@app.patch("/api/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch) -> dict:
    _project_or_404(project_id)

    def mutate(p: dict) -> None:
        if body.title is not None:
            p["title"] = body.title.strip() or p["title"]
        if body.brief is not None:
            p["brief"] = body.brief.model_dump()

    return storage.update_project(project_id, mutate)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    try:
        storage.delete_project(project_id)
    except KeyError:
        raise HTTPException(404, "Проект не найден")
    return {"ok": True}


@app.post("/api/projects/{project_id}/materials")
async def upload_materials(project_id: str, files: list[UploadFile] = File(...), note: str = Form("")) -> dict:
    _project_or_404(project_id)
    pdir = storage.project_dir(project_id)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    added, skipped = [], []
    for upload in files:
        name = Path(upload.filename or "file").name
        kind = media.detect_kind(name)
        if not kind:
            skipped.append(f"{name}: неподдерживаемый формат")
            continue
        mid = storage.new_id()
        ext = Path(name).suffix.lower()
        dst = pdir / "materials" / f"{mid}{ext}"
        size = 0
        with dst.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    break
                out.write(chunk)
        if size > limit:
            dst.unlink(missing_ok=True)
            skipped.append(f"{name}: больше {config.MAX_UPLOAD_MB} МБ")
            continue

        material = {"id": mid, "kind": kind, "name": name, "file": dst.name, "note": note.strip(),
                    "size": size, "created_at": storage.now_iso()}
        try:
            if kind == "text":
                material["text"] = dst.read_text(encoding="utf-8", errors="replace")[:20000]
            if kind in ("video", "audio"):
                material.update(media.probe(dst))
            if kind == "image":
                img = media.open_image(dst)
                material["width"], material["height"] = img.size
        except Exception as exc:
            dst.unlink(missing_ok=True)
            skipped.append(f"{name}: не удалось прочитать файл ({exc})")
            continue
        material["has_thumb"] = media.make_thumbnail(kind, dst, pdir / "thumbs" / f"{mid}.jpg")
        added.append(material)

    storage.update_project(project_id, lambda p: p["materials"].extend(added))
    return {"added": added, "skipped": skipped}


@app.post("/api/projects/{project_id}/notes")
def add_note(project_id: str, body: NoteIn) -> dict:
    _project_or_404(project_id)
    material = {"id": storage.new_id(), "kind": "text", "name": body.name.strip() or "Заметка",
                "text": body.text.strip(), "note": "", "created_at": storage.now_iso()}
    storage.update_project(project_id, lambda p: p["materials"].append(material))
    return material


@app.patch("/api/projects/{project_id}/materials/{material_id}")
def patch_material(project_id: str, material_id: str, body: MaterialPatch) -> dict:
    project = _project_or_404(project_id)
    _item_or_404(project["materials"], material_id, "Материал")

    def mutate(p: dict) -> None:
        m = storage.find(p["materials"], material_id)
        for key, value in body.model_dump(exclude_none=True).items():
            m[key] = value.strip()

    project = storage.update_project(project_id, mutate)
    return storage.find(project["materials"], material_id)


@app.delete("/api/projects/{project_id}/materials/{material_id}")
def delete_material(project_id: str, material_id: str) -> dict:
    project = _project_or_404(project_id)
    material = _item_or_404(project["materials"], material_id, "Материал")
    storage.update_project(project_id, lambda p: p["materials"].remove(storage.find(p["materials"], material_id)))
    pdir = storage.project_dir(project_id)
    if material.get("file"):
        (pdir / "materials" / material["file"]).unlink(missing_ok=True)
    (pdir / "thumbs" / f"{material_id}.jpg").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/projects/{project_id}/materials/{material_id}/file")
def material_file(project_id: str, material_id: str) -> FileResponse:
    project = _project_or_404(project_id)
    material = _item_or_404(project["materials"], material_id, "Материал")
    if not material.get("file"):
        raise HTTPException(404, "У материала нет файла")
    return FileResponse(storage.project_dir(project_id) / "materials" / material["file"], filename=material["name"])


@app.get("/api/projects/{project_id}/materials/{material_id}/thumb")
def material_thumb(project_id: str, material_id: str) -> FileResponse:
    project = _project_or_404(project_id)
    _item_or_404(project["materials"], material_id, "Материал")
    path = storage.project_dir(project_id) / "thumbs" / f"{material_id}.jpg"
    if not path.exists():
        raise HTTPException(404, "Нет превью")
    return FileResponse(path)


@app.post("/api/projects/{project_id}/reels")
def create_reel(project_id: str, body: ReelIn) -> dict:
    project = _project_or_404(project_id)
    _check_voice(body)
    if not any(m["kind"] in ("image", "video", "text") for m in project["materials"]):
        raise HTTPException(400, "Сначала добавьте материалы: фото, видео или текст")
    reel = {"id": storage.new_id(), "status": "queued", "progress": 0, "stage": "В очереди",
            "options": body.model_dump(), "script": None, "created_at": storage.now_iso(), "version": 0}
    storage.update_project(project_id, lambda p: p["reels"].append(reel))
    jobs.submit(project_id, reel["id"], with_script=True)
    return reel


@app.put("/api/projects/{project_id}/reels/{reel_id}/script")
def update_script(project_id: str, reel_id: str, body: ScriptIn) -> dict:
    """Сохранить отредактированный сценарий и пересобрать видео (без повторного запроса к Claude)."""
    project = _project_or_404(project_id)
    reel = _item_or_404(project["reels"], reel_id, "Рилс")
    if reel.get("status") in ("queued", "scripting", "rendering"):
        raise HTTPException(409, "Рилс ещё собирается")
    script = ai.normalize_script(body.model_dump(), project)
    options = dict(reel.get("options") or {})
    if body.voice_options is not None:
        _check_voice(body.voice_options)
        options.update(body.voice_options.model_dump())
    storage.update_reel(project_id, reel_id, script=script, options=options,
                        status="queued", stage="В очереди", progress=0, error=None)
    jobs.submit(project_id, reel_id, with_script=False)
    return storage.find(storage.get_project(project_id)["reels"], reel_id)


@app.post("/api/projects/{project_id}/reels/{reel_id}/regenerate")
def regenerate(project_id: str, reel_id: str) -> dict:
    """Попросить Claude написать новый сценарий для этого рилса."""
    project = _project_or_404(project_id)
    reel = _item_or_404(project["reels"], reel_id, "Рилс")
    if reel.get("status") in ("queued", "scripting", "rendering"):
        raise HTTPException(409, "Рилс ещё собирается")
    storage.update_reel(project_id, reel_id, status="queued", stage="В очереди", progress=0, error=None)
    jobs.submit(project_id, reel_id, with_script=True)
    return storage.find(storage.get_project(project_id)["reels"], reel_id)


@app.delete("/api/projects/{project_id}/reels/{reel_id}")
def delete_reel(project_id: str, reel_id: str) -> dict:
    project = _project_or_404(project_id)
    reel = _item_or_404(project["reels"], reel_id, "Рилс")
    if reel.get("status") in ("scripting", "rendering"):
        raise HTTPException(409, "Рилс ещё собирается — дождитесь окончания")
    storage.update_project(project_id, lambda p: p["reels"].remove(storage.find(p["reels"], reel_id)))
    shutil.rmtree(storage.project_dir(project_id) / "reels" / reel_id, ignore_errors=True)
    return {"ok": True}


def _reel_file(project_id: str, reel_id: str, name: str) -> Path:
    project = _project_or_404(project_id)
    _item_or_404(project["reels"], reel_id, "Рилс")
    path = storage.project_dir(project_id) / "reels" / reel_id / name
    if not path.exists():
        raise HTTPException(404, "Файл ещё не готов")
    return path


@app.get("/api/projects/{project_id}/reels/{reel_id}/video")
def reel_video(project_id: str, reel_id: str, download: bool = False) -> FileResponse:
    path = _reel_file(project_id, reel_id, "reel.mp4")
    return FileResponse(path, media_type="video/mp4", filename=f"reel_{reel_id}.mp4" if download else None)


@app.get("/api/projects/{project_id}/reels/{reel_id}/cover")
def reel_cover(project_id: str, reel_id: str, download: bool = False) -> FileResponse:
    path = _reel_file(project_id, reel_id, "cover.jpg")
    return FileResponse(path, media_type="image/jpeg", filename=f"cover_{reel_id}.jpg" if download else None)


@app.exception_handler(ValueError)
def value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
