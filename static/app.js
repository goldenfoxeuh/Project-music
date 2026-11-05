"use strict";

const $ = (sel) => document.querySelector(sel);
const PLATFORM_COLORS = { spotify: "var(--spotify)", deezer: "var(--deezer)", youtube: "var(--youtube)", apple: "var(--apple)" };

const state = {
  platforms: [],
  playlistKey: null,
  playlist: null,
  target: null,
  job: null,
};

// --- mémoire de session (pour revenir au même endroit après une connexion OAuth)
const memory = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} },
};

function flash(message, ok = false) {
  const el = $("#flash");
  el.textContent = message;
  el.className = "flash" + (ok ? " ok" : "");
  el.hidden = !message;
  if (message) el.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const err = new Error(data.error || data.detail?.[0]?.msg || `Erreur ${res.status}`);
    err.authRequired = data.auth_required;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDuration(ms) {
  if (!ms) return "";
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function trackLine(t) {
  return `${(t.artists || []).join(", ")}${t.artists?.length ? " - " : ""}${t.title}`;
}

function platformName(key) {
  return state.platforms.find((p) => p.key === key)?.name || key;
}

// ------------------------------------------------------------------ plateformes
async function loadPlatforms() {
  state.platforms = await api("/api/platforms");
  renderTargets();
}

function renderTargets() {
  const box = $("#targets");
  box.innerHTML = "";
  for (const p of state.platforms) {
    if (state.playlist && p.key === state.playlist.platform) continue;
    const el = document.createElement("div");
    el.className = "target" + (state.target === p.key ? " selected" : "") + (p.can_write ? "" : " disabled");
    let status;
    if (!p.can_write) status = `<span class="state">Non configuré</span><span class="state">${esc(p.setup_hint)}</span>`;
    else if (p.connected) status = `<span class="state ok">✓ Compte connecté</span><button type="button" class="link" data-logout>Se déconnecter</button>`;
    else status = `<button type="button" class="secondary" data-connect>Connecter mon compte</button>`;
    el.innerHTML = `<div class="name"><span class="dot" style="background:${PLATFORM_COLORS[p.key]}"></span>${esc(p.name)}</div>${status}`;
    if (p.can_write) {
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-connect]")) return connect(p);
        if (e.target.closest("[data-logout]")) return logout(p);
        state.target = p.key;
        memory.set("target", p.key);
        renderTargets();
      });
    }
    box.appendChild(el);
  }
  const target = state.platforms.find((p) => p.key === state.target);
  $("#match-btn").disabled = !(target && target.can_write && state.playlistKey);
  $("#match-btn").textContent = target && !target.connected
    ? `Connecter ${target.name} et rechercher`
    : "Rechercher les morceaux";
}

async function connect(p) {
  memory.set("target", p.key);
  if (p.oauth) {
    window.location.href = `/auth/${p.key}/login`;
    return;
  }
  if (p.key === "apple") return connectApple();
}

async function logout(p) {
  await api(`/auth/${p.key}/logout`, { method: "POST" });
  await loadPlatforms();
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("Impossible de charger MusicKit JS."));
    document.head.appendChild(s);
  });
}

async function connectApple() {
  try {
    const { token } = await api("/api/apple/developer-token");
    if (!window.MusicKit) await loadScript("https://js-cdn.music.apple.com/musickit/v3/musickit.js");
    await window.MusicKit.configure({ developerToken: token, app: { name: "Playlist Transfer", build: "1.0.0" } });
    const music = window.MusicKit.getInstance();
    const userToken = await music.authorize();
    await api("/api/auth/apple", { method: "POST", body: { music_user_token: userToken } });
    flash("Compte Apple Music connecté.", true);
    await loadPlatforms();
  } catch (e) {
    flash(`Connexion Apple Music impossible : ${e.message || e}`);
  }
}

