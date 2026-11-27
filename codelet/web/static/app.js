"use strict";
// codelet web GUI client: 3-pane Agent Workbench (Cockpit + Side /btw + Tasks + Changes + Ctrl+K).

const $ = (id) => document.getElementById(id);
const transcript = $("transcript");
const scroller = $("scroller");

let ws = null, current = null, busy = false, activeSession = null, attachments = [];
let activeModel = "", activeMode = "ask", activeEffort = "auto", modelCatalog = [], modelsMeta = {}, activeVendor = "all", refreshingModels = false;
let availableCommands = [], slashIdx = 0;
let cockpitData = null, cockpitChartTab = "speed", cockpitSpendUnit = "usd";
let activeInspectorTab = "cockpit", btwItems = new Map(), cmdIdx = 0, activeDiffPath = "";

// Crisp Lucide-style SVG icons
const SVG_PENCIL = '<svg class="ic" viewBox="0 0 24 24"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>';
const SVG_TRASH = '<svg class="ic" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';
const SVG_COPY = '<svg class="ic" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const SVG_CHECK = '<svg class="ic" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';
const SVG_CHEV_R = '<svg class="ic t-chev" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>';
const SVG_SUN = '<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>';
const SVG_MOON = '<svg class="ic" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
const SVG_SEND = '<svg class="ic" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
const SVG_STOP = '<svg class="ic" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/></svg>';
const SVG_BRAIN = '<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l2.5 2.5"/></svg>';
const MODE_ICONS = {
  normal: '<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9 12l2 2 4-4"/></svg>',
  ask: '<svg class="ic" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9.5 12l1.8 1.8L15 10"/></svg>',
  plan: '<svg class="ic" viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  auto: '<svg class="ic" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
};
const MODE_DESC = {
  normal: "标准协同：写文件前预览 Diff 确认，白名单常用开发命令自动放行。",
  ask: "步步确认：写文件预览 Diff 且所有非只读终端命令均弹窗确认。",
  plan: "只读规划：仅允许只读分析与规划，所有写入类工具硬拦截。",
  auto: "全自动执行：写文件与常规命令免确认直接执行（仍受高危红线保护）。",
};
const EFFORT_DESC = {
  auto: "跟随当前模型默认思考策略",
  off: "关闭思考链（最低延迟与成本）",
  low: "轻度思考，适合简单修改与问答",
  medium: "中度思考，平衡推理质量与速度",
  high: "深度思考，适合架构设计与疑难排查",
};

// ---------- helpers ----------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const fmt = (n) => (n || 0).toLocaleString();
const fmtCompact = (n) => {
  n = Number(n || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K";
  return String(Math.round(n));
};
const fmtMs = (ms) => {
  if (ms == null) return "—";
  return ms >= 1000 ? (ms / 1000).toFixed(2) + "s" : Math.round(ms) + "ms";
};
const fmtUsd = (v) => (v == null ? "n/a" : "$" + Number(v).toFixed(v >= 1 ? 2 : 4));
const atBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 90;
const scroll = () => { scroller.scrollTop = scroller.scrollHeight; };
function toggleEmpty() { $("empty").classList.toggle("hidden", transcript.children.length > 0); }

async function copyText(text, btn, withLabel) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (__) {}
    ta.remove();
  }
  btn.classList.add("copied");
  const prev = btn.innerHTML;
  btn.innerHTML = SVG_CHECK + (withLabel ? "<span>Copied</span>" : "");
  setTimeout(() => { btn.classList.remove("copied"); btn.innerHTML = prev; }, 1300);
}

// ---------- safe zero-dep Markdown renderer ----------
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function safeHref(url) {
  const u = (url || "").trim();
  return /^(https?:\/\/|mailto:)/i.test(u) ? escHtml(u) : "";
}
function renderInline(src) {
  const codes = [];
  let s = escHtml(src).replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(`<code>${c}</code>`);
    return `\x00C${codes.length - 1}\x00`;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
    const href = safeHref(url);
    return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?;:]|$)/g, "$1<em>$2</em>");
  return s.replace(/\x00C(\d+)\x00/g, (_, i) => codes[Number(i)]);
}
function renderMarkdown(md) {
  if (!md) return "";
  const blocks = [];
  const text = String(md).replace(/\r\n/g, "\n").replace(
    /```([A-Za-z0-9_+-]*)[ \t]*\n([\s\S]*?)(?:\n```[ \t]*(?=\n|$)|$)/g,
    (_, lang, code) => {
      const idx = blocks.length;
      blocks.push(
        `<div class="code-block"><div class="code-head"><span class="code-lang">${escHtml(lang || "text")}</span>` +
        `<button class="code-copy" type="button">${SVG_COPY}<span>Copy</span></button></div>` +
        `<pre><code>${escHtml(code)}</code></pre></div>`
      );
      return `\n\x00B${idx}\x00\n`;
    }
  );
  const lines = text.split("\n"), out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const bMatch = line.trim().match(/^\x00B(\d+)\x00$/);
    if (bMatch) { out.push(blocks[Number(bMatch[1])]); i++; continue; }
    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if (h) { out.push(`<h${h[1].length}>${renderInline(h[2])}</h${h[1].length}>`); i++; continue; }
    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) { out.push("<hr />"); i++; continue; }
    if (/^>\s?/.test(line)) {
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/, "")); i++; }
      out.push(`<blockquote>${q.map((l) => renderInline(l)).join("<br />")}</blockquote>`);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1])) {
      const splitRow = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = splitRow(line); i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) { rows.push(splitRow(lines[i])); i++; }
      out.push(`<table><thead><tr>${head.map((c) => `<th>${renderInline(c)}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(`<li>${renderInline(lines[i].replace(/^\s*[-*+]\s+/, ""))}</li>`); i++; }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(`<li>${renderInline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`); i++; }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    const p = [];
    while (
      i < lines.length && lines[i].trim() &&
      !/^\x00B\d+\x00$/.test(lines[i].trim()) && !/^(#{1,4})\s+/.test(lines[i]) &&
      !/^>\s?/.test(lines[i]) && !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) && !/^\s*(?:---|\*\*\*|___)\s*$/.test(lines[i])
    ) { p.push(renderInline(lines[i])); i++; }
    out.push(`<p>${p.join("<br />")}</p>`);
  }
  return out.join("");
}
function wireCodeCopies(container) {
  container.querySelectorAll(".code-copy").forEach((btn) => {
    btn.onclick = () => {
      const codeEl = btn.closest(".code-block")?.querySelector("pre code");
      if (codeEl) copyText(codeEl.textContent || "", btn, true);
    };
  });
}
function setAssistantMarkdown(a) {
  a.bubble.innerHTML = renderMarkdown(a.text);
  wireCodeCopies(a.bubble);
}

// ---------- tool summary ----------
function summarizeToolInput(name, input) {
  if (!input || typeof input !== "object") return "";
  const pick = (...keys) => {
    for (const k of keys) if (input[k] != null && String(input[k]).trim()) return String(input[k]).trim();
    return "";
  };
  let s = "";
  if (name === "bash" || name === "sandbox") s = pick("command", "cmd");
  else if (name === "read_file" || name === "write_file" || name === "edit_file") s = pick("path", "file_path");
  else if (name === "glob" || name === "grep") s = pick("pattern", "query");
  else if (name === "search_docs") s = pick("query");
  else if (name === "web_fetch") s = pick("url");
  else if (name === "task") s = pick("description", "prompt");
  else if (name === "skill") s = pick("name");
  else if (name === "memory") s = [pick("action"), pick("key")].filter(Boolean).join(" ");
  else if (name === "create_tool") s = pick("name");
  else {
    const first = Object.values(input).find((v) => typeof v === "string" && v.trim());
    if (first) s = first.trim();
  }
  s = s.replace(/\s+/g, " ");
  return s.length > 72 ? s.slice(0, 71) + "…" : s;
}

// ---------- messages ----------
function addMessage(role) {
  const msg = el("div", `msg ${role}`);
  msg.appendChild(el("div", "avatar", role === "assistant" ? "◆" : "U"));
  const body = el("div", "body");
  const head = el("div", "msg-head");
  head.appendChild(el("div", "who", role === "assistant" ? "codelet" : "You"));
  const entry = { msg, body, bubble: null, text: "", reasoning: "", thinkBox: null, thinkPre: null };
  if (role === "assistant") {
    const copyBtn = el("button", "msg-copy");
    copyBtn.type = "button";
    copyBtn.innerHTML = SVG_COPY + "<span>Copy</span>";
    copyBtn.onclick = () => copyText(entry.text, copyBtn, true);
    head.appendChild(copyBtn);
  }
  body.appendChild(head);
  const bubble = el("div", role === "assistant" ? "bubble md" : "bubble");
  entry.bubble = bubble;
  body.appendChild(bubble);
  msg.appendChild(body);
  transcript.appendChild(msg);
  toggleEmpty(); scroll();
  return entry;
}
function ensureAssistant() { return current || (current = addMessage("assistant")); }
function clearThinking(a) { const t = a.body.querySelector(".thinking"); if (t) t.remove(); }

