"""Хранилище проектов: JSON-файл + папки с материалами и готовыми рилсами.

data/projects/<project_id>/
    project.json
    materials/<material_id>.<ext>
    thumbs/<material_id>.jpg
    reels/<reel_id>/reel.mp4, cover.jpg, work/...
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def projects_root() -> Path:
    root = config.DATA_DIR / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_dir(project_id: str) -> Path:
    if not project_id.isalnum():
        raise KeyError(project_id)
    return projects_root() / project_id


def _project_file(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def _write(project: dict) -> None:
    path = _project_file(project["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_projects() -> list[dict]:
    with _lock:
        items = []
        for path in projects_root().glob("*/project.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(items, key=lambda p: p["created_at"], reverse=True)


def get_project(project_id: str) -> dict:
    with _lock:
        path = _project_file(project_id)
        if not path.exists():
            raise KeyError(project_id)
        return json.loads(path.read_text(encoding="utf-8"))


def create_project(title: str, brief: dict[str, Any]) -> dict:
    with _lock:
        project = {
            "id": new_id(),
            "title": title.strip() or "Без названия",
            "brief": brief,
            "created_at": now_iso(),
            "materials": [],
            "reels": [],
        }
        d = project_dir(project["id"])
        for sub in ("materials", "thumbs", "reels"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        _write(project)
        return project


def update_project(project_id: str, mutate: Callable[[dict], Any]) -> dict:
    """Атомарно изменить проект: mutate получает dict и меняет его на месте."""
    with _lock:
        project = get_project(project_id)
        mutate(project)
        _write(project)
        return project


def delete_project(project_id: str) -> None:
    with _lock:
        d = project_dir(project_id)
        if not d.exists():
            raise KeyError(project_id)
        shutil.rmtree(d)


def find(items: list[dict], item_id: str) -> dict:
    for item in items:
        if item["id"] == item_id:
            return item
    raise KeyError(item_id)


def update_reel(project_id: str, reel_id: str, **fields: Any) -> dict:
    def mutate(p: dict) -> None:
        find(p["reels"], reel_id).update(fields)

    return update_project(project_id, mutate)
