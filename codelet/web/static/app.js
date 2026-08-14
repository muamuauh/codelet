"use strict";
// codelet web GUI client: one WebSocket + a render(event) switch, plus REST for
// the sidebar, uploads, and directory browsing. No framework, no build step.

const $ = (id) => document.getElementById(id);
const transcript = $("transcript");
const scroller = $("scroller");

let ws = null, current = null, busy = false, activeSession = null, attachments = [];

// ---------- dom helpers ----------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const fmt = (n) => (n || 0).toLocaleString();
const atBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
const scroll = () => { scroller.scrollTop = scroller.scrollHeight; };
function toggleEmpty() { $("empty").classList.toggle("hidden", transcript.children.length > 0); }

function addMessage(role) {
  const msg = el("div", `msg ${role}`);
  msg.appendChild(el("div", "avatar", role === "assistant" ? "◆" : "U"));
  const body = el("div", "body");
  body.appendChild(el("div", "who", role === "assistant" ? "codelet" : "You"));
  const bubble = el("div", "bubble");
  body.appendChild(bubble);
  msg.appendChild(body);
  transcript.appendChild(msg);
  toggleEmpty(); scroll();
  return { msg, body, bubble, text: "" };
}
function ensureAssistant() { return current || (current = addMessage("assistant")); }
function clearThinking(a) { const t = a.body.querySelector(".thinking"); if (t) t.remove(); }

// ---------- render incoming frames ----------
function render(ev) {
  switch (ev.type) {
    case "profile": return onProfile(ev);
    case "workspace": return onWorkspace(ev);
    case "cleared": transcript.innerHTML = ""; current = null; toggleEmpty(); return;
    case "sessions_changed": return loadSessions();
    case "tools": return renderToggleList("tools", ev.tools, "set_tool_enabled");
    case "skills": return renderToggleList("skills", ev.skills, "set_skill_enabled");
    case "thinking": return onThinking(ev.on);
    case "text_delta": {
      const a = ensureAssistant(); clearThinking(a);
      a.text += ev.text; a.bubble.textContent = a.text;
      if (atBottom()) scroll();
      return;
    }
    case "stream_end": return;
    case "tool_start": {
      const a = ensureAssistant(); clearThinking(a);
      const card = el("div", "tool");
      const head = el("div", "head");
      head.appendChild(el("span", "tname", ev.name));
      head.appendChild(el("span", "badge run", "running"));
      const args = el("div", "args", JSON.stringify(ev.input));
      head.onclick = () => { args.style.display = args.style.display === "none" ? "" : "none"; };
      card.appendChild(head); card.appendChild(args);
      card._name = ev.name; card._done = false;
      a.body.appendChild(card);
      if (atBottom()) scroll();
      return;
    }
    case "tool_result": {
      const a = ensureAssistant();
      const card = [...a.body.querySelectorAll(".tool")].reverse().find((c) => c._name === ev.name && !c._done);
      if (card) {
        card._done = true;
        const badge = card.querySelector(".badge");
        badge.className = "badge " + (ev.is_error ? "err" : "ok");
        badge.textContent = ev.is_error ? "error" : "ok";
        card.appendChild(el("div", "out", ev.output));
      }
      if (atBottom()) scroll();
      return;
    }
    case "notice": { transcript.appendChild(el("div", `note ${ev.level || "info"}`, ev.message)); toggleEmpty(); if (atBottom()) scroll(); return; }
    case "permission_request": return showDiff(ev);
    case "telemetry": return onTelemetry(ev);
    case "turn_done": if (current) clearThinking(current); current = null; setBusy(false); return;
    case "resumed": return onResumed(ev);
    case "error": { transcript.appendChild(el("div", "note error", ev.message)); current = null; setBusy(false); toggleEmpty(); scroll(); return; }
  }
}