// ---------- incoming WS frames ----------
function render(ev) {
  switch (ev.type) {
    case "profile": return onProfile(ev);
    case "workspace": return onWorkspace(ev);
    case "cleared": transcript.innerHTML = ""; current = null; toggleEmpty(); return;
    case "sessions_changed": return loadSessions();
    case "tools": return renderToolsList(ev.tools);
    case "skills": return renderSkillsList(ev.skills);
    case "plugins": return renderPluginsList(ev.plugins);
    case "commands": availableCommands = Array.isArray(ev.commands) ? ev.commands : []; return;
    case "todos": return renderTodos(ev.todos || []);
    case "cockpit": return onCockpit(ev);
    case "panels": return syncPluginPanels(ev.panels || []);
    case "panel_data": return renderPluginPanelData(ev);
    case "ui_action": return onUiAction(ev);
    case "export_markdown": return downloadMarkdown(ev.filename || "codelet.md", ev.content || "");
    case "btw_start": return onBtwStart(ev);
    case "btw_delta": return onBtwDelta(ev);
    case "btw_done": return onBtwDone(ev);
    case "btw_error": return onBtwError(ev);
    case "btw_cleared": return clearBtwUI();
    case "thinking": return onThinking(ev.on);
    case "reasoning_delta": {
      const a = ensureAssistant(); clearThinking(a);
      if (!a.thinkBox) {
        const d = document.createElement("details");
        d.className = "thinking-box"; d.open = true;
        const s = document.createElement("summary");
        s.innerHTML = SVG_BRAIN + "<span>Thought process</span>";
        const pre = el("div", "thinking-text");
        d.appendChild(s); d.appendChild(pre);
        a.body.insertBefore(d, a.bubble);
        a.thinkBox = d; a.thinkPre = pre;
      }
      a.reasoning += ev.text;
      a.thinkPre.textContent = a.reasoning;
      if (atBottom()) scroll();
      return;
    }
    case "text_delta": {
      const a = ensureAssistant(); clearThinking(a);
      if (a.thinkBox && !a.text) a.thinkBox.open = false;
      a.text += ev.text;
      setAssistantMarkdown(a);
      if (atBottom()) scroll();
      return;
    }
    case "stream_end": return;
    case "tool_start": {
      const a = ensureAssistant(); clearThinking(a);
      const card = el("div", "tool");
      const head = el("div", "head");
      head.innerHTML = SVG_CHEV_R;
      head.appendChild(el("span", "tname", ev.name));
      const summary = summarizeToolInput(ev.name, ev.input);
      if (summary) head.appendChild(el("span", "tsum", summary));
      head.appendChild(el("span", "badge run", "running"));
      head.onclick = () => card.classList.toggle("open");
      const tbody = el("div", "tbody");
      tbody.appendChild(el("div", "t-sec-lbl", "Input"));
      tbody.appendChild(el("div", "args", JSON.stringify(ev.input || {}, null, 2)));
      card.appendChild(head); card.appendChild(tbody);
      card._name = ev.name; card._done = false; card._tbody = tbody;
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
        if (ev.output) {
          card._tbody.appendChild(el("div", "t-sec-lbl", "Output"));
          card._tbody.appendChild(el("div", "out", ev.output));
        }
        if (ev.is_error) card.classList.add("open");
      }
      if (["write_file", "edit_file", "bash"].includes(ev.name) && activeInspectorTab === "changes" && $("app").classList.contains("insp-open")) {
        loadGitChanges();
      }
      if (atBottom()) scroll();
      return;
    }
    case "notice": { transcript.appendChild(el("div", `note ${ev.level || "info"}`, ev.message)); toggleEmpty(); if (atBottom()) scroll(); return; }
    case "permission_request": return showDiff(ev);
    case "telemetry": return onTelemetry(ev);
    case "turn_cancelled":
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
function onUiAction(ev) {
  if (ev.open_drawer) openInspector(ev.open_drawer);
  if (ev.open_modal === "model") openModelModal();
}
function downloadMarkdown(filename, content) {
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
}

// ---------- inspector drawer ----------
function openInspector(tab) {
  $("app").classList.add("insp-open");
  $("toggle-inspector").classList.add("active");
  try { localStorage.setItem("codelet_insp_open", "1"); } catch (_) {}
  if (tab) switchInspectorTab(tab);
}
function closeInspector() {
  $("app").classList.remove("insp-open");
  $("toggle-inspector").classList.remove("active");
  try { localStorage.setItem("codelet_insp_open", "0"); } catch (_) {}
}
function switchInspectorTab(tab) {
  activeInspectorTab = tab;
  try { localStorage.setItem("codelet_insp_tab", tab); } catch (_) {}
  document.querySelectorAll(".insp-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".insp-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === tab));
  if (tab === "cockpit") send({ type: "get_cockpit" });
  else if (tab === "changes") loadGitChanges();
  else if (tab === "btw") setTimeout(() => $("btw-input")?.focus(), 40);
  else if (tab && tab.startsWith("plugin:")) send({ type: "render_panel", id: tab.slice(7) });
}

// ---------- Dynamic plugin UI panels ----------
function syncPluginPanels(panels) {
  const tabBar = document.querySelector(".insp-tabs");
  const insp = $("inspector");
  if (!tabBar || !insp) return;
  const keepIds = new Set(panels.map((p) => "plugin:" + p.id));

  // Remove tabs/panes belonging to unmounted plugins.
  tabBar.querySelectorAll(".insp-tab[data-dynamic='1']").forEach((btn) => {
    if (!keepIds.has(btn.dataset.tab)) btn.remove();
  });
  insp.querySelectorAll(".insp-pane[data-dynamic='1']").forEach((pane) => {
    if (!keepIds.has(pane.dataset.pane)) pane.remove();
  });
  if (activeInspectorTab.startsWith("plugin:") && !keepIds.has(activeInspectorTab)) {
    switchInspectorTab("cockpit");
  }

  panels.forEach((p) => {
    const key = "plugin:" + p.id;
    let btn = tabBar.querySelector(`.insp-tab[data-tab="${CSS.escape(key)}"]`);
    if (!btn) {
      btn = el("button", "insp-tab", p.title || p.id);
      btn.type = "button";
      btn.dataset.tab = key;
      btn.dataset.dynamic = "1";
      btn.setAttribute("role", "tab");
      if (p.description) btn.title = p.description;
      btn.addEventListener("click", () => switchInspectorTab(key));
      tabBar.appendChild(btn);
    }
    let pane = insp.querySelector(`.insp-pane[data-pane="${CSS.escape(key)}"]`);
    if (!pane) {
      pane = el("section", "insp-pane plugin-pane");
      pane.dataset.pane = key;
      pane.dataset.dynamic = "1";
      const bar = el("div", "changes-bar");
      bar.appendChild(el("span", "git-branch mono", `${p.plugin || "plugin"} · ${p.title || p.id}`));
      const refBtn = el("button", "ck-mini-btn", "刷新");
      refBtn.type = "button";
      refBtn.onclick = () => send({ type: "render_panel", id: p.id });
      bar.appendChild(refBtn);
      pane.appendChild(bar);
      pane.appendChild(el("div", "plugin-pane-body"));
      insp.appendChild(pane);
    }
    if (activeInspectorTab === key) {
      btn.classList.add("active");
      pane.classList.add("active");
      send({ type: "render_panel", id: p.id });
    }
  });
}

function renderPluginPanelData(ev) {
  const key = "plugin:" + (ev.id || "");
  const pane = document.querySelector(`.insp-pane[data-pane="${CSS.escape(key)}"] .plugin-pane-body`);
  if (!pane) return;
  pane.innerHTML = "";
  if (ev.error) {
    pane.appendChild(el("div", "note error", ev.error));
    return;
  }
  if (Array.isArray(ev.sections)) {
    ev.sections.forEach((sec) => {
      const card = el("div", "ck-card");
      card.appendChild(el("div", "ck-toggle", sec.title || ""));
      const body = el("div", "ck-body");
      (sec.items || []).forEach((it) => {
        const row = el("div", "kv");
        row.appendChild(el("span", "kv-k", it.label || ""));
        const val = el("span", "kv-v" + (it.mono ? " mono" : "") + (it.badge === "ok" ? " good" : ""), it.value || "");
        row.appendChild(val);
        body.appendChild(row);
      });
      card.appendChild(body);
      pane.appendChild(card);
    });
    return;
  }
  if (ev.markdown) {
    const box = el("div", "ck-card md");
    box.innerHTML = renderMarkdown(ev.markdown);
    pane.appendChild(box);
    return;
  }
  pane.appendChild(el("pre", "diff", JSON.stringify(ev, null, 2)));
}
$("toggle-inspector").addEventListener("click", () => {
  if ($("app").classList.contains("insp-open")) closeInspector();
  else openInspector(activeInspectorTab);
});
$("close-inspector").addEventListener("click", closeInspector);
document.querySelectorAll(".insp-tab").forEach((b) => b.addEventListener("click", () => switchInspectorTab(b.dataset.tab)));
$("ctx-pill").addEventListener("click", () => openInspector("cockpit"));
$("bal-pill").addEventListener("click", () => openInspector("cockpit"));
$("btw-quick").addEventListener("click", () => openInspector("btw"));

