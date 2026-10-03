"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const BUSY = ["queued", "scripting", "rendering"];
const KIND_LABEL = { image: "Фото", video: "Видео", audio: "Музыка", text: "Текст" };

let state = { projects: [], project: null, pollTimer: null, editingReel: null, tts: { providers: [], default: "" } };

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
  mountVoice($("#voice-controls"));
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

// ---------- озвучка ----------

const SPEEDS = [0.9, 1.0, 1.1, 1.2, 1.3];

function loadVoicePrefs() {
  try { return JSON.parse(localStorage.getItem("voicePrefs") || "{}"); } catch { return {}; }
}
function saveVoicePrefs(v) {
  try { localStorage.setItem("voicePrefs", JSON.stringify(v)); } catch { /* приватный режим */ }
}

function providerById(id) { return state.tts.providers.find((p) => p.id === id); }

// Подача голоса для своего TTS (CosyVoice понимает инструкции только по-английски).
const DELIVERY = [
  { id: "preset", label: "Как в пресете голоса" },
  { id: "none", label: "Без инструкции", value: "" },
  { id: "energetic", label: "Энергично, с драйвом", value: "speak energetically and enthusiastically, upbeat tone, slightly fast pace" },
  { id: "calm", label: "Спокойно и тепло", value: "speak calmly and warmly, medium pace" },
  { id: "confident", label: "Уверенно, по-деловому", value: "speak confidently and clearly, like a professional business presenter" },
  { id: "friendly", label: "Дружелюбно, как другу", value: "speak in a friendly, cheerful conversational tone, smiling" },
  { id: "intrigue", label: "Интригующе", value: "speak in an intriguing, slightly hushed tone, building suspense" },
  { id: "custom", label: "Своя инструкция (по-английски)…" },
];
const CYRILLIC = /[А-Яа-яЁё]/;

function deliveryFromInstruct(instruct) {
  if (instruct === undefined || instruct === null) return { id: "preset", custom: "" };
  const preset = DELIVERY.find((d) => d.value === instruct);
  return preset ? { id: preset.id, custom: "" } : { id: "custom", custom: instruct };
}

