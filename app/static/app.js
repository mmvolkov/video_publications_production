"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const BUSY = ["queued", "scripting", "rendering"];
const KIND_LABEL = { image: "Фото", video: "Видео", audio: "Музыка", text: "Текст" };

let state = { projects: [], project: null, pollTimer: null, editingReel: null };

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || `Ошибка ${res.status}`);
  }
  return data;
}

function toast(text, ms = 3000) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), ms);
}

async function guarded(fn) {
  try { await fn(); } catch (e) { toast(e.message, 5000); }
}

// ---------- проекты ----------

async function loadStatus() {
  const s = await api("/api/status");
  const el = $("#ai-status");
  el.textContent = s.ai ? "Claude подключён" : "Без ИИ: черновые сценарии";
  el.className = "pill " + (s.ai ? "ok" : "warn");
  el.title = s.ai ? `Модель: ${s.model}` : "Задайте ANTHROPIC_API_KEY, чтобы сценарии писал Claude";
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  const list = $("#project-list");
  list.innerHTML = state.projects.map((p) => `
    <button class="project-link ${state.project?.id === p.id ? "active" : ""}" data-id="${esc(p.id)}">
      ${p.cover ? `<img src="${esc(p.cover)}" alt="">` : `<span class="ph"></span>`}
      <span>${esc(p.title)}<small>${p.materials} матер. · ${p.reels} рилс.</small></span>
    </button>`).join("") || `<p class="muted">Пока нет проектов</p>`;
}

async function openProject(id) {
  state.project = await api(`/api/projects/${id}`);
  location.hash = id;
  $("#empty").classList.add("hidden");
  $("#project").classList.remove("hidden");
  renderProject();
  loadProjects();
}

function renderProject() {
  const p = state.project;
  $("#project-title").value = p.title;
  const form = $("#brief-form");
  for (const key of ["topic", "goal", "audience", "tone", "cta"]) form.elements[key].value = p.brief?.[key] || "";
  $("#brief-card").open = !p.brief?.topic;
  renderMaterials();
  renderReels();
  schedulePoll();
}

$("#new-project").addEventListener("click", () => guarded(async () => {
  const title = prompt("Название проекта", "Новый рилс");
  if (title === null) return;
  const p = await api("/api/projects", { method: "POST", body: { title } });
  await openProject(p.id);
}));

$("#project-list").addEventListener("click", (e) => {
  const btn = e.target.closest(".project-link");
  if (btn) guarded(() => openProject(btn.dataset.id));
});

$("#project-title").addEventListener("change", (e) => guarded(async () => {
  state.project = await api(`/api/projects/${state.project.id}`, { method: "PATCH", body: { title: e.target.value } });
  loadProjects();
}));

$("#delete-project").addEventListener("click", () => guarded(async () => {
  if (!confirm(`Удалить проект «${state.project.title}» со всеми материалами и рилсами?`)) return;
  await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
  state.project = null;
  location.hash = "";
  $("#project").classList.add("hidden");
  $("#empty").classList.remove("hidden");
  loadProjects();
}));

$("#brief-form").addEventListener("submit", (e) => guarded(async () => {
  e.preventDefault();
  const brief = Object.fromEntries(new FormData(e.target));
  state.project = await api(`/api/projects/${state.project.id}`, { method: "PATCH", body: { brief } });
  $("#brief-saved").textContent = "Сохранено ✓";
  setTimeout(() => ($("#brief-saved").textContent = ""), 2000);
}));

// ---------- материалы ----------