// ---------- Cockpit rendering ----------
function gaugeColor(pct) {
  if (pct >= 85) return "var(--err)";
  if (pct >= 70) return "var(--warn)";
  return "var(--ok)";
}
function onCockpit(ev) {
  cockpitData = ev;
  const enabled = ev && ev.enabled !== false;
  $("ck-disabled").classList.toggle("hidden", enabled);
  $("ck-content").classList.toggle("hidden", !enabled);
  $("ctx-pill").classList.toggle("hidden", !enabled);
  if (!enabled) { $("bal-pill").classList.add("hidden"); return; }

  const ctx = ev.context || {}, cache = ev.cache || {}, perf = ev.perf || {}, bal = ev.balance || {}, usage = ev.usage || {};
  const pct = Math.max(0, Math.min(100, Number(ctx.pct || 0)));
  const color = gaugeColor(pct);

  // Topbar mini context ring
  $("topCtxPct").textContent = Math.round(pct) + "%";
  const mrFg = $("topRingFg");
  mrFg.style.strokeDashoffset = String(56.55 * (1 - pct / 100));
  mrFg.style.stroke = color;
  $("ctx-pill").title = `Context: ${fmtCompact(ctx.used)} / ${fmtCompact(ctx.limit)} (${pct}%) — click for Cockpit`;

  // Topbar balance pill
  if (bal.available && bal.total_credits != null) {
    $("bal-pill").classList.remove("hidden");
    $("topBalVal").textContent = "$" + Number(bal.total_credits).toFixed(2);
  } else {
    $("bal-pill").classList.add("hidden");
  }

  // Card 1: Context gauge + 5-segment popover
  $("ckRingFg").style.strokeDashoffset = String(251.33 * (1 - pct / 100));
  $("ckRingFg").style.stroke = color;
  $("ckCtxPct").textContent = pct.toFixed(pct >= 10 ? 0 : 1) + "%";
  $("ckCtxUsed").textContent = fmtCompact(ctx.used);
  $("ckCtxRatio").textContent = `${fmtCompact(ctx.used)} / ${fmtCompact(ctx.limit)}`;
  $("ckCacheHit").textContent = `${cache.cumulative_hit_pct ?? 0}%`;
  $("ckUncachedIn").textContent = fmtCompact(cache.latest_uncached_in);
  $("ckLatestOut").textContent = fmtCompact(cache.latest_out);
  $("ckTotIn").textContent = fmtCompact(cache.total_uncached_in);
  $("ckTotCache").textContent = fmtCompact(cache.total_cache_read);
  $("ckTotOut").textContent = fmtCompact(cache.total_out);
  $("ckTotTurns").textContent = `${cache.total_calls || 0} / ${ctx.total_tool_calls || 0}`;

  renderContextPopover(ctx);

  // Card 2: Mode sync
  if (ev.mode) syncCockpitMode(ev.mode);

  // Card 3: Latency & speed
  $("ckPerfSamples").textContent = `${perf.samples || 0} calls`;
  $("ckLatLatest").textContent = fmtMs(perf.latest_latency_ms);
  $("ckSpdLatest").textContent = perf.latest_speed_tps != null ? `${perf.latest_speed_tps} tok/s` : "—";
  $("ckLatAvg").textContent = perf.avg_latency_ms != null ? `${fmtMs(perf.avg_latency_ms)} / ${fmtMs(perf.p95_latency_ms)}` : "—";
  $("ckSpdAvg").textContent = perf.avg_speed_tps != null ? `${perf.avg_speed_tps} / ${perf.peak_speed_tps} t/s` : "—";
  renderTrendChart(perf.series || []);

  // Card 4: Balance & spend
  if (bal.available && bal.total_credits != null) {
    $("ckBalTotal").textContent = `$${Number(bal.total_credits).toFixed(2)}`;
    $("ckBalSub").textContent = `充值 $${Number(bal.top_up_credits || 0).toFixed(2)} · 赠金 $${Number(bal.bonus_credits || 0).toFixed(2)}`;
    $("ckBalEst").textContent = bal.est_days_left != null ? `预计可用 ~${bal.est_days_left} 天` : "";
  } else if (bal.configured && bal.error) {
    $("ckBalTotal").textContent = "查询失败";
    $("ckBalSub").textContent = bal.error;
    $("ckBalEst").textContent = "";
  } else {
    $("ckBalTotal").textContent = fmtUsd(usage.last_7d?.cost_usd || 0) + " (7d)";
    $("ckBalSub").textContent = "在 .env 配置 ZENMUX_MANAGEMENT_API_KEY 可显示实时余额";
    $("ckBalEst").textContent = "本地计费";
  }
  $("ckTodaySpend").textContent = fmtUsd(usage.today?.cost_usd || 0);
  $("ckTodayCalls").textContent = `${usage.today?.calls || 0} calls · ${fmtCompact((usage.today?.input_tokens || 0) + (usage.today?.output_tokens || 0))} tok`;
  $("ckWeekSpend").textContent = fmtUsd(usage.last_7d?.cost_usd || 0);
  $("ckWeekCalls").textContent = `${usage.last_7d?.calls || 0} calls · ${fmtCompact((usage.last_7d?.input_tokens || 0) + (usage.last_7d?.output_tokens || 0))} tok`;
  $("ckAllTime").textContent = `累计 ${fmtUsd(usage.all_time?.cost_usd || 0)}`;
  renderSpendVisuals(usage);
}

function renderContextPopover(ctx) {
  const seg = ctx.segments || {};
  const limit = Math.max(1, Number(ctx.limit || 200000));
  $("ckPopTotal").textContent = `${fmtCompact(ctx.used)} / ${fmtCompact(limit)}`;
  const items = [
    ["system", "System prompt", seg.system || 0],
    ["tools", "Tools & outputs", seg.tools || 0],
    ["assistant", "Assistant", seg.assistant || 0],
    ["user", "User messages", seg.user || 0],
    ["free", "Free headroom", seg.free || 0],
  ];
  const stack = $("ckPopStack"); stack.innerHTML = "";
  items.slice(0, 4).forEach(([k, _, v]) => {
    if (v <= 0) return;
    const div = el("div", `pop-seg pop-seg-${k}`);
    div.style.width = `${Math.max(0.5, (v / limit) * 100)}%`;
    stack.appendChild(div);
  });
  const rows = $("ckPopRows"); rows.innerHTML = "";
  items.forEach(([k, label, v]) => {
    const r = el("div", "pop-row");
    const l = el("span", "pop-row-left");
    l.appendChild(el("span", `pop-dot pop-dot-${k}`));
    l.appendChild(el("span", null, label));
    const pct = ((v / limit) * 100).toFixed(1);
    r.appendChild(l);
    r.appendChild(el("span", "mono", `${fmtCompact(v)} (${pct}%)`));
    rows.appendChild(r);
  });
  const toolsBox = $("ckPopTools"); toolsBox.innerHTML = "";
  (ctx.top_tools || []).forEach((t) => toolsBox.appendChild(el("span", "pop-tool-pill", `${t.name} ×${t.count}`)));
}

function renderTrendChart(series) {
  const svg = $("ckTrendSvg");
  const empty = $("ckChartEmpty");
  if (!series || series.length === 0) {
    svg.innerHTML = ""; empty.classList.remove("hidden");
    $("ckChartMax").textContent = ""; $("ckChartSum").textContent = "";
    return;
  }
  empty.classList.add("hidden");
  const key = cockpitChartTab === "latency" ? "latency_s" : cockpitChartTab === "context" ? "context_k" : "speed";
  const unit = cockpitChartTab === "latency" ? "s" : cockpitChartTab === "context" ? "K" : "tok/s";
  const vals = series.map((d) => Number(d[key] || 0));
  const maxV = Math.max(1, ...vals);
  const lastV = vals[vals.length - 1];
  $("ckChartMax").textContent = `max ${maxV.toFixed(1)} ${unit}`;
  $("ckChartSum").textContent = `latest ${lastV.toFixed(1)} ${unit}`;

  const W = 320, H = 96, padT = 22, padB = 8, padX = 8;
  const stepX = vals.length > 1 ? (W - padX * 2) / (vals.length - 1) : 0;
  const pts = vals.map((v, i) => {
    const x = vals.length > 1 ? padX + i * stepX : W / 2;
    const y = padT + (1 - v / maxV) * (H - padT - padB);
    return [x, y];
  });
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${pts[0][0].toFixed(1)},${H - padB} ${line} ${pts[pts.length - 1][0].toFixed(1)},${H - padB}`;
  const dots = pts.map(([x, y]) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.6" fill="var(--accent)"/>`).join("");
  svg.innerHTML =
    `<polygon points="${area}" fill="var(--accent)" opacity="0.14"/>` +
    `<polyline points="${line}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` +
    dots;
}