// ---------------------------------------------------------------------- étape 1
async function readPlaylist(url) {
  flash("");
  const btn = $("#read-btn");
  btn.disabled = true;
  btn.textContent = "Lecture…";
  try {
    const data = await api("/api/playlist", { method: "POST", body: { url } });
    state.playlistKey = data.key;
    state.playlist = data.playlist;
    memory.set("link", url);
    renderPlaylist();
    $("#step-target").hidden = false;
    if (state.target === state.playlist.platform) state.target = null;
    renderTargets();
  } catch (e) {
    if (e.authRequired) {
      const p = state.platforms.find((x) => x.key === e.authRequired);
      flash(e.message);
      if (p && p.can_write && !p.connected) {
        memory.set("link", url);
        memory.set("resume", "read");
        if (confirm(`${e.message}\n\nConnecter ${p.name} maintenant ?`)) return connect(p);
      }
    } else {
      flash(e.message);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Analyser";
  }
}

function renderPlaylist() {
  const pl = state.playlist;
  $("#source-result").hidden = false;
  const badge = $("#pl-platform");
  badge.textContent = platformName(pl.platform);
  badge.className = `badge ${pl.platform}`;
  $("#pl-name").textContent = pl.name;
  const total = pl.tracks.reduce((s, t) => s + (t.duration_ms || 0), 0);
  $("#pl-meta").textContent = [
    `${pl.tracks.length} morceaux`,
    total ? `${Math.round(total / 60000)} min` : null,
    pl.owner ? `par ${pl.owner}` : null,
  ].filter(Boolean).join(" · ");
  const img = $("#pl-image");
  img.hidden = !pl.image;
  if (pl.image) img.src = pl.image;
  $("#pl-tracks").innerHTML = pl.tracks
    .map((t) => `<li>${esc(trackLine(t))} <span class="muted">${fmtDuration(t.duration_ms)}</span></li>`)
    .join("");
  $("#new-name").value = pl.name;
}

function download(filename, content, type) {
  const blob = new Blob([content], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    flash(message, true);
  } catch {
    download("playlist.txt", text, "text/plain");
  }
}

// ---------------------------------------------------------------------- étape 3
async function startMatch() {
  const target = state.platforms.find((p) => p.key === state.target);
  if (!target) return;
  if (!target.connected) {
    memory.set("resume", "match");
    return connect(target);
  }
  flash("");
  $("#step-match").hidden = false;
  $("#step-create").hidden = true;
  $("#match-summary").hidden = true;
  $("#create-result").hidden = true;
  $("#match-btn").disabled = true;
  try {
    let job = await api("/api/match", { method: "POST", body: { playlist_key: state.playlistKey, target: target.key } });
    while (job.status === "running") {
      updateProgress(job);
      await new Promise((r) => setTimeout(r, 800));
      job = await api(`/api/jobs/${job.id}?results=false`);
    }
    job = await api(`/api/jobs/${job.id}`);
    state.job = job;
    updateProgress(job);
    renderMatches();
    $("#step-create").hidden = false;
    $("#step-create").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    flash(e.message);
    if (e.authRequired) await loadPlatforms();
  } finally {
    $("#match-btn").disabled = false;
  }
}

function updateProgress(job) {
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 100;
  $("#progress-bar").style.width = pct + "%";
  $("#progress-text").textContent = job.status === "running"
    ? `Recherche sur ${platformName(job.target)}… ${job.done} / ${job.total}`
    : `${job.found} trouvés sur ${job.total} · ${job.missing} introuvables`;
}

function scoreClass(s) { return s >= 90 ? "high" : s >= 75 ? "mid" : "low"; }

function renderMatches() {
  const onlyMissing = $("#only-missing").checked;
  const rows = state.job.results.map((r, i) => {
    if (onlyMissing && r.match) return "";
    const src = r.source;
    const m = r.match;
    const found = m
      ? `<div class="t">${esc(m.title)}</div><div class="a">${esc((m.artists || []).join(", "))}${m.album ? " · " + esc(m.album) : ""}</div>`
      : `<div class="t">Introuvable</div>`;
    const score = m ? `<span class="score ${scoreClass(r.score)}">${r.method === "isrc" ? "ISRC ✓" : Math.round(r.score) + " %"}</span>` : "";
    return `<tr class="${m ? "" : "missing"}">
      <td class="sel">${m ? `<input type="checkbox" data-i="${i}" ${r.checked === false ? "" : "checked"}>` : ""}</td>
      <td><div class="t">${esc(src.title)}</div><div class="a">${esc((src.artists || []).join(", "))}</div></td>
      <td class="found">${found}</td>
      <td>${score}</td></tr>`;
  });
  $("#match-rows").innerHTML = rows.join("");
  $("#match-summary").hidden = false;
}

// ---------------------------------------------------------------------- étape 4
async function createPlaylist() {
  const job = state.job;
  if (!job) return;
  const ids = job.results.filter((r) => r.match && r.checked !== false).map((r) => r.match.platform_id);
  const btn = $("#create-btn");
  btn.disabled = true;
  btn.textContent = "Création…";
  flash("");
  try {
    const desc = `Copiée depuis ${platformName(state.playlist.platform)}${state.playlist.url ? " : " + state.playlist.url : ""}`;
    const res = await api("/api/create", {
      method: "POST",
      body: { job_id: job.id, name: $("#new-name").value, description: desc, track_ids: ids },
    });
    const box = $("#create-result");
    box.hidden = false;
    box.innerHTML = `✓ Playlist créée sur ${esc(platformName(job.target))} avec ${res.added} morceaux :<br>
      <a href="${esc(res.url)}" target="_blank" rel="noopener">${esc(res.url)}</a>`;
    memory.set("resume", null);
  } catch (e) {
    flash(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Créer la playlist";
  }
}

// ---------------------------------------------------------------- événements
$("#link-form").addEventListener("submit", (e) => {
  e.preventDefault();
  readPlaylist($("#link").value.trim());
});
$("#match-btn").addEventListener("click", startMatch);
$("#create-btn").addEventListener("click", createPlaylist);
$("#only-missing").addEventListener("change", renderMatches);
$("#match-rows").addEventListener("change", (e) => {
  const i = e.target.dataset.i;
  if (i != null) state.job.results[+i].checked = e.target.checked;
});
$("#copy-missing").addEventListener("click", () => {
  const missing = state.job.results.filter((r) => !r.match).map((r) => trackLine(r.source));
  copyText(missing.join("\n") || "(aucun)", `${missing.length} morceaux introuvables copiés dans le presse-papiers.`);
});
$("#export-txt").addEventListener("click", () => {
  copyText(state.playlist.tracks.map(trackLine).join("\n"), "Liste des morceaux copiée dans le presse-papiers.");
});
$("#export-csv").addEventListener("click", () => {
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [["Titre", "Artistes", "Album", "Durée", "ISRC"].join(",")];
  for (const t of state.playlist.tracks) {
    lines.push([q(t.title), q((t.artists || []).join(", ")), q(t.album), q(fmtDuration(t.duration_ms)), q(t.isrc)].join(","));
  }
  download(`${state.playlist.name || "playlist"}.csv`, "﻿" + lines.join("\n"), "text/csv");
});

// ---------------------------------------------------------------- démarrage
(async function init() {
  const params = new URLSearchParams(location.search);
  if (params.get("error")) flash(params.get("error"));
  if (params.get("connected")) flash(`Compte ${params.get("connected")} connecté.`, true);
  if (params.toString()) history.replaceState(null, "", "/");

  state.target = memory.get("target");
  try {
    await loadPlatforms();
  } catch (e) {
    flash("Le serveur ne répond pas : " + e.message);
    return;
  }
  // Retour d'une connexion OAuth : on reprend là où on en était.
  const link = memory.get("link");
  const resume = memory.get("resume");
  if (link) {
    $("#link").value = link;
    if (params.get("connected") || resume) {
      if (resume === "read") memory.set("resume", null);
      await readPlaylist(link);
      if (resume === "match" && params.get("connected") === state.target) {
        memory.set("resume", null);
        await startMatch();
      }
    }
  }
})();