function renderMaterials() {
  const p = state.project;
  const base = `/api/projects/${p.id}/materials`;
  $("#materials").innerHTML = p.materials.map((m) => {
    let media;
    if (m.kind === "text") {
      media = `<div class="textbox"><span class="badge">${esc(m.name || "Текст")}</span><br>${esc((m.text || "").slice(0, 400))}<button class="del" data-del="${m.id}" title="Удалить">✕</button></div>`;
    } else {
      const inner = m.has_thumb ? `<img src="${base}/${m.id}/thumb" alt="" loading="lazy">` : `<span class="icon">${m.kind === "audio" ? "🎵" : "📄"}</span>`;
      const dur = m.duration ? ` · ${m.duration.toFixed(1)} с` : "";
      media = `<div class="media">${inner}<span class="badge">${KIND_LABEL[m.kind]}${dur}</span><button class="del" data-del="${m.id}" title="Удалить">✕</button></div>`;
    }
    const note = m.kind === "text" ? "" :
      `<input data-note="${m.id}" value="${esc(m.note)}" placeholder="${m.kind === "audio" ? esc(m.name) : "Комментарий: что на кадре"}" maxlength="1000">`;
    return `<div class="material">${media}${note}</div>`;
  }).join("") || `<p class="muted">Материалов пока нет.</p>`;

  const audio = p.materials.filter((m) => m.kind === "audio");
  $("#music-select").innerHTML = (audio.length ? "" : `<option value="">Без музыки</option>`) +
    audio.map((m) => `<option value="${m.id}">${esc(m.note || m.name)}</option>`).join("") +
    (audio.length ? `<option value="none">Без музыки</option>` : "");
}

$("#materials").addEventListener("click", (e) => {
  const id = e.target.dataset.del;
  if (!id) return;
  guarded(async () => {
    if (!confirm("Удалить материал?")) return;
    await api(`/api/projects/${state.project.id}/materials/${id}`, { method: "DELETE" });
    state.project.materials = state.project.materials.filter((m) => m.id !== id);
    renderMaterials();
  });
});

$("#materials").addEventListener("change", (e) => {
  const id = e.target.dataset.note;
  if (!id) return;
  guarded(async () => {
    const m = await api(`/api/projects/${state.project.id}/materials/${id}`, { method: "PATCH", body: { note: e.target.value } });
    Object.assign(state.project.materials.find((x) => x.id === id), m);
    toast("Комментарий сохранён");
  });
});