function mountVoice(box, opts = {}) {
  const prefs = loadVoicePrefs();
  const providerId = opts.tts_provider || prefs.tts_provider || state.tts.default;
  box.innerHTML = `
    <label class="check"><input type="checkbox" data-v="voiceover"> 🎙 Озвучить диктором</label>
    <div class="voice-row">
      <label>Провайдер<select data-v="provider">${state.tts.providers.map((p) =>
        `<option value="${p.id}" ${p.available ? "" : "disabled"}>${esc(p.name)}${p.available ? "" : " — нет ключа"}</option>`).join("")}</select></label>
      <label>Голос<select data-v="voice"></select></label>
      <label>Темп<select data-v="speed">${SPEEDS.map((x) => `<option value="${x}">${x.toFixed(1)}×</option>`).join("")}</select></label>
      <button type="button" class="btn" data-v="preview">▶ Прослушать</button>
    </div>
    <div class="voice-row delivery-row voice-extra" data-v="delivery-row">
      <label>Подача<select data-v="delivery">${DELIVERY.map((d) => `<option value="${d.id}">${esc(d.label)}</option>`).join("")}</select></label>
      <label class="span-wide" data-v="custom-wrap">Инструкция для модели
        <input data-v="custom" maxlength="300" placeholder="speak with excitement, fast pace, like a sports commentator"></label>
      <span class="muted span-wide" data-v="delivery-hint"></span>
    </div>
    <label class="check voice-extra"><input type="checkbox" data-v="karaoke"> Караоке-субтитры (подсветка слова, которое звучит)</label>
    <div class="voice-extra muted" data-v="note"></div>
    <audio data-v="audio" controls class="hidden"></audio>`;
  const q = (name) => box.querySelector(`[data-v=${name}]`);
  const fillVoices = (selected) => {
    const p = providerById(q("provider").value);
    q("voice").innerHTML = (p?.voices || []).map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("");
    if (selected && [...q("voice").options].some((o) => o.value === selected)) q("voice").value = selected;
    q("note").textContent = p?.note || "";
    q("delivery-row").classList.toggle("hidden", !p?.instruct);
    updateDelivery();
  };
  const updateDelivery = () => {
    const p = providerById(q("provider").value);
    const custom = q("delivery").value === "custom";
    q("custom-wrap").classList.toggle("hidden", !custom);
    const presetInstruct = p?.voices.find((v) => v.id === q("voice").value)?.instruct;
    let hint = "";
    if (q("delivery").value === "preset") hint = presetInstruct ? `В пресете: «${presetInstruct}»` : "У этого голоса в пресете нет инструкции";
    if (custom && CYRILLIC.test(q("custom").value)) hint = "⚠️ Пишите по-английски: русскую инструкцию модель зачитает вслух";
    q("delivery-hint").textContent = hint;
  };
  const toggle = () => box.classList.toggle("voice-off", !q("voiceover").checked);

  q("voiceover").checked = opts.voiceover ?? prefs.voiceover ?? false;
  if (providerById(providerId)?.available) q("provider").value = providerId;
  fillVoices(opts.tts_voice || prefs.tts_voice);
  q("speed").value = String(opts.tts_speed || prefs.tts_speed || 1.0);
  q("karaoke").checked = opts.karaoke ?? prefs.karaoke ?? true;
  const delivery = deliveryFromInstruct("tts_instruct" in opts ? opts.tts_instruct : prefs.tts_instruct);
  q("delivery").value = delivery.id;
  q("custom").value = delivery.custom;
  updateDelivery();
  if (!q("speed").value) q("speed").value = "1";
  toggle();

  q("voiceover").addEventListener("change", toggle);
  q("provider").addEventListener("change", () => fillVoices());
  q("voice").addEventListener("change", updateDelivery);
  q("delivery").addEventListener("change", updateDelivery);
  q("custom").addEventListener("input", updateDelivery);
  q("preview").addEventListener("click", () => guarded(async () => {
    const btn = q("preview");
    btn.disabled = true;
    btn.textContent = "Синтезирую…";
    try {
      const res = await fetch("/api/tts/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: q("provider").value, voice: q("voice").value, speed: Number(q("speed").value),
          instruct: readInstruct(box),
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Ошибка ${res.status}`);
      const fallback = res.headers.get("X-TTS-Fallback");
      if (fallback) toast(decodeURIComponent(fallback), 7000);
      const audio = q("audio");
      audio.src = URL.createObjectURL(await res.blob());
      audio.classList.remove("hidden");
      audio.play().catch(() => {});
    } finally {
      btn.disabled = false;
      btn.textContent = "▶ Прослушать";
    }
  }));
}

function readInstruct(box) {
  const q = (name) => box.querySelector(`[data-v=${name}]`);
  if (!providerById(q("provider").value)?.instruct) return null;
  const id = q("delivery").value;
  if (id === "custom") return q("custom").value.trim();
  return DELIVERY.find((d) => d.id === id)?.value ?? null;
}

function readVoice(box) {
  const q = (name) => box.querySelector(`[data-v=${name}]`);
  const v = {
    voiceover: q("voiceover").checked,
    tts_provider: q("provider").value,
    tts_voice: q("voice").value,
    tts_speed: Number(q("speed").value) || 1,
    karaoke: q("karaoke").checked,
    tts_instruct: readInstruct(box),
  };
  saveVoicePrefs(v);
  return v;
}

function voiceLabel(options) {
  if (!options?.voiceover) return "";
  const p = providerById(options.tts_provider);
  const voice = p?.voices.find((v) => v.id === options.tts_voice)?.name || options.tts_voice || "";
  const delivery = deliveryFromInstruct(options.tts_instruct);
  const manner = p?.instruct && delivery.id !== "preset"
    ? `, ${(DELIVERY.find((d) => d.id === delivery.id)?.label || "").toLowerCase().replace("…", "")}` : "";
  return ` · 🎙 ${voice.split(" — ")[0]}${p ? ` (${p.name}${manner})` : ""}${options.karaoke === false ? "" : " · караоке"}`;
}

// ---------- рилсы ----------

$("#reel-form").addEventListener("submit", (e) => guarded(async () => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.duration = Number(data.duration);
  if (data.music_id === "none") data.music_id = "__none__";
  Object.assign(data, readVoice($("#voice-controls")));
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
          <div class="meta muted">${new Date(r.created_at).toLocaleString("ru-RU")}${r.duration ? ` · ${r.duration} с` : ""}${voiceLabel(r.options)}${sourceNote}</div>
          ${r.status === "error" ? `<p class="error">${esc(r.error)}</p>` : ""}
          ${r.status === "done" && r.voice_note ? `<p class="voice-note">⚠️ ${esc(r.voice_note)}</p>` : ""}
          ${s.caption ? `<div class="caption">${esc(s.caption)}<div class="tags">${esc((s.hashtags || []).join(" "))}</div></div>` : ""}
          ${s.scenes ? `<ol class="scenes-preview">${s.scenes.map((sc) => `<li>${esc(sc.text || "(без текста)")} — ${sc.duration} с${sc.voice ? `<br><span class="voice-text">🎙 ${esc(sc.voice)}</span>` : ""}</li>`).join("")}</ol>` : ""}
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
        <input type="number" data-f="duration" value="${sc.duration}" min="0.5" max="30" step="any" title="Секунд">
        <button type="button" class="btn small ghost" data-move="-1" title="Выше">↑</button>
        <button type="button" class="btn small ghost" data-move="1" title="Ниже">↓</button>
        <button type="button" class="btn small ghost danger" data-remove title="Удалить сцену">✕</button>
      </div>
      <label class="scene-field">Текст на экране<textarea data-f="text" rows="2" maxlength="300">${esc(sc.text)}</textarea></label>
      <label class="scene-field">🎙 Озвучка<textarea data-f="voice" rows="2" maxlength="600" placeholder="Пусто — сцена без голоса">${esc(sc.voice)}</textarea></label>
      <input type="hidden" data-f="start" value="${sc.start || 0}">
    </div>
  </div>`;
}

function readScenes() {
  return [...document.querySelectorAll("#scene-list .scene")].map((el) => ({
    material_id: $("[data-f=material_id]", el).value,
    duration: Number($("[data-f=duration]", el).value) || 3,
    text: $("[data-f=text]", el).value,
    voice: $("[data-f=voice]", el).value,
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
  mountVoice($("#editor-voice"), reel.options || {});
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
    voice_options: readVoice($("#editor-voice")),
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
  await guarded(async () => { state.tts = await api("/api/tts"); });
  await guarded(loadProjects);
  const id = location.hash.slice(1);
  if (id && state.projects.some((p) => p.id === id)) guarded(() => openProject(id));
})();
