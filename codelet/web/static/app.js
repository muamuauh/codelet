"use strict";
// Thin client for the codelet web GUI: one WebSocket, a render(event) switch,
// and REST calls to populate the sidebar. No framework, no build step.

const $ = (id) => document.getElementById(id);
const transcript = $("transcript");
const connDot = $("conn");

let ws = null;
let current = null;   // the in-flight assistant message element
let busy = false;

// ---------- helpers ----------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function atBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 60;
}
function scroll() { transcript.scrollTop = transcript.scrollHeight; }

function addMessage(role) {
  const msg = el("div", `msg ${role}`);
  msg.appendChild(el("div", "role", role));
  const bubble = el("div", "bubble");
  msg.appendChild(bubble);
  transcript.appendChild(msg);
  scroll();
  return { msg, bubble, text: "" };
}

function ensureAssistant() {
  if (!current) current = addMessage("assistant");
  return current;
}

// ---------- event rendering ----------

function render(ev) {
  switch (ev.type) {
    case "profile": return onProfile(ev);
    case "thinking": return onThinking(ev.on);
    case "text_delta": {
      const a = ensureAssistant();
      clearThinking(a);
      a.text += ev.text;
      a.bubble.textContent = a.text;
      if (atBottom()) scroll();
      return;
    }
    case "stream_end": return;
    case "tool_start": {
      const a = ensureAssistant();
      clearThinking(a);
      const card = el("div", "tool");
      const head = el("div", "head");
      head.appendChild(el("span", "name", ev.name));
      head.appendChild(el("span", "badge run", "running"));
      card.appendChild(head);
      const args = el("div", "args", JSON.stringify(ev.input));
      card.appendChild(args);
      card._name = ev.name; card._done = false;
      a.msg.appendChild(card);
      if (atBottom()) scroll();
      return;
    }
    case "tool_result": {
      const a = ensureAssistant();
      const card = [...a.msg.querySelectorAll(".tool")].reverse()
        .find((c) => c._name === ev.name && !c._done);
      const badge = card ? card.querySelector(".badge") : null;
      if (card) {
        card._done = true;
        badge.className = "badge " + (ev.is_error ? "err" : "ok");
        badge.textContent = ev.is_error ? "error" : "ok";
        const out = el("div", "out", ev.output);
        card.appendChild(out);
      }
      if (atBottom()) scroll();
      return;
    }
    case "notice": {
      transcript.appendChild(el("div", `note ${ev.level || "dim"}`, ev.message));
      if (atBottom()) scroll();
      return;
    }
    case "permission_request": return showModal(ev);
    case "telemetry": return onTelemetry(ev);
    case "turn_done": {
      if (current) clearThinking(current);
      current = null;
      setBusy(false);
      return;
    }
    case "resumed": return onResumed(ev);
    case "error": {
      transcript.appendChild(el("div", "note error", ev.message));
      current = null; setBusy(false); scroll();
      return;
    }
  }
}

function onThinking(on) {
  const a = ensureAssistant();
  if (on) {
    if (!a.msg.querySelector(".thinking")) {
      const t = el("div", "thinking", "thinking");
      a.bubble.after(t);
    }
  } else {
    clearThinking(a);
  }
}
function clearThinking(a) {
  const t = a.msg.querySelector(".thinking");
  if (t) t.remove();
}

function onTelemetry(ev) {
  const t = ev.turn || {}, s = ev.session || {};
  $("tel-turn").textContent = (t.input_tokens || 0) + " / " + (t.output_tokens || 0);
  $("tel-session").textContent = (s.input_tokens || 0) + " / " + (s.output_tokens || 0);
  $("tel-cost").textContent = s.cost_usd == null ? "n/a" : "$" + s.cost_usd.toFixed(4);
}