function renderSpendVisuals(usage) {
  const daily = usage.daily_7d || [];
  const bars = $("ckDailyBars"); bars.innerHTML = "";
  const field = cockpitSpendUnit === "tokens" ? "tokens" : "cost_usd";
  const maxD = Math.max(1e-9, ...daily.map((d) => Number(d[field] || 0)));
  daily.forEach((d) => {
    const col = el("div", "daily-col" + (d.is_today ? " today" : ""));
    const val = Number(d[field] || 0);
    col.title = `${d.date}: ${fmtUsd(d.cost_usd)} · ${fmtCompact(d.tokens)} tok (${d.calls} calls)`;
    const track = el("div", "daily-bar-track");
    const bar = el("div", "daily-bar");
    bar.style.height = val > 0 ? `${Math.max(8, Math.round((val / maxD) * 100))}%` : "2px";
    track.appendChild(bar);
    col.appendChild(track);
    col.appendChild(el("span", "daily-day mono", d.date.slice(3)));
    bars.appendChild(col);
  });

  const models = usage.models_7d || [];
  $("ckModelCnt").textContent = `${models.length} 模型`;
  const mbox = $("ckModelList"); mbox.innerHTML = "";
  if (!models.length) { mbox.appendChild(el("div", "muted", "暂无模型记录")); return; }
  const maxM = Math.max(1e-9, ...models.map((m) => Number(m[field] || 0)));
  models.forEach((m) => {
    const row = el("div", "ck-mrow");
    const top = el("div", "ck-mrow-top");
    top.appendChild(el("span", "ck-mname mono", m.model));
    const vText = field === "tokens" ? fmtCompact(m.tokens) : fmtUsd(m.cost_usd);
    top.appendChild(el("span", "mono muted", vText));
    const bar = el("div", "ck-mbar");
    const fill = el("div", "ck-mfill");
    fill.style.width = `${Math.max(3, Math.round((Number(m[field] || 0) / maxM) * 100))}%`;
    bar.appendChild(fill);
    row.appendChild(top); row.appendChild(bar);
    mbox.appendChild(row);
  });
}

function syncCockpitMode(mode) {
  const b = $("ckModeBadge");
  if (b) { b.textContent = mode.toUpperCase(); b.dataset.mode = mode; }
  $("ckModeDesc").textContent = MODE_DESC[mode] || "";
  document.querySelectorAll("#ckModeSeg .seg-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
}
function syncCockpitEffort(effort) {
  const b = $("ckEffortBadge");
  if (b) { b.textContent = effort.toUpperCase(); b.dataset.effort = effort; }
  document.querySelectorAll("#ckEffortSeg .seg-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.effort === effort));
}

// Cockpit controls wiring
$("ckRingTrigger").addEventListener("click", () => $("ckRingAnchor").classList.toggle("pinned"));
document.addEventListener("click", (e) => {
  if (!$("ckRingAnchor").contains(e.target)) $("ckRingAnchor").classList.remove("pinned");
});
$("ckLimitSelect").addEventListener("change", (e) => send({ type: "set_context_limit", limit: Number(e.target.value) }));
$("ckCompactBtn").addEventListener("click", () => send({ type: "compact", focus: "" }));
$("ckRefreshBal").addEventListener("click", () => send({ type: "refresh_balance" }));
document.querySelectorAll("#ckModeSeg .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const m = btn.dataset.mode;
    if (m && m !== activeMode) { updateModeUI(m); send({ type: "set_mode", mode: m }); }
  });
});
document.querySelectorAll("#ckEffortSeg .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const ef = btn.dataset.effort;
    if (ef && ef !== activeEffort) { updateEffortUI(ef); send({ type: "set_effort", effort: ef }); }
  });
});
document.querySelectorAll(".ck-pill").forEach((btn) => {
  btn.addEventListener("click", () => {
    const cmd = btn.dataset.qcmd || "";
    if (cmd === "/btw") { openInspector("btw"); return; }
    if (cmd === "/diff") { openInspector("changes"); return; }
    if (cmd === "/compact" || cmd === "/export") { send({ type: "slash", line: cmd }); return; }
    $("input").value = cmd + " "; $("input").focus();
  });
});
document.querySelectorAll("#ckChartTabs .ck-tab").forEach((b) => {
  b.addEventListener("click", () => {
    cockpitChartTab = b.dataset.chart;
    document.querySelectorAll("#ckChartTabs .ck-tab").forEach((x) => x.classList.toggle("active", x === b));
    if (cockpitData?.perf) renderTrendChart(cockpitData.perf.series || []);
  });
});
document.querySelectorAll("#ckSpendUnit .ck-tab").forEach((b) => {
  b.addEventListener("click", () => {
    cockpitSpendUnit = b.dataset.unit;
    document.querySelectorAll("#ckSpendUnit .ck-tab").forEach((x) => x.classList.toggle("active", x === b));
    if (cockpitData?.usage) renderSpendVisuals(cockpitData.usage);
  });
});
document.querySelectorAll("[data-ck-toggle]").forEach((btn) => {
  const key = btn.dataset.ckToggle;
  const card = btn.closest(".ck-card");
  try { if (localStorage.getItem("codelet_ck_" + key) === "1") card.classList.add("collapsed"); } catch (_) {}
  btn.addEventListener("click", () => {
    const col = card.classList.toggle("collapsed");
    try { localStorage.setItem("codelet_ck_" + key, col ? "1" : "0"); } catch (_) {}
  });
});

// ---------- Side /btw panel ----------
function clearBtwUI() {
  btwItems.clear();
  $("btw-messages").innerHTML =
    '<div class="btw-empty"><p><b>/btw 独立侧栏提问</b></p>' +
    '<p class="muted">随时顺嘴问旁枝问题，绝不污染主对话上下文；即使主任务正在运行中也可以并发提问。</p></div>';
}
function onBtwStart(ev) {
  const box = $("btw-messages");
  box.querySelector(".btw-empty")?.remove();
  const u = el("div", "btw-item user");
  u.appendChild(el("div", "btw-bub", ev.question));
  box.appendChild(u);
  const a = el("div", "btw-item assistant");
  const bub = el("div", "btw-bub md", "…");
  a.appendChild(bub);
  box.appendChild(a);
  btwItems.set(ev.id, { bub, text: "" });
  box.scrollTop = box.scrollHeight;
}
function onBtwDelta(ev) {
  const slot = btwItems.get(ev.id);
  if (!slot) return;
  slot.text += ev.text;
  slot.bub.innerHTML = renderMarkdown(slot.text);
  wireCodeCopies(slot.bub);
  const box = $("btw-messages");
  box.scrollTop = box.scrollHeight;
}
function onBtwDone(ev) {
  const slot = btwItems.get(ev.id);
  if (!slot) return;
  if (ev.text && !slot.text) {
    slot.text = ev.text;
    slot.bub.innerHTML = renderMarkdown(slot.text);
    wireCodeCopies(slot.bub);
  }
}
function onBtwError(ev) {
  const slot = btwItems.get(ev.id);
  if (slot) slot.bub.textContent = "Error: " + (ev.message || "failed");
}
function submitBtw() {
  const q = $("btw-input").value.trim();
  if (!q) return;
  $("btw-input").value = "";
  send({ type: "btw_ask", text: q, include_context: $("btw-ctx").checked });
}
$("btw-form").addEventListener("submit", (e) => { e.preventDefault(); submitBtw(); });
$("btw-input").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitBtw(); }
});
$("btw-clear").addEventListener("click", () => send({ type: "btw_clear" }));

// ---------- Live Tasks (TodoWrite) ----------
function renderTodos(todos) {
  const list = Array.isArray(todos) ? todos : [];
  const done = list.filter((t) => t.status === "completed").length;
  const active = list.find((t) => t.status === "in_progress");
  $("tab-tasks-cnt").textContent = list.length ? `${done}/${list.length}` : "0";
  $("tasks-progress-lbl").textContent = list.length ? `${done} of ${list.length} completed` : "No active checklist yet";
  $("tasks-fill").style.width = list.length ? `${Math.round((done / list.length) * 100)}%` : "0%";

  // Sticky compact bar above composer
  const bar = $("todo-bar");
  if (!list.length) {
    bar.classList.add("hidden");
  } else {
    bar.classList.remove("hidden");
    $("tb-text").textContent = active ? (active.activeForm || active.content) : (done === list.length ? "All tasks completed" : list[0].content);
    $("tb-count").textContent = `${done}/${list.length}`;
  }

  const ul = $("tasks-list"); ul.innerHTML = "";
  if (!list.length) { ul.appendChild(el("li", "muted", "When the agent plans multi-step work via todo_write, steps appear live here.")); return; }
  list.forEach((t) => {
    const li = el("li", `task-row ${t.status || "pending"}`);
    const ic = el("span", "task-st");
    if (t.status === "completed") ic.innerHTML = SVG_CHECK;
    else if (t.status === "in_progress") ic.textContent = "●";
    else ic.textContent = "○";
    li.appendChild(ic);
    li.appendChild(el("span", null, t.status === "in_progress" && t.activeForm ? t.activeForm : t.content));
    ul.appendChild(li);
  });
}
$("todo-bar").addEventListener("click", () => openInspector("tasks"));