function uploadFiles(files) {
  if (!files.length || !state.project) return;
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const bar = $("#upload-progress");
  bar.classList.remove("hidden");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/projects/${state.project.id}/materials`);
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.firstElementChild.style.width = `${(e.loaded / e.total) * 100}%`; };
  xhr.onloadend = async () => {
    bar.classList.add("hidden");
    bar.firstElementChild.style.width = "0";
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* пусто */ }
    if (xhr.status !== 200) return toast(data.detail || `Ошибка загрузки (${xhr.status})`, 5000);
    if (data.skipped?.length) toast("Пропущено: " + data.skipped.join("; "), 6000);
    else toast(`Добавлено: ${data.added.length}`);
    await openProject(state.project.id);
  };
  xhr.send(form);
}

const dz = $("#dropzone");
$("#file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

$("#note-form").addEventListener("submit", (e) => guarded(async () => {
  e.preventDefault();
  const text = e.target.elements.text.value.trim();
  if (!text) return;
  const m = await api(`/api/projects/${state.project.id}/notes`, { method: "POST", body: { text } });
  state.project.materials.push(m);
  e.target.reset();
  renderMaterials();
}));

// ---------- рилсы ----------

$("#reel-form").addEventListener("submit", (e) => guarded(async () => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.duration = Number(data.duration);
  if (data.music_id === "none") data.music_id = "__none__";
  const reel = await api(`/api/projects/${state.project.id}/reels`, { method: "POST", body: data });
  state.project.reels.push(reel);
  renderReels();
  schedulePoll();
  toast("Рилс поставлен в работу");
}));

function reelUrl(r, what, download = false) {
  return `/api/projects/${state.project.id}/reels/${r.id}/${what}?v=${r.version || 0}${download ? "&download=1" : ""}`;
}

function renderReels() {
  const reels = [...state.project.reels].reverse();
  const container = $("#reels");
  // не перерисовываем карточки, где пользователь смотрит видео
  const playing = new Set([...container.querySelectorAll("video")].filter((v) => !v.paused).map((v) => v.dataset.key));
  container.innerHTML = reels.map((r) => {
    const s = r.script || {};
    const busy = BUSY.includes(r.status);
    const key = `${r.id}:${r.version || 0}`;
    const player = r.status === "done"
      ? `<video data-key="${key}" src="${reelUrl(r, "video")}" poster="${reelUrl(r, "cover")}" controls playsinline preload="none"></video>`
      : `<div class="placeholder">${busy
          ? `<div>${esc(r.stage || "В очереди")}</div><div class="progress"><div style="width:${Math.round((r.progress || 0) * 100)}%"></div></div>`
          : r.status === "error" ? "⚠️ Ошибка" : ""}</div>`;
    const sourceNote = r.script_source === "draft" ? " · черновой сценарий без ИИ" : "";
    return `
      <article class="reel" data-id="${r.id}">
        <div>${player}</div>
        <div>
          <h3>${esc(s.title || "Рилс")}</h3>
          <div class="meta muted">${new Date(r.created_at).toLocaleString("ru-RU")}${r.duration ? ` · ${r.duration} с` : ""}${sourceNote}</div>
          ${r.status === "error" ? `<p class="error">${esc(r.error)}</p>` : ""}
          ${s.caption ? `<div class="caption">${esc(s.caption)}<div class="tags">${esc((s.hashtags || []).join(" "))}</div></div>` : ""}
          ${s.scenes ? `<ol class="scenes-preview">${s.scenes.map((sc) => `<li>${esc(sc.text || "(без текста)")} — ${sc.duration} с</li>`).join("")}</ol>` : ""}
          <div class="actions">
            ${r.status === "done" ? `
              <a class="btn primary small" href="${reelUrl(r, "video", true)}">⬇ Видео</a>
              <a class="btn small" href="${reelUrl(r, "cover", true)}">⬇ Обложка</a>
              <button class="btn small" data-act="copy">📋 Копировать подпись</button>` : ""}
            ${s.scenes && !busy ? `<button class="btn small" data-act="edit">✏️ Редактировать сценарий</button>` : ""}
            ${!busy ? `<button class="btn small" data-act="regen">🔄 Новый вариант</button>` : ""}
            ${!busy || r.status === "queued" ? `<button class="btn small ghost danger" data-act="delete">Удалить</button>` : ""}
          </div>
        </div>
      </article>`;
  }).join("") || `<p class="muted">Здесь появятся готовые ролики.</p>`;
  for (const v of container.querySelectorAll("video")) if (playing.has(v.dataset.key)) v.play().catch(() => {});
}

$("#reels").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = btn.closest(".reel").dataset.id;
  const reel = state.project.reels.find((r) => r.id === id);
  const base = `/api/projects/${state.project.id}/reels/${id}`;
  guarded(async () => {
    switch (btn.dataset.act) {
      case "copy": {
        const s = reel.script;
        await navigator.clipboard.writeText(`${s.caption}\n\n${(s.hashtags || []).join(" ")}`.trim());
        toast("Подпись скопирована");
        break;
      }
      case "edit":
        openEditor(reel);
        break;
      case "regen":
        if (!confirm("Claude напишет новый сценарий и пересоберёт этот рилс. Продолжить?")) return;
        Object.assign(reel, await api(`${base}/regenerate`, { method: "POST" }));
        renderReels(); schedulePoll();
        break;
      case "delete":
        if (!confirm("Удалить рилс?")) return;
        await api(base, { method: "DELETE" });
        state.project.reels = state.project.reels.filter((r) => r.id !== id);
        renderReels(); loadProjects();
        break;
    }
  });
});

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!state.project?.reels.some((r) => BUSY.includes(r.status))) return;
  state.pollTimer = setTimeout(async () => {
    try {
      const fresh = await api(`/api/projects/${state.project.id}`);
      if (fresh.id !== state.project.id) return;
      const wasBusy = state.project.reels.filter((r) => BUSY.includes(r.status)).map((r) => r.id);
      state.project.reels = fresh.reels;
      renderReels();
      if (wasBusy.some((id) => fresh.reels.find((r) => r.id === id)?.status === "done")) {
        toast("Рилс готов 🎉"); loadProjects();
      }
    } catch { /* повторим */ }
    schedulePoll();
  }, 2000);
}

// ---------- редактор сценария ----------

function materialOptions(selected) {
  const visual = state.project.materials.filter((m) => m.kind === "image" || m.kind === "video");
  return `<option value="">Цветная карточка</option>` + visual.map((m) =>
    `<option value="${m.id}" ${m.id === selected ? "selected" : ""}>${KIND_LABEL[m.kind]}: ${esc(m.note || m.name)}</option>`).join("");
}

function sceneHtml(sc) {
  const thumb = sc.material_id ? `/api/projects/${state.project.id}/materials/${sc.material_id}/thumb` : "";
  return `<div class="scene">
    ${thumb ? `<img class="thumb" src="${thumb}" alt="">` : `<div class="thumb"></div>`}
    <div>
      <div class="row">
        <select data-f="material_id">${materialOptions(sc.material_id)}</select>
        <input type="number" data-f="duration" value="${sc.duration}" min="0.5" max="30" step="0.5" title="Секунд">
        <button type="button" class="btn small ghost" data-move="-1" title="Выше">↑</button>
        <button type="button" class="btn small ghost" data-move="1" title="Ниже">↓</button>
        <button type="button" class="btn small ghost danger" data-remove title="Удалить сцену">✕</button>
      </div>
      <textarea data-f="text" rows="2" maxlength="300" placeholder="Текст на экране">${esc(sc.text)}</textarea>
      <input type="hidden" data-f="start" value="${sc.start || 0}">
    </div>
  </div>`;
}

function readScenes() {
  return [...document.querySelectorAll("#scene-list .scene")].map((el) => ({
    material_id: $("[data-f=material_id]", el).value,
    duration: Number($("[data-f=duration]", el).value) || 3,
    text: $("[data-f=text]", el).value,
    start: Number($("[data-f=start]", el).value) || 0,
  }));
}

function renderScenes(scenes) {
  $("#scene-list").innerHTML = scenes.map(sceneHtml).join("");
  const total = scenes.reduce((a, s) => a + (Number(s.duration) || 0), 0);
  $("#editor-total").textContent = `Сцен: ${scenes.length} · ≈ ${total.toFixed(1)} с`;
}

function openEditor(reel) {
  state.editingReel = reel;
  const s = reel.script;
  const f = $("#editor-form");
  f.elements.cover_text.value = s.cover_text || "";
  f.elements.caption.value = s.caption || "";
  f.elements.hashtags.value = (s.hashtags || []).join(" ");
  renderScenes(s.scenes);
  $("#editor").returnValue = "";
  $("#editor").showModal();
}

$("#scene-list").addEventListener("click", (e) => {
  const scenes = readScenes();
  const idx = [...document.querySelectorAll("#scene-list .scene")].indexOf(e.target.closest(".scene"));
  if (e.target.dataset.move) {
    const to = idx + Number(e.target.dataset.move);
    if (to < 0 || to >= scenes.length) return;
    [scenes[idx], scenes[to]] = [scenes[to], scenes[idx]];
    renderScenes(scenes);
  } else if (e.target.hasAttribute("data-remove")) {
    if (scenes.length === 1) return toast("Должна остаться хотя бы одна сцена");
    scenes.splice(idx, 1);
    renderScenes(scenes);
  }
});
$("#scene-list").addEventListener("change", () => renderScenes(readScenes()));
$("#add-scene").addEventListener("click", () => renderScenes([...readScenes(), { material_id: "", text: "", duration: 3 }]));

$("#editor").addEventListener("close", () => guarded(async () => {
  if ($("#editor").returnValue !== "save" || !state.editingReel) return;
  const f = $("#editor-form");
  const body = {
    ...state.editingReel.script,
    cover_text: f.elements.cover_text.value,
    caption: f.elements.caption.value,
    hashtags: f.elements.hashtags.value.split(/[\s,]+/).filter(Boolean),
    scenes: readScenes(),
  };
  const reel = await api(`/api/projects/${state.project.id}/reels/${state.editingReel.id}/script`, { method: "PUT", body });
  Object.assign(state.editingReel, reel);
  state.editingReel = null;
  renderReels(); schedulePoll();
  toast("Пересобираю видео…");
}));

// ---------- старт ----------

(async () => {
  await guarded(loadStatus);
  await guarded(loadProjects);
  const id = location.hash.slice(1);
  if (id && state.projects.some((p) => p.id === id)) guarded(() => openProject(id));
})();