function onThinking(on) {
  const a = ensureAssistant();
  if (on && !a.body.querySelector(".thinking")) a.body.appendChild(el("div", "thinking", "thinking"));
  else if (!on) clearThinking(a);
}
function onTelemetry(ev) {
  const t = ev.turn || {}, s = ev.session || {};
  $("tt-in").textContent = fmt(t.input_tokens); $("tt-out").textContent = fmt(t.output_tokens);
  $("ts-in").textContent = fmt(s.input_tokens); $("ts-out").textContent = fmt(s.output_tokens);
  $("ts-cost").textContent = s.cost_usd == null ? "n/a" : "~$" + s.cost_usd.toFixed(4);
}
function onProfile(ev) {
  if (ev.user) {
    $("user").textContent = ev.user;
    $("user-avatar").textContent = (ev.user[0] || "?").toUpperCase();
  }
  if (ev.mode) $("mode").value = ev.mode;
  const sel = $("model");
  const models = (ev.models && ev.models.length) ? ev.models : (ev.model ? [ev.model] : []);
  const sig = models.join("|");
  if (sel._sig !== sig) { sel.innerHTML = ""; models.forEach((m) => sel.appendChild(new Option(m, m))); sel._sig = sig; }
  if (ev.model) sel.value = ev.model;
}
function onWorkspace(ev) { $("ws-name").textContent = ev.name || ev.cwd; $("ws-chip").title = ev.cwd; }
function onResumed(ev) {
  transcript.innerHTML = ""; current = null;
  (ev.messages || []).forEach((m) => {
    const b = addMessage(m.role);
    b.bubble.textContent = m.text || "";
    (m.tools || []).forEach((name) => {
      const card = el("div", "tool"); const head = el("div", "head");
      head.appendChild(el("span", "tname", name));
      head.appendChild(el("span", "badge ok", "done"));
      card.appendChild(head); b.body.appendChild(card);
    });
  });
  current = null; toggleEmpty(); scroll();
}

// ---------- diff approval ----------
function showDiff(ev) {
  $("modal-tool").textContent = ev.tool;
  const pre = $("modal-diff"); pre.innerHTML = "";
  ev.diff.split("\n").forEach((line) => {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    pre.appendChild(el("span", cls, line + "\n"));
  });
  $("modal").classList.remove("hidden");
  const decide = (ok) => { $("modal").classList.add("hidden"); send({ type: ok ? "approve" : "reject", id: ev.id }); };
  $("approve").onclick = () => decide(true);
  $("reject").onclick = () => decide(false);
}

// ---------- socket ----------
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onclose = () => { setTimeout(connect, 1500); };
  ws.onmessage = (e) => render(JSON.parse(e.data));
}
function setBusy(b) { busy = b; $("send").disabled = b; }

// ---------- attachments / upload ----------
async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const locals = files.map((f) => ({ is_image: f.type.startsWith("image/"), url: URL.createObjectURL(f) }));
  let res;
  try { res = await (await fetch("/api/upload", { method: "POST", body: fd })).json(); }
  catch (_) { transcript.appendChild(el("div", "note error", "upload failed")); return; }
  (res.files || []).forEach((sv, i) => attachments.push({ ...sv, url: locals[i] && locals[i].url }));
  renderAttachments();
}
function renderAttachments() {
  const box = $("attachments"); box.innerHTML = "";
  attachments.forEach((a, idx) => {
    const chip = el("div", "att-chip");
    if (a.is_image && a.url) { const im = document.createElement("img"); im.src = a.url; chip.appendChild(im); }
    chip.appendChild(el("span", null, a.name));
    const x = el("span", "x", "×"); x.onclick = () => { attachments.splice(idx, 1); renderAttachments(); };
    chip.appendChild(x); box.appendChild(chip);
  });
}
$("attach").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });

// ---------- composer ----------
function submit() {
  const text = $("input").value.trim();
  if ((!text && !attachments.length) || busy) return;
  const atts = attachments.slice();
  const b = addMessage("user");
  b.bubble.textContent = text || "(sent attachments)";
  if (atts.length) {
    const box = el("div", "att");
    atts.forEach((a) => {
      if (a.is_image && a.url) { const im = document.createElement("img"); im.src = a.url; box.appendChild(im); }
      else box.appendChild(el("span", "file", a.name));
    });
    b.body.appendChild(box);
  }
  $("input").value = ""; $("input").style.height = "auto"; attachments = []; renderAttachments();
  current = null; setBusy(true);
  // Server turns image attachments into vision blocks and other files into a
  // readable path note; slash commands only apply when there are no attachments.
  if (text.startsWith("/") && !atts.length) {
    send({ type: "slash", line: text });
  } else {
    send({ type: "prompt", text, attachments: atts.map((a) => ({ name: a.name, path: a.path, is_image: a.is_image })) });
  }
  scroll();
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
$("input").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px"; });
$("mode").addEventListener("change", (e) => send({ type: "set_mode", mode: e.target.value }));
$("model").addEventListener("change", (e) => send({ type: "set_model", model: e.target.value }));
$("new-chat").addEventListener("click", () => send({ type: "new_conversation" }));

// ---------- workspace picker ----------
let wsCurrent = "";
async function browseTo(path) {
  let r; try { r = await (await fetch("/api/browse?path=" + encodeURIComponent(path || ""))).json(); } catch (_) { return; }
  wsCurrent = r.is_root ? "" : r.path;
  $("ws-cur").textContent = r.path;
  $("ws-input").value = r.is_root ? "" : r.path;
  const ul = $("ws-dirs"); ul.innerHTML = "";
  if (r.parent) {  // ".." — parent dir, or the drive list when at a drive root
    const up = el("li", "up");
    up.innerHTML = '<svg class="ic" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
    up.appendChild(document.createTextNode(" ..")); up.onclick = () => browseTo(r.parent); ul.appendChild(up);
  }
  (r.entries || []).forEach((d) => {
    const li = el("li");
    li.innerHTML = '<svg class="ic" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
    li.appendChild(document.createTextNode(" " + d.name)); li.onclick = () => browseTo(d.path); ul.appendChild(li);
  });
}
$("ws-chip").addEventListener("click", () => { $("ws-modal").classList.remove("hidden"); browseTo(""); });
$("ws-cancel").addEventListener("click", () => $("ws-modal").classList.add("hidden"));
$("ws-go").addEventListener("click", () => browseTo($("ws-input").value.trim()));
$("ws-input").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); browseTo($("ws-input").value.trim()); } });
$("ws-open").addEventListener("click", () => {
  const path = $("ws-input").value.trim() || wsCurrent;
  $("ws-modal").classList.add("hidden");
  send({ type: "set_workspace", path });
});

// ---------- sidebar (REST) ----------
async function safe(url) { try { return await (await fetch(url)).json(); } catch (_) { return null; } }
async function loadSessions() {
  const s = await safe("/api/sessions");
  const ul = $("sessions"); ul.innerHTML = "";
  const rows = (s && s.project) || [];
  if (!rows.length) { ul.appendChild(el("li", "meta", "(no conversations here yet)")); return; }
  rows.forEach((r) => {
    const li = el("li", "session");
    const main = el("div", "s-main");
    main.appendChild(el("span", "title", r.title || r.summary || "(untitled)"));
    main.appendChild(el("span", "meta", `${r.updated_at || ""} · ${r.message_count ?? "?"} msgs`));
    main.onclick = () => { activeSession = r.id; send({ type: "resume", id: r.id }); highlight(); };
    const acts = el("div", "s-acts");
    const rename = el("button", "s-btn", "✎"); rename.title = "rename";
    rename.onclick = (e) => {
      e.stopPropagation();
      const t = prompt("Rename conversation:", r.title || r.summary || "");
      if (t && t.trim()) send({ type: "rename_session", id: r.id, title: t.trim() });
    };
    const del = el("button", "s-btn", "🗑"); del.title = "delete";
    del.onclick = (e) => {
      e.stopPropagation();
      if (confirm("Delete this conversation?")) send({ type: "delete_session", id: r.id });
    };
    acts.appendChild(rename); acts.appendChild(del);
    li.appendChild(main); li.appendChild(acts);
    li._id = r.id; ul.appendChild(li);
  });
  highlight();
}
function highlight() { [...$("sessions").children].forEach((li) => li.classList.toggle("active", li._id === activeSession)); }
async function loadPanels() { await loadSessions(); }  // tools/skills arrive over WS

// Tools & skills: a checkbox per row toggles it live (hot-plug).
function renderToggleList(id, rows, msgType) {
  const ul = $(id); ul.innerHTML = "";
  if (!rows || !rows.length) { ul.appendChild(el("li", "meta", "(none)")); return; }
  rows.forEach((r) => {
    const li = el("li", "toggle" + (r.enabled ? "" : " off"));
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = r.enabled !== false;
    cb.title = r.enabled !== false ? "enabled — click to disable" : "disabled — click to enable";
    cb.onchange = () => send({ type: msgType, name: r.name, enabled: cb.checked });
    const label = el("span", "tname title", r.name);
    if (r.description) label.title = r.description;
    li.appendChild(cb); li.appendChild(label);
    ul.appendChild(li);
  });
}

$("quit").addEventListener("click", async () => {
  if (!confirm("Stop the codelet server and close the app?")) return;
  try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
  if (ws) { ws.onclose = null; ws.close(); }
  document.body.innerHTML =
    '<div style="display:grid;place-items:center;height:100vh;font:15px system-ui;color:#888;background:#1a1a1d">' +
    'codelet server stopped — you can close this tab.</div>';
});

connect();
loadPanels();
toggleEmpty();