// ---------- Git Changes & Diff ----------
async function loadGitChanges() {
  const data = await safe("/api/git/changes");
  const ul = $("git-files"); ul.innerHTML = "";
  if (!data || !data.is_git) {
    $("git-branch").textContent = "not a git repo";
    $("tab-changes-cnt").textContent = "0";
    ul.appendChild(el("li", "muted", "Current workspace is not a Git repository."));
    $("git-diff-wrap").classList.add("hidden");
    return;
  }
  $("git-branch").textContent = data.branch || "HEAD";
  const files = data.files || [];
  $("tab-changes-cnt").textContent = String(files.length);
  if (!files.length) {
    ul.appendChild(el("li", "muted", "Working tree clean — no modified files."));
    $("git-diff-wrap").classList.add("hidden");
    return;
  }
  files.forEach((f) => {
    const li = el("li", "git-file" + (f.path === activeDiffPath ? " active" : ""));
    li.appendChild(el("span", `gf-code ${f.status}`, f.code));
    li.appendChild(el("span", "gf-path", f.path));
    const stat = el("span", "gf-stat");
    if (f.added) stat.appendChild(el("span", "gf-add", `+${f.added}`));
    if (f.deleted) stat.appendChild(el("span", "gf-del", `-${f.deleted}`));
    li.appendChild(stat);
    li.onclick = () => openGitDiff(f.path);
    ul.appendChild(li);
  });
}
async function openGitDiff(path) {
  activeDiffPath = path;
  document.querySelectorAll(".git-file").forEach((li) => li.classList.toggle("active", li.querySelector(".gf-path")?.textContent === path));
  const r = await safe("/api/git/diff?path=" + encodeURIComponent(path));
  $("git-diff-path").textContent = path;
  const pre = $("git-diff-pre"); pre.innerHTML = "";
  ((r && r.diff) || "(no diff)").split("\n").forEach((line) => {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    pre.appendChild(el("span", cls, line + "\n"));
  });
  $("git-diff-wrap").classList.remove("hidden");
}
$("git-refresh").addEventListener("click", loadGitChanges);
$("git-diff-close").addEventListener("click", () => { activeDiffPath = ""; $("git-diff-wrap").classList.add("hidden"); });

// ---------- Ctrl+K Global Command Palette ----------
function buildPaletteActions() {
  const acts = [
    { name: "Switch model…", desc: activeModel, cat: "model", run: () => openModelModal() },
    { name: "Refresh model list", desc: "Fetch latest models from endpoint", cat: "model", run: () => triggerModelRefresh() },
    { name: "Open Cockpit", desc: "Context gauge, speed, cache & ZenMux balance", cat: "panel", run: () => openInspector("cockpit") },
    { name: "Open Side /btw", desc: "Ask a side question without polluting main context", cat: "panel", run: () => openInspector("btw") },
    { name: "Open Tasks", desc: "View live todo checklist", cat: "panel", run: () => openInspector("tasks") },
    { name: "Open Git Changes", desc: "Inspect modified files & diffs", cat: "panel", run: () => openInspector("changes") },
    { name: "New conversation", desc: "Clear messages and start fresh", cat: "chat", run: () => send({ type: "new_conversation" }) },
    { name: "Compact context (/compact)", desc: "Summarize middle turns to free context window", cat: "chat", run: () => send({ type: "compact", focus: "" }) },
    { name: "Export conversation (/export)", desc: "Download current chat as Markdown", cat: "chat", run: () => send({ type: "slash", line: "/export" }) },
    { name: "Change workspace…", desc: $("ws-chip").title || "", cat: "env", run: () => { $("ws-modal").classList.remove("hidden"); browseTo(""); } },
    { name: "Toggle Light / Dark theme", desc: "Switch UI color scheme", cat: "ui", run: () => $("theme-toggle").click() },
  ];
  ["normal", "ask", "plan", "auto"].forEach((m) => {
    acts.push({ name: `Mode: ${m}`, desc: MODE_DESC[m], cat: "mode", run: () => { updateModeUI(m); send({ type: "set_mode", mode: m }); } });
  });
  ["auto", "off", "low", "medium", "high"].forEach((ef) => {
    acts.push({ name: `Thinking: ${ef}`, desc: EFFORT_DESC[ef], cat: "think", run: () => { updateEffortUI(ef); send({ type: "set_effort", effort: ef }); } });
  });
  availableCommands.forEach((c) => {
    acts.push({ name: `/${c.name}`, desc: c.description || "", cat: c.source || "slash", run: () => { $("input").value = `/${c.name} `; $("input").focus(); } });
  });
  return acts;
}
function renderCmdPalette() {
  const q = ($("cmd-input").value || "").trim().toLowerCase();
  const list = buildPaletteActions().filter((a) => !q || a.name.toLowerCase().includes(q) || (a.desc || "").toLowerCase().includes(q) || a.cat.includes(q));
  const ul = $("cmd-list"); ul.innerHTML = "";
  if (!list.length) { ul.appendChild(el("li", "m-empty", "No matching actions.")); return; }
  cmdIdx = Math.min(cmdIdx, list.length - 1);
  list.slice(0, 14).forEach((a, i) => {
    const li = el("li", "cmd-row" + (i === cmdIdx ? " active" : ""));
    li.appendChild(el("span", "cmd-name", a.name));
    li.appendChild(el("span", "cmd-desc", a.desc || ""));
    li.appendChild(el("span", "cmd-cat", a.cat));
    li.onclick = () => { $("cmd-modal").classList.add("hidden"); a.run(); };
    ul.appendChild(li);
  });
}
function openCmdPalette() {
  cmdIdx = 0;
  $("cmd-input").value = "";
  $("cmd-modal").classList.remove("hidden");
  renderCmdPalette();
  $("cmd-input").focus();
}
$("cmd-palette-btn").addEventListener("click", openCmdPalette);
$("cmd-modal").addEventListener("click", (e) => { if (e.target === $("cmd-modal")) $("cmd-modal").classList.add("hidden"); });
$("cmd-input").addEventListener("input", () => { cmdIdx = 0; renderCmdPalette(); });
$("cmd-input").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  const rows = [...$("cmd-list").querySelectorAll(".cmd-row")];
  if (e.key === "ArrowDown" && rows.length) { e.preventDefault(); cmdIdx = (cmdIdx + 1) % rows.length; renderCmdPalette(); }
  else if (e.key === "ArrowUp" && rows.length) { e.preventDefault(); cmdIdx = (cmdIdx - 1 + rows.length) % rows.length; renderCmdPalette(); }
  else if (e.key === "Enter" && rows[cmdIdx]) { e.preventDefault(); rows[cmdIdx].click(); }
  else if (e.key === "Escape") { $("cmd-modal").classList.add("hidden"); }
});