function onProfile(ev) {
  $("provider-model").textContent = `${ev.provider}/${ev.model}` + (ev.profile ? ` · ${ev.profile}` : "");
  if (ev.mode) $("mode").value = ev.mode;
  const sel = $("profile");
  if (ev.profiles && sel.options.length !== ev.profiles.length + 1) {
    sel.innerHTML = "";
    sel.appendChild(new Option("(default)", ""));
    ev.profiles.forEach((p) => sel.appendChild(new Option(p, p)));
    if (ev.profile) sel.value = ev.profile;
  }
}

function onResumed(ev) {
  transcript.innerHTML = "";
  current = null;
  (ev.messages || []).forEach((m) => {
    const b = addMessage(m.role);
    b.bubble.textContent = m.text || "";
    (m.tools || []).forEach((name) => {
      const card = el("div", "tool");
      const head = el("div", "head");
      head.appendChild(el("span", "name", name));
      head.appendChild(el("span", "badge ok", "done"));
      card.appendChild(head);
      b.msg.appendChild(card);
    });
  });
  current = null;
  scroll();
}

// ---------- diff modal ----------

function showModal(ev) {
  $("modal-tool").textContent = ev.tool;
  const pre = $("modal-diff");
  pre.innerHTML = "";
  ev.diff.split("\n").forEach((line) => {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    pre.appendChild(el("span", cls, line + "\n"));
  });
  $("modal").classList.remove("hidden");
  const decide = (approve) => {
    $("modal").classList.add("hidden");
    send({ type: approve ? "approve" : "reject", id: ev.id });
  };
  $("approve").onclick = () => decide(true);
  $("reject").onclick = () => decide(false);
}

// ---------- socket ----------

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => connDot.classList.add("on");
  ws.onclose = () => { connDot.classList.remove("on"); setTimeout(connect, 1500); };
  ws.onmessage = (e) => render(JSON.parse(e.data));
}

function setBusy(b) {
  busy = b;
  $("send").disabled = b;
}

// ---------- input ----------

function submit() {
  const inp = $("input");
  const text = inp.value.trim();
  if (!text || busy) return;
  addMessage("user").bubble.textContent = text;
  inp.value = ""; inp.style.height = "auto";
  current = null;
  setBusy(true);
  send(text.startsWith("/") ? { type: "slash", line: text } : { type: "prompt", text });
  scroll();
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
});
$("input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 180) + "px";
});
$("mode").addEventListener("change", (e) => send({ type: "set_mode", mode: e.target.value }));
$("profile").addEventListener("change", (e) => send({ type: "set_profile", name: e.target.value }));

// ---------- sidebar (REST) ----------

async function loadPanels() {
  try {
    const s = await (await fetch("/api/sessions")).json();
    fillSessions("sessions-project", s.project);
    fillSessions("sessions-global", s.global);
  } catch (_) {}
  fillList("tools", await safe("/api/tools"), (t) => `${t.name}`, (t) => t.description);
  fillList("skills", await safe("/api/skills"), (t) => t.name, (t) => t.description);
}
async function safe(url) { try { return await (await fetch(url)).json(); } catch (_) { return []; } }

function fillSessions(id, rows) {
  const ul = $(id); ul.innerHTML = "";
  if (!rows || !rows.length) { ul.appendChild(el("li", "meta", "(none)")); return; }
  rows.forEach((r) => {
    const li = el("li", "session");
    li.appendChild(el("span", "title", r.title || r.summary || "(untitled)"));
    li.appendChild(el("span", "meta", `${r.id} · ${r.message_count ?? "?"} msgs`));
    li.onclick = () => send({ type: "resume", id: r.id });
    ul.appendChild(li);
  });
}
function fillList(id, rows, title, desc) {
  const ul = $(id); ul.innerHTML = "";
  (rows || []).forEach((r) => {
    const li = el("li");
    li.appendChild(el("span", "title", title(r)));
    if (desc(r)) li.appendChild(el("span", "desc", " — " + desc(r)));
    ul.appendChild(li);
  });
}

connect();
loadPanels();
