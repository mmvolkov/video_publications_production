import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ai, config, main, storage


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="session")
def media_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    ff = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi"]
    subprocess.run([*ff, "-i", "testsrc2=size=640x360:rate=25", "-t", "2", str(d / "clip.mp4")], check=True)
    subprocess.run([*ff, "-i", "mandelbrot=size=600x800", "-frames:v", "1", str(d / "photo.jpg")], check=True)
    subprocess.run([*ff, "-i", "sine=frequency=330:duration=3", str(d / "music.mp3")], check=True)
    return d


def wait_reel(project_id, reel_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        reel = storage.find(storage.get_project(project_id)["reels"], reel_id)
        if reel["status"] in ("done", "error"):
            return reel
        time.sleep(0.5)
    raise TimeoutError("reel not finished")


def upload(client, pid, media_files, *names, note=""):
    files = [("files", (n, (media_files / n).read_bytes())) for n in names]
    r = client.post(f"/api/projects/{pid}/materials", files=files, data={"note": note})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow_without_ai(client, media_files):
    assert client.get("/api/status").json()["ai"] is False
    p = client.post("/api/projects", json={"title": "Кофейня", "brief": {"topic": "Новая кофейня у метро", "cta": "Приходите за первым капучино"}}).json()
    pid = p["id"]

    added = upload(client, pid, media_files, "photo.jpg", "clip.mp4", "music.mp3")["added"]
    assert [m["kind"] for m in added] == ["image", "video", "audio"]
    assert added[0]["has_thumb"] and added[1]["has_thumb"]
    assert added[1]["duration"] == pytest.approx(2, abs=0.2)

    bad = client.post(f"/api/projects/{pid}/materials", files=[("files", ("x.exe", b"123"))]).json()
    assert bad["added"] == [] and bad["skipped"]

    client.post(f"/api/projects/{pid}/notes", json={"text": "Варим на зерне собственной обжарки. Открыты с 7 утра!"})
    client.patch(f"/api/projects/{pid}/materials/{added[0]['id']}", json={"note": "Наш бариста"})

    reel = client.post(f"/api/projects/{pid}/reels", json={"duration": 15}).json()
    reel = wait_reel(pid, reel["id"])
    assert reel["status"] == "done", reel.get("error")
    assert reel["script_source"] == "draft"
    assert reel["script"]["scenes"][1]["text"] == "Наш бариста"

    video = client.get(f"/api/projects/{pid}/reels/{reel['id']}/video")
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    assert client.get(f"/api/projects/{pid}/reels/{reel['id']}/cover").status_code == 200
    dl = client.get(f"/api/projects/{pid}/reels/{reel['id']}/video?download=1")
    assert "attachment" in dl.headers["content-disposition"]

    # правка сценария и пересборка без ИИ
    script = reel["script"]
    script["scenes"] = script["scenes"][:2] + [{"material_id": added[1]["id"], "text": "Зал", "duration": 5, "start": 0.5}]
    r = client.put(f"/api/projects/{pid}/reels/{reel['id']}/script", json=script)
    assert r.status_code == 200, r.text
    reel2 = wait_reel(pid, reel["id"])
    assert reel2["status"] == "done", reel2.get("error")
    assert reel2["version"] == 2
    assert reel2["duration"] == pytest.approx(sum(s["duration"] for s in reel2["script"]["scenes"]))

    listing = client.get("/api/projects").json()
    assert listing[0]["reels"] == 1 and listing[0]["cover"]

    assert client.delete(f"/api/projects/{pid}/reels/{reel['id']}").status_code == 200
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_reel_requires_materials(client):
    pid = client.post("/api/projects", json={"title": "Пусто"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/reels", json={}).status_code == 400


def test_claude_script_is_used(client, media_files, monkeypatch):
    pid = client.post("/api/projects", json={"title": "AI"}).json()["id"]
    photo = upload(client, pid, media_files, "photo.jpg")["added"][0]
    calls = {}

    def fake_claude(project, options):
        calls["messages"] = ai.build_messages(project, options)
        return {
            "title": "Хук", "cover_text": "Обложка",
            "scenes": [
                {"material_id": "", "text": "Стоп! Вы теряете клиентов", "duration": 2},
                {"material_id": photo["id"], "text": "Вот почему", "duration": 3},
                {"material_id": "missing", "text": "Несуществующий кадр станет карточкой", "duration": 99},
            ],
            "caption": "Подпись", "hashtags": ["маркетинг", "#reels"],
        }

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(ai, "generate_with_claude", fake_claude)
    reel = client.post(f"/api/projects/{pid}/reels", json={"duration": 15, "wishes": "дерзко"}).json()
    reel = wait_reel(pid, reel["id"])
    assert reel["status"] == "done", reel.get("error")
    assert reel["script_source"] == "claude"
    assert reel["script"]["hashtags"] == ["#маркетинг", "#reels"]
    assert reel["script"]["scenes"][2] == {"material_id": "", "text": "Несуществующий кадр станет карточкой", "voice": "", "duration": 15.0}
    content = calls["messages"][0]["content"]
    assert any(b["type"] == "image" for b in content)
    assert "дерзко" in content[0]["text"]
    assert "Озвучка: выключена" in content[0]["text"]


def test_basic_auth(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "secret")
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", auth=("admin", "wrong")).status_code == 401
    assert client.get("/api/projects", auth=("admin", "secret")).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_interrupted_reels_are_marked_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    p = storage.create_project("X", {})
    storage.update_project(p["id"], lambda d: d["reels"].append({"id": "r1", "status": "rendering"}))
    main.recover_interrupted()
    assert storage.get_project(p["id"])["reels"][0]["status"] == "error"


def test_path_traversal_is_rejected(client):
    assert client.get("/api/projects/..%2F..%2Fetc").status_code == 404