// ---------- mode & model UI ----------
function updateModeUI(mode) {
  if (!MODE_ICONS[mode]) return;
  activeMode = mode;
  $("mode-label").textContent = mode;
  $("mode-icon").innerHTML = MODE_ICONS[mode];
  $("mode-chip").dataset.mode = mode;
  [...$("mode-menu").querySelectorAll(".pop-item")].forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  syncCockpitMode(mode);
}
function closeModeMenu() {
  $("mode-menu").classList.add("hidden");
  $("mode-chip").setAttribute("aria-expanded", "false");
}
function updateEffortUI(effort) {
  if (!EFFORT_DESC[effort]) return;
  activeEffort = effort;
  $("effort-label").textContent = "Think: " + (effort === "medium" ? "med" : effort);
  $("effort-chip").dataset.effort = effort;
  [...$("effort-menu").querySelectorAll(".pop-item")].forEach((b) => {
    const on = b.dataset.effort === effort;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  syncCockpitEffort(effort);
}
function closeEffortMenu() {
  $("effort-menu").classList.add("hidden");
  $("effort-chip").setAttribute("aria-expanded", "false");
}
function onProfile(ev) {
  if (ev.user) {
    $("user").textContent = ev.user;
    $("user-avatar").textContent = (ev.user[0] || "?").toUpperCase();
  }
  if (ev.mode) updateModeUI(ev.mode);
  if (ev.reasoning_effort) updateEffortUI(ev.reasoning_effort);
  if (ev.model) activeModel = ev.model;
  if (Array.isArray(ev.model_catalog) && ev.model_catalog.length) modelCatalog = ev.model_catalog;
  else if (Array.isArray(ev.models)) modelCatalog = ev.models.map((id) => ({ id, owned_by: id.includes("/") ? id.split("/")[0] : "" }));
  if (ev.models_meta) modelsMeta = ev.models_meta;
  setRefreshing(false);
  updateModelChip();
  if (!$("model-modal").classList.contains("hidden")) renderModelPicker();
}
function fmtCtx(n) {
  if (!n) return "";
  if (n >= 1e6) { const v = n / 1e6; return (Number.isInteger(v) ? v : v.toFixed(1)) + "M"; }
  if (n >= 1e3) return Math.round(n / 1e3) + "K";
  return String(n);
}
function fmtPrice(p) {
  if (!p || p.input == null || p.output == null) return "";
  return `$${Number(p.input)}/$${Number(p.output)}`;
}
function fmtAge(ts) {
  if (!ts) return "";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return "just now";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
  return Math.floor(sec / 86400) + "d ago";
}
function fmtSessionTime(raw) {
  if (!raw) return "";
  const iso = /[Zz]|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw + "Z";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return raw;
  const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (sec < 60) return "just now";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function modelMetaText() {
  const n = modelCatalog.length;
  const parts = [`${n} model${n === 1 ? "" : "s"}`];
  if (modelsMeta.source) parts.push(modelsMeta.source);
  if (modelsMeta.fetched_at) parts.push("updated " + fmtAge(modelsMeta.fetched_at));
  return parts.join(" · ");
}
function updateModelChip() {
  $("model-name").textContent = activeModel || "—";
  $("model-chip").title = (activeModel ? activeModel + " — " : "") + modelMetaText();
  $("model-refresh").title = "Refresh models (" + modelMetaText() + ")";
  const metaEl = $("model-meta");
  if (metaEl) metaEl.textContent = modelMetaText();
}
function setRefreshing(on) {
  refreshingModels = on;
  $("model-refresh").classList.toggle("spinning", on);
  $("model-refresh").disabled = on;
  const btn = $("model-modal-refresh");
  if (btn) {
    btn.disabled = on; btn.classList.toggle("spinning", on);
    const lbl = btn.querySelector("span");
    if (lbl) lbl.textContent = on ? "Refreshing…" : "Refresh";
  }
}
function triggerModelRefresh() {
  if (refreshingModels || !ws || ws.readyState !== 1) return;
  setRefreshing(true);
  send({ type: "refresh_models" });
  setTimeout(() => setRefreshing(false), 18000);
}
function selectModel(id) {
  if (!id) return;
  activeModel = id;
  updateModelChip();
  $("model-modal").classList.add("hidden");
  send({ type: "set_model", model: id });
}
function openModelModal() {
  closeModeMenu();
  $("model-modal").classList.remove("hidden");
  $("model-search").value = "";
  renderModelPicker();
  $("model-search").focus();
}
function renderModelPicker() {
  updateModelChip();
  const q = ($("model-search").value || "").trim().toLowerCase();
  const hasPinned = modelCatalog.some((m) => m.pinned);
  const vendorCounts = new Map();
  modelCatalog.forEach((m) => {
    const v = (m.owned_by || (m.id.includes("/") ? m.id.split("/")[0] : "other")).toLowerCase();
    vendorCounts.set(v, (vendorCounts.get(v) || 0) + 1);
  });
  const vendors = [...vendorCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

  const tabBox = $("model-vendors"); tabBox.innerHTML = "";
  const tabs = [["all", `All (${modelCatalog.length})`]];
  if (hasPinned) tabs.push(["pinned", "Pinned"]);
  vendors.slice(0, 12).forEach(([v, cnt]) => tabs.push([v, `${v} (${cnt})`]));
  if (activeVendor !== "all" && !tabs.some(([k]) => k === activeVendor)) activeVendor = "all";
  tabs.forEach(([key, label]) => {
    const b = el("button", "vtab" + (activeVendor === key ? " active" : ""), label);
    b.type = "button"; b.onclick = () => { activeVendor = key; renderModelPicker(); };
    tabBox.appendChild(b);
  });

  const filtered = modelCatalog.filter((m) => {
    const v = (m.owned_by || (m.id.includes("/") ? m.id.split("/")[0] : "other")).toLowerCase();
    if (activeVendor === "pinned" && !m.pinned) return false;
    if (activeVendor !== "all" && activeVendor !== "pinned" && v !== activeVendor) return false;
    if (!q) return true;
    return m.id.toLowerCase().includes(q) || (m.display_name || "").toLowerCase().includes(q) || v.includes(q);
  });

  const ul = $("model-list"); ul.innerHTML = "";
  filtered.forEach((m) => {
    const li = el("li", "m-row" + (m.id === activeModel ? " active" : ""));
    const left = el("div", "m-main");
    left.appendChild(el("div", "m-id", m.id));
    if (m.display_name && m.display_name.toLowerCase() !== m.id.toLowerCase()) left.appendChild(el("div", "m-sub", m.display_name));
    const right = el("div", "m-tags");
    if (m.pinned) right.appendChild(el("span", "m-tag pin", "pinned"));
    const ctx = fmtCtx(m.context_length); if (ctx) right.appendChild(el("span", "m-tag", ctx));
    const price = fmtPrice(m.pricing); if (price) right.appendChild(el("span", "m-tag", price));
    if (m.vision) right.appendChild(el("span", "m-tag cap", "vision"));
    if (m.reasoning) right.appendChild(el("span", "m-tag cap", "reasoning"));
    li.appendChild(left); li.appendChild(right);
    li.onclick = () => selectModel(m.id);
    ul.appendChild(li);
  });

  const rawQ = ($("model-search").value || "").trim();
  if (rawQ && !modelCatalog.some((m) => m.id === rawQ)) {
    const custom = el("li", "m-row custom");
    custom.appendChild(el("div", "m-id", `Use custom model "${rawQ}"`));
    custom.onclick = () => selectModel(rawQ);
    ul.appendChild(custom);
  } else if (!filtered.length) {
    ul.appendChild(el("li", "m-empty", "No matching models."));
  }
}
function onWorkspace(ev) { $("ws-name").textContent = ev.name || ev.cwd; $("ws-chip").title = ev.cwd; }
function onResumed(ev) {
  transcript.innerHTML = ""; current = null;
  (ev.messages || []).forEach((m) => {
    const b = addMessage(m.role);
    b.text = m.text || "";
    if (m.role === "assistant") setAssistantMarkdown(b);
    else b.bubble.textContent = b.text;
    (m.tools || []).forEach((name) => {
      const card = el("div", "tool");
      const head = el("div", "head");
      head.appendChild(el("span", "tname", name));
      head.appendChild(el("span", "badge ok", "done"));
      card.appendChild(head); b.body.appendChild(card);
    });
  });
  current = null; toggleEmpty(); scroll();
}

// ---------- confirm & diff modals ----------
function askConfirm({ title, message, okText = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    $("confirm-title").textContent = title;
    $("confirm-msg").textContent = message;
    const okBtn = $("confirm-ok");
    okBtn.textContent = okText;
    okBtn.className = "btn " + (danger ? "danger" : "primary");
    const modal = $("confirm-modal"); modal.classList.remove("hidden");
    const finish = (val) => {
      modal.classList.add("hidden");
      okBtn.onclick = null; $("confirm-cancel").onclick = null; modal.onclick = null;
      resolve(val);
    };
    okBtn.onclick = () => finish(true);
    $("confirm-cancel").onclick = () => finish(false);
    modal.onclick = (e) => { if (e.target === modal) finish(false); };
  });
}
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

// ---------- socket & busy/stop button ----------
function setConnState(state) {
  const dot = $("conn-dot");
  if (!dot) return;
  dot.className = "dot" + (state === "on" ? " on" : state === "err" ? " err" : "");
  dot.title = state === "on" ? "Connected" : state === "err" ? "Disconnected" : "Reconnecting…";
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function connect() {
  setConnState("wait");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setConnState("on");
  ws.onclose = () => { setConnState("wait"); setTimeout(connect, 1500); };
  ws.onerror = () => setConnState("err");
  ws.onmessage = (e) => render(JSON.parse(e.data));
}
function setBusy(b) {
  busy = b;
  const btn = $("send");
  btn.classList.toggle("stop", b);
  btn.innerHTML = b ? SVG_STOP : SVG_SEND;
  btn.title = b ? "Stop current turn" : "Send";
}

// ---------- theme & sidebar ----------
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "dark" || theme === "light") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
  const isDark = theme === "dark" || (!theme && window.matchMedia("(prefers-color-scheme: dark)").matches);
  $("theme-toggle").innerHTML = isDark ? SVG_SUN : SVG_MOON;
  $("theme-toggle").title = isDark ? "Switch to light theme" : "Switch to dark theme";
}
$("theme-toggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const isDark = cur === "dark" || (!cur && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const next = isDark ? "light" : "dark";
  try { localStorage.setItem("codelet_theme", next); } catch (_) {}
  applyTheme(next);
});
$("toggle-sidebar").addEventListener("click", () => {
  const collapsed = $("app").classList.toggle("side-collapsed");
  try { localStorage.setItem("codelet_side_collapsed", collapsed ? "1" : "0"); } catch (_) {}
});

// ---------- draggable pane resizers ----------
const DEFAULT_SIDE_W = 284, DEFAULT_INSP_W = 428;
function setPaneWidth(varName, px, storageKey) {
  const app = $("app");
  app.style.setProperty(varName, Math.round(px) + "px");
  if (storageKey) { try { localStorage.setItem(storageKey, String(Math.round(px))); } catch (_) {} }
}
function wireResizer(elId, { side, varName, storageKey, minW, maxW, defaultW }) {
  const handle = $(elId);
  if (!handle) return;
  handle.addEventListener("dblclick", () => setPaneWidth(varName, defaultW, storageKey));
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const app = $("app");
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("active");
    app.classList.add("resizing");

    const onMove = (ev) => {
      const vw = window.innerWidth;
      const otherW = side === "left"
        ? (app.classList.contains("insp-open") ? parseFloat(getComputedStyle(app).getPropertyValue("--insp-w")) || DEFAULT_INSP_W : 0)
        : (app.classList.contains("side-collapsed") ? 0 : parseFloat(getComputedStyle(app).getPropertyValue("--side-w")) || DEFAULT_SIDE_W);
      const availMax = Math.max(minW, Math.min(maxW, vw - otherW - 360));
      const raw = side === "left" ? ev.clientX : (vw - ev.clientX);
      const clamped = Math.max(minW, Math.min(availMax, raw));
      app.style.setProperty(varName, Math.round(clamped) + "px");
    };
    const onUp = () => {
      handle.classList.remove("active");
      app.classList.remove("resizing");
      const finalW = parseFloat(getComputedStyle(app).getPropertyValue(varName)) || defaultW;
      try { localStorage.setItem(storageKey, String(Math.round(finalW))); } catch (_) {}
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });
}
wireResizer("resizer-left", { side: "left", varName: "--side-w", storageKey: "codelet_side_w", minW: 200, maxW: 520, defaultW: DEFAULT_SIDE_W });
wireResizer("resizer-right", { side: "right", varName: "--insp-w", storageKey: "codelet_insp_w", minW: 300, maxW: 720, defaultW: DEFAULT_INSP_W });
["nav-sessions", "nav-plugins", "nav-tools", "nav-skills"].forEach((id) => {
  const d = $(id);
  if (!d) return;
  try {
    const saved = localStorage.getItem("codelet_" + id);
    if (saved === "1") d.open = true;
    else if (saved === "0") d.open = false;
  } catch (_) {}
  d.addEventListener("toggle", () => { try { localStorage.setItem("codelet_" + id, d.open ? "1" : "0"); } catch (_) {} });
});
scroller.addEventListener("scroll", () => {
  const far = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 160;
  $("scroll-bottom").classList.toggle("hidden", !far);
});
$("scroll-bottom").addEventListener("click", () => scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }));

// ---------- uploads / paste / drag-drop ----------
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
$("input").addEventListener("paste", (e) => {
  const items = [...(e.clipboardData?.items || [])];
  const files = items.filter((it) => it.kind === "file").map((it) => it.getAsFile()).filter(Boolean);
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer?.types?.includes("Files")) return;
  e.preventDefault(); dragDepth++; $("drop-overlay").classList.remove("hidden");
});
window.addEventListener("dragover", (e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); });
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (dragDepth === 0) $("drop-overlay").classList.add("hidden"); });
window.addEventListener("drop", (e) => {
  dragDepth = 0; $("drop-overlay").classList.add("hidden");
  if (e.dataTransfer?.files?.length) { e.preventDefault(); uploadFiles(e.dataTransfer.files); }
});

// ---------- slash menu ----------
function getMatchingCommands(val) {
  if (!val.startsWith("/") || /\s/.test(val)) return [];
  const q = val.slice(1).toLowerCase();
  return availableCommands.filter((c) => c.name.toLowerCase().includes(q));
}
function hideSlashMenu() { $("slash-menu").classList.add("hidden"); }
function renderSlashMenu() {
  const list = getMatchingCommands($("input").value);
  const menu = $("slash-menu");
  if (!list.length) { menu.classList.add("hidden"); return; }
  slashIdx = Math.min(slashIdx, list.length - 1);
  menu.innerHTML = "";
  list.forEach((c, i) => {
    const b = el("button", "slash-item" + (i === slashIdx ? " active" : ""));
    b.type = "button";
    b.appendChild(el("span", "s-cmd", "/" + c.name));
    b.appendChild(el("span", "s-desc", c.description || ""));
    if (c.source) b.appendChild(el("span", "s-src", c.source));
    b.onclick = () => { $("input").value = "/" + c.name + " "; hideSlashMenu(); $("input").focus(); };
    menu.appendChild(b);
  });
  menu.classList.remove("hidden");
}

// ---------- composer ----------
function submit() {
  if (busy) { send({ type: "cancel" }); return; }
  const text = $("input").value.trim();
  if (!text && !attachments.length) return;
  hideSlashMenu();

  // `/btw` runs in the isolated Side panel without polluting main transcript or busy lock.
  if (/^\/btw(?:\s|$)/i.test(text) && !attachments.length) {
    const q = text.replace(/^\/btw\s*/i, "").trim();
    $("input").value = ""; $("input").style.height = "auto";
    openInspector("btw");
    if (q) send({ type: "btw_ask", text: q, include_context: $("btw-ctx").checked });
    return;
  }

  const atts = attachments.slice();
  const isSlash = text.startsWith("/") && !atts.length;
  if (!isSlash) {
    const b = addMessage("user");
    b.text = text || "(sent attachments)";
    b.bubble.textContent = b.text;
    if (atts.length) {
      const box = el("div", "att");
      atts.forEach((a) => {
        if (a.is_image && a.url) { const im = document.createElement("img"); im.src = a.url; box.appendChild(im); }
        else box.appendChild(el("span", "file", a.name));
      });
      b.body.appendChild(box);
    }
  }
  $("input").value = ""; $("input").style.height = "auto"; attachments = []; renderAttachments();
  current = null;
  if (isSlash) {
    send({ type: "slash", line: text });
  } else {
    setBusy(true);
    send({ type: "prompt", text, attachments: atts.map((a) => ({ name: a.name, path: a.path, is_image: a.is_image })) });
  }
  scroll();
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
$("input").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  const menuOpen = !$("slash-menu").classList.contains("hidden");
  if (menuOpen) {
    const items = [...$("slash-menu").querySelectorAll(".slash-item")];
    if (e.key === "ArrowDown") { e.preventDefault(); slashIdx = (slashIdx + 1) % items.length; renderSlashMenu(); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); slashIdx = (slashIdx - 1 + items.length) % items.length; renderSlashMenu(); return; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
      if (items[slashIdx]) { e.preventDefault(); items[slashIdx].click(); return; }
    }
    if (e.key === "Escape") { e.preventDefault(); hideSlashMenu(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!busy) submit();
  }
});
$("input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
  slashIdx = 0; renderSlashMenu();
});

document.querySelectorAll(".starter").forEach((btn) => {
  btn.addEventListener("click", () => { $("input").value = btn.dataset.prompt || ""; $("input").focus(); submit(); });
});

$("mode-chip").addEventListener("click", (e) => {
  e.stopPropagation();
  closeEffortMenu();
  const open = $("mode-menu").classList.toggle("hidden") === false;
  $("mode-chip").setAttribute("aria-expanded", open ? "true" : "false");
});
[...$("mode-menu").querySelectorAll(".pop-item")].forEach((b) => {
  b.addEventListener("click", () => {
    const m = b.dataset.mode; closeModeMenu();
    if (m && m !== activeMode) { updateModeUI(m); send({ type: "set_mode", mode: m }); }
  });
});
$("effort-chip").addEventListener("click", (e) => {
  e.stopPropagation();
  closeModeMenu();
  const open = $("effort-menu").classList.toggle("hidden") === false;
  $("effort-chip").setAttribute("aria-expanded", open ? "true" : "false");
});
[...$("effort-menu").querySelectorAll(".pop-item")].forEach((b) => {
  b.addEventListener("click", () => {
    const ef = b.dataset.effort; closeEffortMenu();
    if (ef && ef !== activeEffort) { updateEffortUI(ef); send({ type: "set_effort", effort: ef }); }
  });
});
document.addEventListener("click", (e) => {
  if (!$("mode-wrap").contains(e.target)) closeModeMenu();
  if (!$("effort-wrap").contains(e.target)) closeEffortMenu();
  if (!$("composer").contains(e.target)) hideSlashMenu();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if ($("cmd-modal").classList.contains("hidden")) openCmdPalette();
    else $("cmd-modal").classList.add("hidden");
    return;
  }
  if (e.key === "Escape") {
    closeModeMenu(); closeEffortMenu(); hideSlashMenu();
    $("model-modal").classList.add("hidden");
    $("ws-modal").classList.add("hidden");
    $("cmd-modal").classList.add("hidden");
  }
});
$("model-chip").addEventListener("click", openModelModal);
$("model-refresh").addEventListener("click", triggerModelRefresh);
$("model-modal-refresh").addEventListener("click", triggerModelRefresh);
$("model-close").addEventListener("click", () => $("model-modal").classList.add("hidden"));
$("model-modal").addEventListener("click", (e) => { if (e.target === $("model-modal")) $("model-modal").classList.add("hidden"); });
$("model-search").addEventListener("input", renderModelPicker);
$("model-search").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Escape") { $("model-modal").classList.add("hidden"); return; }
  if (e.key === "Enter") { e.preventDefault(); const first = $("model-list").querySelector(".m-row"); if (first) first.click(); }
});
$("new-chat").addEventListener("click", () => send({ type: "new_conversation" }));

// ---------- workspace picker ----------
let wsCurrent = "";
async function browseTo(path) {
  let r; try { r = await (await fetch("/api/browse?path=" + encodeURIComponent(path || ""))).json(); } catch (_) { return; }
  wsCurrent = r.is_root ? "" : r.path;
  $("ws-cur").textContent = r.path; $("ws-input").value = r.is_root ? "" : r.path;
  const ul = $("ws-dirs"); ul.innerHTML = "";
  if (r.parent) {
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
$("ws-modal").addEventListener("click", (e) => { if (e.target === $("ws-modal")) $("ws-modal").classList.add("hidden"); });
$("ws-go").addEventListener("click", () => browseTo($("ws-input").value.trim()));
$("ws-input").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); browseTo($("ws-input").value.trim()); } });
$("ws-open").addEventListener("click", () => {
  const path = $("ws-input").value.trim() || wsCurrent;
  if (!path) return;
  $("ws-modal").classList.add("hidden");
  send({ type: "set_workspace", path });
});

// ---------- left sidebar lists ----------
async function safe(url) { try { return await (await fetch(url)).json(); } catch (_) { return null; } }
function startInlineRename(li, r) {
  const titleEl = li.querySelector(".title");
  if (!titleEl || li.querySelector(".s-rename-input")) return;
  const orig = r.title || r.summary || "";
  const inp = el("input", "s-rename-input"); inp.type = "text"; inp.value = orig;
  titleEl.replaceWith(inp); inp.focus(); inp.select();
  let done = false;
  const finish = (save) => {
    if (done) return; done = true;
    const next = inp.value.trim();
    inp.replaceWith(el("span", "title", (save && next) ? next : (orig || "(untitled)")));
    if (save && next && next !== orig) send({ type: "rename_session", id: r.id, title: next });
  };
  inp.onclick = (e) => e.stopPropagation();
  inp.onkeydown = (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  inp.onblur = () => finish(true);
}
async function loadSessions() {
  const s = await safe("/api/sessions");
  const ul = $("sessions"); ul.innerHTML = "";
  const rows = (s && s.project) || [];
  $("cnt-sessions").textContent = String(rows.length);
  if (!rows.length) { ul.appendChild(el("li", "meta", "(no conversations here yet)")); return; }
  rows.forEach((r) => {
    const li = el("li", "session");
    const main = el("div", "s-main");
    main.appendChild(el("span", "title", r.title || r.summary || "(untitled)"));
    const when = fmtSessionTime(r.updated_at);
    const metaEl = el("span", "meta", `${when}${when ? " · " : ""}${r.message_count ?? "?"} msgs`);
    if (r.updated_at) metaEl.title = r.updated_at;
    main.appendChild(metaEl);
    main.onclick = () => { activeSession = r.id; send({ type: "resume", id: r.id }); highlight(); };
    const acts = el("div", "s-acts");
    const rename = el("button", "s-btn"); rename.type = "button"; rename.innerHTML = SVG_PENCIL; rename.setAttribute("aria-label", "Rename conversation");
    rename.onclick = (e) => { e.stopPropagation(); startInlineRename(li, r); };
    const del = el("button", "s-btn danger"); del.type = "button"; del.innerHTML = SVG_TRASH; del.setAttribute("aria-label", "Delete conversation");
    del.onclick = async (e) => {
      e.stopPropagation();
      const ok = await askConfirm({ title: "Delete conversation?", message: `Remove "${r.title || r.summary || "(untitled)"}" permanently?`, okText: "Delete", danger: true });
      if (ok) send({ type: "delete_session", id: r.id });
    };
    acts.appendChild(rename); acts.appendChild(del);
    li.appendChild(main); li.appendChild(acts);
    li._id = r.id; ul.appendChild(li);
  });
  highlight();
}
function highlight() { [...$("sessions").children].forEach((li) => li.classList.toggle("active", li._id === activeSession)); }

function renderPluginsList(rows) {
  const ul = $("plugins"); ul.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  $("cnt-plugins").textContent = `${list.filter((p) => p.enabled).length}/${list.length}`;
  if (!list.length) { ul.appendChild(el("li", "meta", "(none)")); return; }
  list.forEach((p) => {
    const li = el("li", "plugin-row" + (p.enabled ? "" : " off"));
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!p.enabled;
    cb.onchange = () => send({ type: "set_plugin_enabled", name: p.name, enabled: cb.checked });
    const main = el("div", "p-main");
    main.appendChild(el("div", "p-name", p.name));
    if (p.description) main.appendChild(el("div", "p-desc", p.description));
    const chips = el("div", "p-chips");
    (p.requires || []).forEach((r) => chips.appendChild(el("span", "p-chip req", "req:" + r)));
    (p.tools || []).forEach((t) => chips.appendChild(el("span", "p-chip", t)));
    (p.skills || []).forEach((s) => chips.appendChild(el("span", "p-chip skill", "✦ " + s)));
    (p.panels || []).forEach((pn) => chips.appendChild(el("span", "p-chip panel", "◫ " + (pn.title || pn.id))));
    (p.commands || []).forEach((c) => chips.appendChild(el("span", "p-chip cmd", "/" + c)));
    if (p.has_turn_policy) chips.appendChild(el("span", "p-chip", "turn policy"));
    if (p.has_middleware) chips.appendChild(el("span", "p-chip", "middleware"));
    if (p.skipped_reason) chips.appendChild(el("span", "p-chip warn", p.skipped_reason));
    if (chips.children.length) main.appendChild(chips);
    li.appendChild(cb); li.appendChild(main); ul.appendChild(li);
  });
}
function renderToolsList(rows) {
  const ul = $("tools"); ul.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  $("cnt-tools").textContent = `${list.filter((t) => t.enabled !== false).length}/${list.length}`;
  if (!list.length) { ul.appendChild(el("li", "meta", "(none)")); return; }
  const core = [], fromPlugins = [], evolved = [];
  list.forEach((r) => {
    const src = r.source || "core";
    if (src === "core") core.push(r);
    else if (src === "evolved") evolved.push(r);
    else fromPlugins.push(r);
  });
  const appendGroup = (title, items, showSource) => {
    if (!items.length) return;
    ul.appendChild(el("li", "group-hdr", title));
    items.forEach((r) => {
      const li = el("li", "toggle" + (r.enabled !== false ? "" : " off"));
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = r.enabled !== false;
      cb.onchange = () => send({ type: "set_tool_enabled", name: r.name, enabled: cb.checked });
      const body = el("div", "t-body");
      const label = el("span", "tname", r.name);
      if (r.description) label.title = r.description;
      body.appendChild(label);
      if (showSource && r.source && r.source !== "core") {
        body.appendChild(el("span", "src-tag" + (r.source === "evolved" ? " evolved" : ""), r.source));
      }
      li.appendChild(cb); li.appendChild(body); ul.appendChild(li);
    });
  };
  appendGroup("Core", core, false);
  appendGroup("From plugins", fromPlugins, true);
  appendGroup("Self-evolved", evolved, true);
}
function renderSkillsList(rows) {
  const ul = $("skills"); ul.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  $("cnt-skills").textContent = `${list.filter((s) => s.enabled !== false).length}/${list.length}`;
  if (!list.length) { ul.appendChild(el("li", "meta", "(none)")); return; }

  const fromPlugins = [], proj = [], glob = [], builtin = [], other = [];
  list.forEach((r) => {
    if (r.plugin) fromPlugins.push(r);
    else if (r.scope === "project") proj.push(r);
    else if (r.scope === "global") glob.push(r);
    else if (r.scope === "builtin") builtin.push(r);
    else other.push(r);
  });

  const appendGroup = (title, items) => {
    if (!items.length) return;
    ul.appendChild(el("li", "group-hdr", title));
    items.forEach((r) => {
      const li = el("li", "toggle" + (r.enabled !== false ? "" : " off"));
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = r.enabled !== false;
      cb.onchange = () => send({ type: "set_skill_enabled", name: r.name, enabled: cb.checked });
      const body = el("div", "t-body");
      const label = el("span", "tname", r.name);
      if (r.description) label.title = r.description;
      body.appendChild(label);
      if (r.plugin) {
        body.appendChild(el("span", "src-tag plugin", r.plugin));
      } else if (r.category) {
        body.appendChild(el("span", "src-tag cat", r.category));
      }
      li.appendChild(cb); li.appendChild(body); ul.appendChild(li);
    });
  };

  appendGroup("Built-in", builtin);
  appendGroup("From plugins", fromPlugins);
  appendGroup("Standalone · Project", proj);
  appendGroup("Standalone · Global", glob);
  appendGroup("Standalone", other);
}

$("quit").addEventListener("click", async () => {
  const ok = await askConfirm({ title: "Stop codelet server?", message: "This will shut down the local backend server and disconnect this tab.", okText: "Stop server", danger: true });
  if (!ok) return;
  try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
  if (ws) { ws.onclose = null; ws.close(); }
  setConnState("err");
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;font:15px system-ui;color:#888;background:#18191c">codelet server stopped — you can close this tab.</div>';
});

// ---------- bootstrap ----------
try {
  applyTheme(localStorage.getItem("codelet_theme"));
  const savedSideW = Number(localStorage.getItem("codelet_side_w"));
  if (savedSideW >= 200 && savedSideW <= 520) setPaneWidth("--side-w", savedSideW);
  const savedInspW = Number(localStorage.getItem("codelet_insp_w"));
  if (savedInspW >= 300 && savedInspW <= 720) setPaneWidth("--insp-w", savedInspW);
  if (localStorage.getItem("codelet_side_collapsed") === "1") $("app").classList.add("side-collapsed");
  const savedTab = localStorage.getItem("codelet_insp_tab");
  if (savedTab) activeInspectorTab = savedTab;
  if (localStorage.getItem("codelet_insp_open") === "1") openInspector(activeInspectorTab);
} catch (_) {
  applyTheme(null);
}
updateModeUI("ask");
connect();
loadSessions();
toggleEmpty();
