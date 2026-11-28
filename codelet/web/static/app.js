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
let autoFollow = true;
let scrollRaf = 0;
function scroll(force = false) {
  if (force) autoFollow = true;
  if (!autoFollow) return;
  const apply = () => {
    scroller.scrollTop = scroller.scrollHeight;
    $("scroll-bottom")?.classList.add("hidden");
  };
  apply();
  if (!scrollRaf) {
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      if (autoFollow) apply();
    });
  }
}
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

// ---------- messages & collapsible turn trace ----------
function addMessage(role) {
  const msg = el("div", `msg ${role}`);
  msg.appendChild(el("div", "avatar", role === "assistant" ? "◆" : "U"));
  const body = el("div", "body");
  const head = el("div", "msg-head");
  head.appendChild(el("div", "who", role === "assistant" ? "codelet" : "You"));
  const entry = {
    msg, body, bubble: null, text: "", fallbackNotes: [],
    traceEl: null, traceSteps: null, traceLabel: null, traceMeta: null,
    activeThinkBody: null, activeThinkText: "",
    toolCount: 0, toolNames: [], errCount: 0, thinkCount: 0,
    startedAt: Date.now(), finalized: false,
  };
  if (role === "assistant") {
    const copyBtn = el("button", "msg-copy");
    copyBtn.type = "button";
    copyBtn.innerHTML = SVG_COPY + "<span>Copy</span>";
    copyBtn.onclick = () => copyText(entry.text || entry.fallbackNotes.join("\n\n"), copyBtn, true);
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

function ensureTrace(a, { open = true, running = true } = {}) {
  if (a.traceEl) return a.traceEl;
  const d = document.createElement("details");
  d.className = "turn-trace" + (running ? " running" : "");
  d.open = open;
  const sum = document.createElement("summary");
  sum.className = "trace-summary";
  sum.innerHTML = '<svg class="ic trace-chev" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg><span class="trace-dot"></span>';
  const lbl = el("span", "trace-label", running ? "思考与执行中…" : "执行过程");
  const meta = el("span", "trace-meta mono", "");
  sum.appendChild(lbl);
  sum.appendChild(meta);
  const steps = el("div", "trace-steps");
  d.appendChild(sum);
  d.appendChild(steps);
  // Always place the execution trace ABOVE the final answer bubble.
  a.body.insertBefore(d, a.bubble);
  a.traceEl = d;
  a.traceSteps = steps;
  a.traceLabel = lbl;
  a.traceMeta = meta;
  return d;
}

function summarizeToolCounts(names) {
  if (!names || !names.length) return "";
  const counts = new Map();
  names.forEach((n) => counts.set(n, (counts.get(n) || 0) + 1));
  const pairs = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const top = pairs.slice(0, 3).map(([k, v]) => (v > 1 ? `${k} ×${v}` : k));
  if (pairs.length > 3) top.push(`+${pairs.length - 3}`);
  return top.join(", ");
}

function updateTraceSummary(a, { done = false, cancelled = false, showDuration = true } = {}) {
  if (!a || !a.traceEl) return;
  a.traceEl.classList.toggle("running", !done);
  a.traceEl.classList.toggle("has-err", a.errCount > 0);
  const toolsSummary = summarizeToolCounts(a.toolNames);
  const parts = [];
  if (!done) {
    if (a.toolCount > 0) {
      parts.push(`正在执行 · 已调用 ${a.toolCount} 个工具`);
      if (toolsSummary) parts.push(`(${toolsSummary})`);
    } else if (a.thinkCount > 0) {
      parts.push("正在思考…");
    } else {
      parts.push("执行中…");
    }
    a.traceLabel.textContent = parts.join(" ");
    a.traceMeta.textContent = "运行中";
    return;
  }

  if (a.thinkCount > 0 && a.toolCount > 0) {
    parts.push(`已完成思考 · 调用 ${a.toolCount} 个工具`);
  } else if (a.toolCount > 0) {
    parts.push(`调用 ${a.toolCount} 个工具`);
  } else if (a.thinkCount > 0) {
    parts.push("已完成思考");
  } else {
    parts.push("执行过程");
  }
  if (toolsSummary) parts.push(`(${toolsSummary})`);
  if (a.errCount > 0) parts.push(`· ${a.errCount} 失败`);
  if (cancelled) parts.push("· 已中断");
  a.traceLabel.textContent = parts.join(" ");
  if (showDuration && a.startedAt) {
    const sec = Math.max(0.1, (Date.now() - a.startedAt) / 1000);
    a.traceMeta.textContent = sec >= 60 ? `${Math.floor(sec / 60)}m ${(sec % 60).toFixed(0)}s` : `${sec.toFixed(1)}s`;
  } else {
    a.traceMeta.textContent = "";
  }
}

function appendTraceNote(a, text) {
  const clean = (text || "").trim();
  if (!clean) return;
  ensureTrace(a, { open: !a.finalized, running: !a.finalized });
  a.fallbackNotes.push(clean);
  const note = el("div", "trace-note md");
  note.innerHTML = renderMarkdown(clean);
  wireCodeCopies(note);
  a.traceSteps.appendChild(note);
}

function appendTraceThinking(a, text, { streaming = false } = {}) {
  ensureTrace(a, { open: !a.finalized, running: !a.finalized });
  if (streaming && a.activeThinkBody) {
    a.activeThinkText += text;
    a.activeThinkBody.textContent = a.activeThinkText;
    return;
  }
  const box = el("div", "trace-think");
  const head = el("div", "trace-think-head");
  head.innerHTML = SVG_BRAIN + "<span>思考过程</span>";
  const body = el("div", "trace-think-body", text || "");
  box.appendChild(head);
  box.appendChild(body);
  a.traceSteps.appendChild(box);
  a.thinkCount += 1;
  if (streaming) {
    a.activeThinkBody = body;
    a.activeThinkText = text || "";
  }
}

function appendToolCard(a, { name, input, output = "", is_error = false, status = "running" }) {
  ensureTrace(a, { open: !a.finalized, running: !a.finalized });
  const card = el("div", "tool");
  const head = el("div", "head");
  head.innerHTML = SVG_CHEV_R;
  head.appendChild(el("span", "tname", name || "?"));
  const summary = summarizeToolInput(name, input);
  if (summary) head.appendChild(el("span", "tsum", summary));
  const badgeCls = status === "running" ? "run" : (is_error ? "err" : "ok");
  const badgeTxt = status === "running" ? "running" : (is_error ? "error" : "ok");
  head.appendChild(el("span", `badge ${badgeCls}`, badgeTxt));
  head.onclick = () => card.classList.toggle("open");
  const tbody = el("div", "tbody");
  if (input && typeof input === "object" && Object.keys(input).length > 0) {
    tbody.appendChild(el("div", "t-sec-lbl", "Input"));
    tbody.appendChild(el("div", "args", JSON.stringify(input, null, 2)));
  }
  if (output) {
    tbody.appendChild(el("div", "t-sec-lbl", "Output"));
    tbody.appendChild(el("div", "out", output));
  }
  card.appendChild(head);
  card.appendChild(tbody);
  card._name = name;
  card._done = status !== "running";
  card._tbody = tbody;
  if (is_error) card.classList.add("open");
  a.traceSteps.appendChild(card);
  a.toolCount += 1;
  a.toolNames.push(name || "?");
  if (is_error) a.errCount += 1;
  return card;
}

function finalizeAssistantTurn(a, { cancelled = false } = {}) {
  if (!a || a.finalized) return;
  a.finalized = true;
  a.activeThinkBody = null;
  clearThinking(a);
  const hasFinalText = Boolean((a.text || "").trim());
  a.bubble.classList.toggle("empty-bubble", !hasFinalText);
  if (a.traceEl) {
    updateTraceSummary(a, { done: true, cancelled, showDuration: true });
    // Collapse thinking & tool calls automatically once the final answer is ready.
    a.traceEl.open = !hasFinalText || cancelled;
  }
}

// ---------- incoming WS frames ----------
function render(ev) {
  switch (ev.type) {
    case "profile": return onProfile(ev);
    case "workspace": return onWorkspace(ev);
    case "cleared":
      transcript.innerHTML = ""; current = null; activeSession = null;
      cockpitViewSession = "__live__";
      toggleEmpty(); highlight(); renderCockpitToolbar();
      return;
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
      appendTraceThinking(a, ev.text || "", { streaming: true });
      updateTraceSummary(a, { done: false });
      if (atBottom()) scroll();
      return;
    }
    case "text_delta": {
      const a = ensureAssistant(); clearThinking(a);
      a.activeThinkBody = null;
      a.text += ev.text;
      a.bubble.classList.remove("empty-bubble");
      setAssistantMarkdown(a);
      if (atBottom()) scroll();
      return;
    }
    case "stream_end": return;
    case "tool_start": {
      const a = ensureAssistant(); clearThinking(a);
      a.activeThinkBody = null;
      // Any text emitted before a tool call is intermediate narration -> move it into the trace above the final answer.
      if (a.text.trim()) {
        appendTraceNote(a, a.text);
        a.text = "";
        a.bubble.innerHTML = "";
        a.bubble.classList.add("empty-bubble");
      }
      appendToolCard(a, { name: ev.name, input: ev.input || {}, status: "running" });
      if (a.traceEl) a.traceEl.open = true;
      updateTraceSummary(a, { done: false });
      if (atBottom()) scroll();
      return;
    }
    case "tool_result": {
      const a = ensureAssistant();
      const scope = a.traceSteps || a.body;
      const card = [...scope.querySelectorAll(".tool")].reverse().find((c) => c._name === ev.name && !c._done);
      if (card) {
        card._done = true;
        const badge = card.querySelector(".badge");
        badge.className = "badge " + (ev.is_error ? "err" : "ok");
        badge.textContent = ev.is_error ? "error" : "ok";
        if (ev.output) {
          card._tbody.appendChild(el("div", "t-sec-lbl", "Output"));
          card._tbody.appendChild(el("div", "out", ev.output));
        }
        if (ev.is_error) {
          card.classList.add("open");
          a.errCount += 1;
        }
      }
      updateTraceSummary(a, { done: false });
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
    case "turn_done": {
      if (current) finalizeAssistantTurn(current, { cancelled: ev.type === "turn_cancelled" });
      current = null; setBusy(false);
      if (atBottom()) scroll();
      return;
    }
    case "resumed": return onResumed(ev);
    case "error": {
      if (current) finalizeAssistantTurn(current, { cancelled: true });
      transcript.appendChild(el("div", "note error", ev.message));
      current = null; setBusy(false); toggleEmpty(); scroll();
      return;
    }
  }
}

function onThinking(on) {
  const a = ensureAssistant();
  if (on && !a.body.querySelector(".thinking")) a.body.appendChild(el("div", "thinking", "thinking"));
  else if (!on) clearThinking(a);
}
function onTelemetry(ev) {
  const t = ev.turn || {}, s = ev.session || {};
  const costText = s.cost_usd == null ? "n/a" : "~$" + s.cost_usd.toFixed(4);
  const totSessTok = (s.input_tokens || 0) + (s.output_tokens || 0);
  // Compact inline numbers in topbar so narrow panes never clip
  $("tt-in").textContent = fmtCompact(t.input_tokens); $("tt-out").textContent = fmtCompact(t.output_tokens);
  $("ts-in").textContent = fmtCompact(s.input_tokens); $("ts-out").textContent = fmtCompact(s.output_tokens);
  $("ts-cost").textContent = costText;
  if ($("ts-short-tok")) $("ts-short-tok").textContent = fmtCompact(totSessTok);
  if ($("ts-short-cost")) $("ts-short-cost").textContent = costText;
  // Exact comma-formatted numbers in the expandable popover
  if ($("tp-turn-in")) $("tp-turn-in").textContent = fmt(t.input_tokens);
  if ($("tp-turn-out")) $("tp-turn-out").textContent = fmt(t.output_tokens);
  if ($("tp-sess-in")) $("tp-sess-in").textContent = fmt(s.input_tokens);
  if ($("tp-sess-out")) $("tp-sess-out").textContent = fmt(s.output_tokens);
  if ($("tp-cost")) $("tp-cost").textContent = costText;
}
function onUiAction(ev) {
  if (ev.open_drawer) openInspector(ev.open_drawer);
  if (ev.open_modal === "model") openModelModal();
}
const desktopApi = () => (typeof window !== "undefined" && window.codeletDesktop) || null;
function pushNotice(message, level = "info") {
  transcript.appendChild(el("div", `note ${level}`, message));
  toggleEmpty();
  if (atBottom()) scroll();
}
async function downloadMarkdown(filename, content) {
  const api = desktopApi();
  if (api && typeof api.saveMarkdown === "function") {
    try {
      const res = await api.saveMarkdown(filename, content);
      if (res && res.ok && res.path) pushNotice(`Exported conversation to ${res.path}`, "info");
      else if (res && !res.canceled && res.error) pushNotice(`Export failed: ${res.error}`, "error");
      return;
    } catch (_) {}
  }
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
$("btw-quick").addEventListener("click", () => openInspector("btw"));

// Topbar collapsible telemetry popover
function closeTelPopover() {
  $("tel-popover")?.classList.add("hidden");
  $("tel-pill")?.setAttribute("aria-expanded", "false");
}
$("tel-pill")?.addEventListener("click", (e) => {
  e.stopPropagation();
  const pop = $("tel-popover");
  if (!pop) return;
  const open = pop.classList.toggle("hidden") === false;
  $("tel-pill").setAttribute("aria-expanded", open ? "true" : "false");
});
$("tp-open-cockpit")?.addEventListener("click", () => { closeTelPopover(); openInspector("cockpit"); });
document.addEventListener("click", (e) => {
  const wrap = $("tel-wrap");
  if (wrap && !wrap.contains(e.target)) closeTelPopover();
});

// ---------- Cockpit rendering ----------
function gaugeColor(pct) {
  if (pct >= 85) return "var(--err)";
  if (pct >= 70) return "var(--warn)";
  return "var(--ok)";
}
let cockpitViewSession = "__live__";
let cockpitProjectFilter = "__all__";
let cockpitSessionGroups = [];
let cockpitPollTimer = null;

function onCockpit(ev, { fromHistory = false } = {}) {
  if (!fromHistory && !ev?.viewing_history && ev?.session_id) {
    const changed = activeSession !== ev.session_id;
    activeSession = ev.session_id;
    if (cockpitViewSession === activeSession) cockpitViewSession = "__live__";
    if (changed) { highlight(); renderCockpitToolbar(); }
  }
  if (fromHistory && ev && ev.viewing_history === false) {
    cockpitViewSession = "__live__";
    renderCockpitToolbar();
  }
  if (!fromHistory && cockpitViewSession !== "__live__") return;
  cockpitData = ev;
  const enabled = ev && ev.enabled !== false;
  $("ck-disabled").classList.toggle("hidden", enabled);
  $("ck-content").classList.toggle("hidden", !enabled);
  $("ctx-pill").classList.toggle("hidden", !enabled);
  const histBanner = $("ckHistBanner");
  if (histBanner) {
    const isHist = Boolean(ev && ev.viewing_history && (!activeSession || ev.session_id !== activeSession));
    histBanner.classList.toggle("hidden", !isHist);
    if (isHist) $("ckHistLabel").textContent = `历史会话：${ev.session_title || ev.session_id || ""}`;
  }
  if (!enabled) return;

  const ctx = ev.context || {}, cache = ev.cache || {}, perf = ev.perf || {}, usage = ev.usage || {};
  const pct = Math.max(0, Math.min(100, Number(ctx.pct || 0)));
  const color = gaugeColor(pct);

  // Topbar mini context ring
  $("topCtxPct").textContent = Math.round(pct) + "%";
  const mrFg = $("topRingFg");
  mrFg.style.strokeDashoffset = String(56.55 * (1 - pct / 100));
  mrFg.style.stroke = color;
  $("ctx-pill").title = `Context: ${fmtCompact(ctx.used)} / ${fmtCompact(ctx.limit)} (${pct}%) — click for Cockpit`;
  if ($("tp-ctx")) $("tp-ctx").textContent = `${fmtCompact(ctx.used)} / ${fmtCompact(ctx.limit)} (${pct}%)`;

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

  // Card 4: Usage & spend (provider-agnostic)
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
function renderCockpitToolbar() {
  const projSel = $("ckProjectSelect");
  const sessSel = $("ckSessionSelect");
  if (!projSel || !sessSel) return;

  const namedGroups = cockpitSessionGroups.filter((g) => g.workspace);
  const allGroups = cockpitSessionGroups;
  const prevProj = cockpitProjectFilter;
  projSel.innerHTML = "";
  const optAll = document.createElement("option");
  optAll.value = "__all__";
  optAll.textContent = `全部项目 (${namedGroups.length || allGroups.length})`;
  projSel.appendChild(optAll);
  allGroups.forEach((g) => {
    const val = g.workspace || "__other__";
    const o = document.createElement("option");
    o.value = val;
    o.textContent = `${g.name || "Other"} (${(g.sessions || []).length})`;
    projSel.appendChild(o);
  });
  const validProj = [...projSel.options].some((o) => o.value === prevProj) ? prevProj : "__all__";
  cockpitProjectFilter = validProj;
  projSel.value = validProj;

  const curWsName = ($("ws-name")?.textContent || "").trim();
  const prevSess = (activeSession && cockpitViewSession === activeSession) ? "__live__" : cockpitViewSession;
  cockpitViewSession = prevSess;
  sessSel.innerHTML = "";

  // Locate activeSession in the grouped session list so we never render duplicate entries
  let activeEntry = null, activeGroup = null;
  if (activeSession) {
    for (const g of allGroups) {
      const hit = (g.sessions || []).find((r) => r.id === activeSession);
      if (hit) { activeEntry = hit; activeGroup = g; break; }
    }
  }
  const showProjSuffix = validProj === "__all__";
  const liveOpt = document.createElement("option");
  liveOpt.value = "__live__";
  if (activeEntry) {
    const activeTitle = (activeEntry.title || activeEntry.summary || activeEntry.id).slice(0, 28);
    liveOpt.textContent = (showProjSuffix && activeGroup?.name) ? `${activeTitle} (${activeGroup.name})` : activeTitle;
  } else {
    liveOpt.textContent = (showProjSuffix && curWsName && curWsName !== "—") ? `当前对话 (${curWsName})` : "当前对话";
  }
  sessSel.appendChild(liveOpt);

  const visibleGroups = validProj === "__all__"
    ? allGroups
    : allGroups.filter((g) => (g.workspace || "__other__") === validProj);
  visibleGroups.forEach((g) => {
    (g.sessions || []).forEach((r) => {
      if (activeSession && r.id === activeSession) return;
      const o = document.createElement("option");
      o.value = r.id;
      const title = (r.title || r.summary || r.id).slice(0, 28);
      o.textContent = (showProjSuffix && g.name) ? `${title} (${g.name})` : title;
      sessSel.appendChild(o);
    });
  });

  if ([...sessSel.options].some((o) => o.value === prevSess)) {
    sessSel.value = prevSess;
  } else {
    cockpitViewSession = "__live__";
    sessSel.value = "__live__";
  }
}

async function refreshCockpitData({ manual = false, reloadList = false } = {}) {
  const btn = $("ckRefreshBtn");
  if (manual && btn) btn.classList.add("spinning");
  try {
    if (reloadList) await loadSessions();
    if (cockpitViewSession === "__live__") {
      if (ws && ws.readyState === 1) send({ type: "get_cockpit" });
      else {
        const snap = await safe("/api/cockpit");
        if (snap) onCockpit(snap);
      }
    } else {
      const snap = await safe("/api/cockpit?session_id=" + encodeURIComponent(cockpitViewSession));
      if (snap && !snap.error) onCockpit(snap, { fromHistory: true });
    }
  } finally {
    if (manual && btn) setTimeout(() => btn.classList.remove("spinning"), 380);
  }
}

function resetCockpitPoller() {
  if (cockpitPollTimer) { clearInterval(cockpitPollTimer); cockpitPollTimer = null; }
  const ms = Number($("ckPollSelect")?.value || 0);
  if (ms >= 1000) {
    cockpitPollTimer = setInterval(() => {
      if ($("app").classList.contains("insp-open") && activeInspectorTab === "cockpit") {
        refreshCockpitData({ manual: false, reloadList: false });
      }
    }, ms);
  }
}

$("ckProjectSelect")?.addEventListener("change", () => {
  cockpitProjectFilter = $("ckProjectSelect").value || "__all__";
  renderCockpitToolbar();
  if (cockpitViewSession === "__live__") refreshCockpitData();
});
$("ckSessionSelect")?.addEventListener("change", () => {
  cockpitViewSession = $("ckSessionSelect").value || "__live__";
  refreshCockpitData({ manual: true });
});
$("ckBackLive")?.addEventListener("click", () => {
  cockpitViewSession = "__live__";
  if ($("ckSessionSelect")) $("ckSessionSelect").value = "__live__";
  refreshCockpitData({ manual: true });
});
$("ckPollSelect")?.addEventListener("change", () => {
  try { localStorage.setItem("codelet_ck_poll", $("ckPollSelect").value); } catch (_) {}
  resetCockpitPoller();
});
$("ckRefreshBtn")?.addEventListener("click", () => refreshCockpitData({ manual: true, reloadList: true }));
try {
  const savedPoll = localStorage.getItem("codelet_ck_poll");
  if (savedPoll != null && $("ckPollSelect")) $("ckPollSelect").value = savedPoll;
} catch (_) {}
resetCockpitPoller();

$("ckRingTrigger").addEventListener("click", () => $("ckRingAnchor").classList.toggle("pinned"));
document.addEventListener("click", (e) => {
  if (!$("ckRingAnchor").contains(e.target)) $("ckRingAnchor").classList.remove("pinned");
});
$("ckLimitSelect").addEventListener("change", (e) => send({ type: "set_context_limit", limit: Number(e.target.value) }));
$("ckCompactBtn").addEventListener("click", () => send({ type: "compact", focus: "" }));
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
    { name: "API & Model Settings…", desc: "Configure API Key (DPAPI), Base URL & profiles", cat: "env", run: () => openSettingsModal() },
    { name: "Switch model…", desc: activeModel, cat: "model", run: () => openModelModal() },
    { name: "Refresh model list", desc: "Fetch latest models from endpoint", cat: "model", run: () => triggerModelRefresh() },
    { name: "Open Cockpit", desc: "Context gauge, speed, cache & usage spend", cat: "panel", run: () => openInspector("cockpit") },
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
  const effortNames = { auto: "Auto", off: "Off", low: "Low", medium: "Med", high: "High" };
  $("effort-label").textContent = effortNames[effort] || effort;
  $("effort-chip").dataset.effort = effort;
  $("effort-chip").title = `Thinking: ${effort}${EFFORT_DESC[effort] ? " — " + EFFORT_DESC[effort] : ""}`;
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
  if ($("settings-dot") && typeof ev.api_key_configured === "boolean") {
    $("settings-dot").classList.toggle("hidden", ev.api_key_configured);
  }
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
  const entry = modelCatalog.find((m) => m.id === activeModel);
  const label = (entry && entry.display_name) ? entry.display_name : activeModel;
  $("model-name").textContent = label || "—";
  const idHint = (entry && entry.display_name && entry.display_name !== activeModel) ? `${entry.display_name} (${activeModel})` : activeModel;
  $("model-chip").title = (idHint ? idHint + " — " : "") + modelMetaText();
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
    const hasAlias = m.display_name && m.display_name.toLowerCase() !== m.id.toLowerCase();
    left.appendChild(el("div", "m-id", hasAlias ? m.display_name : m.id));
    if (hasAlias) left.appendChild(el("div", "m-sub mono", m.id));
    const right = el("div", "m-tags");
    if (m.custom) right.appendChild(el("span", "m-tag pin", "directory"));
    else if (m.pinned) right.appendChild(el("span", "m-tag pin", "pinned"));
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
let wsUiState = { nav_open: {}, ws_groups_open: {} };
let applyingWsUi = false;
function applyWorkspaceUiState(st) {
  wsUiState = {
    nav_open: (st && typeof st.nav_open === "object" && st.nav_open) ? { ...st.nav_open } : {},
    ws_groups_open: (st && typeof st.ws_groups_open === "object" && st.ws_groups_open) ? { ...st.ws_groups_open } : {},
  };
  applyingWsUi = true;
  try {
    ["nav-sessions", "nav-plugins", "nav-tools", "nav-skills"].forEach((id) => {
      const d = $(id);
      if (!d) return;
      if (Object.prototype.hasOwnProperty.call(wsUiState.nav_open, id)) {
        d.open = Boolean(wsUiState.nav_open[id]);
      }
    });
  } finally {
    applyingWsUi = false;
  }
}
function onWorkspace(ev) {
  const name = ev.name || ev.cwd;
  $("ws-name").textContent = name;
  $("ws-chip").title = ev.cwd;
  if (ev.ui_state) {
    applyWorkspaceUiState(ev.ui_state);
    loadSessions();
  }
  const api = desktopApi();
  if (api) {
    if (typeof api.setTitle === "function") api.setTitle(`codelet — ${name}`);
    if (typeof api.reportWorkspace === "function" && ev.cwd) api.reportWorkspace(ev.cwd);
  }
}
function onResumed(ev) {
  transcript.innerHTML = ""; current = null;
  if (ev.id) activeSession = ev.id;
  cockpitViewSession = "__live__";
  (ev.messages || []).forEach((m) => {
    const b = addMessage(m.role);
    b.finalized = true;
    b.text = m.text || "";
    if (m.role === "assistant") {
      const steps = Array.isArray(m.steps) && m.steps.length
        ? m.steps
        : (m.tools || []).map((name) => ({ type: "tool", name, input: {}, output: "", is_error: false }));
      if (steps.length) {
        ensureTrace(b, { open: false, running: false });
        steps.forEach((st) => {
          if (st.type === "thinking" && st.text) appendTraceThinking(b, st.text, { streaming: false });
          else if (st.type === "note" && st.text) appendTraceNote(b, st.text);
          else if (st.type === "tool") {
            appendToolCard(b, {
              name: st.name,
              input: st.input || {},
              output: st.output || "",
              is_error: Boolean(st.is_error),
              status: "done",
            });
          }
        });
        updateTraceSummary(b, { done: true, showDuration: false });
        // Keep collapsed when there is a final answer; expand only if there is no final text
        b.traceEl.open = !b.text.trim();
      }
      if (b.text.trim()) {
        b.bubble.classList.remove("empty-bubble");
        setAssistantMarkdown(b);
      } else {
        b.bubble.classList.add("empty-bubble");
      }
    } else {
      b.bubble.textContent = b.text;
    }
  });
  current = null; toggleEmpty(); highlight(); renderCockpitToolbar(); scroll(true);
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
  d.addEventListener("toggle", () => {
    try { localStorage.setItem("codelet_" + id, d.open ? "1" : "0"); } catch (_) {}
    if (!applyingWsUi) {
      wsUiState.nav_open[id] = d.open;
      send({ type: "set_ui_state", nav_open: { [id]: d.open } });
    }
  });
});
scroller.addEventListener("scroll", () => {
  const dist = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  autoFollow = dist < 80;
  $("scroll-bottom").classList.toggle("hidden", dist <= 120);
}, { passive: true });
$("scroll-bottom").addEventListener("click", () => {
  autoFollow = true;
  scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  $("scroll-bottom").classList.add("hidden");
});
if (typeof ResizeObserver === "function") {
  new ResizeObserver(() => {
    if (autoFollow) scroll();
  }).observe(transcript);
}

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
  scroll(true);
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
    $("settings-modal").classList.add("hidden");
    $("discover-modal").classList.add("hidden");
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
async function openWorkspacePicker() {
  const api = desktopApi();
  if (api && typeof api.pickWorkspaceDir === "function") {
    try {
      const chosen = await api.pickWorkspaceDir($("ws-chip").title || "");
      if (chosen) send({ type: "set_workspace", path: chosen });
      return;
    } catch (_) {}
  }
  $("ws-modal").classList.remove("hidden");
  browseTo("");
}
$("ws-chip").addEventListener("click", openWorkspacePicker);
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
function buildSessionRow(r) {
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
  li._id = r.id;
  return li;
}
async function loadSessions() {
  const s = await safe("/api/sessions");
  const ul = $("sessions"); ul.innerHTML = "";
  const groups = (s && Array.isArray(s.groups) && s.groups.length) ? s.groups : null;
  cockpitSessionGroups = groups || [];
  renderCockpitToolbar();
  if (groups) {
    let total = 0;
    groups.forEach((g) => { total += (g.sessions || []).length; });
    $("cnt-sessions").textContent = String(total);
    if (total === 0 && groups.length === 1 && !groups[0].sessions.length) {
      ul.appendChild(el("li", "meta", "(no conversations yet)"));
      return;
    }
    const SVG_FOLDER = '<svg class="ic ws-g-ic" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
    const SVG_CHEV = '<svg class="ic ws-g-chev" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>';
    groups.forEach((g) => {
      const items = g.sessions || [];
      if (!g.is_current && !items.length) return;
      const wrap = el("li", "ws-grp-li");
      const det = document.createElement("details");
      det.className = "ws-grp" + (g.is_current ? " current" : "");
      const gKey = (g.workspace || "_other").toLowerCase();
      const key = "codelet_wsg_" + gKey;
      let isOpen = g.is_current || items.some((x) => x.id === activeSession);
      if (wsUiState.ws_groups_open && Object.prototype.hasOwnProperty.call(wsUiState.ws_groups_open, gKey)) {
        isOpen = Boolean(wsUiState.ws_groups_open[gKey]);
      } else {
        try {
          const saved = localStorage.getItem(key);
          if (saved === "1") isOpen = true;
          else if (saved === "0") isOpen = false;
        } catch (_) {}
      }
      det.open = isOpen;
      let lastOpen = isOpen;
      det.addEventListener("toggle", () => {
        if (det.open === lastOpen) return;
        lastOpen = det.open;
        try { localStorage.setItem(key, det.open ? "1" : "0"); } catch (_) {}
        wsUiState.ws_groups_open[gKey] = det.open;
        send({ type: "set_ui_state", ws_groups_open: { [gKey]: det.open } });
      });
      const sum = document.createElement("summary");
      sum.title = g.workspace || "Other workspaces";
      sum.innerHTML = SVG_FOLDER;
      sum.appendChild(el("span", "ws-g-name", g.name || g.workspace || "Other"));
      sum.appendChild(el("span", "ws-g-cnt", String(items.length)));
      sum.insertAdjacentHTML("beforeend", SVG_CHEV);
      det.appendChild(sum);

      const subUl = el("ul", "ws-grp-list");
      if (!items.length) {
        subUl.appendChild(el("li", "ws-grp-empty", "(no conversations in this folder)"));
      } else {
        items.forEach((r) => subUl.appendChild(buildSessionRow(r)));
      }
      det.appendChild(subUl);
      wrap.appendChild(det);
      ul.appendChild(wrap);
    });
    highlight();
    return;
  }
  const rows = (s && s.project) || [];
  $("cnt-sessions").textContent = String(rows.length);
  if (!rows.length) { ul.appendChild(el("li", "meta", "(no conversations here yet)")); return; }
  rows.forEach((r) => ul.appendChild(buildSessionRow(r)));
  highlight();
}
function highlight() { $("sessions").querySelectorAll("li.session").forEach((li) => li.classList.toggle("active", li._id === activeSession)); }

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
  const api = desktopApi();
  if (api && typeof api.quitApp === "function") {
    const ok = await askConfirm({ title: "Quit codelet?", message: "This will exit codelet Desktop and stop the local backend server.", okText: "Quit", danger: true });
    if (!ok) return;
    await api.quitApp();
    return;
  }
  const ok = await askConfirm({ title: "Stop codelet server?", message: "This will shut down the local backend server and disconnect this tab.", okText: "Stop server", danger: true });
  if (!ok) return;
  try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
  if (ws) { ws.onclose = null; ws.close(); }
  setConnState("err");
  document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;font:15px system-ui;color:#888;background:#18191c">codelet server stopped — you can close this tab.</div>';
});

// ---------- desktop integration hooks ----------
{
  const api = desktopApi();
  if (api) {
    document.body.classList.add("is-desktop");
    if (typeof api.openExternal === "function") {
      document.addEventListener("click", (e) => {
        const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
        if (!a) return;
        const href = a.getAttribute("href") || "";
        if (/^https?:\/\//i.test(href)) {
          e.preventDefault();
          api.openExternal(href);
        }
      });
    }
    if (typeof api.onAction === "function") {
      api.onAction((payload) => {
        if (!payload || !payload.type) return;
        if (payload.type === "new-chat") send({ type: "new_conversation" });
        else if (payload.type === "pick-workspace") openWorkspacePicker();
        else if (payload.type === "open-settings") openSettingsModal();
        else if (payload.type === "open-workspace" && payload.path) send({ type: "set_workspace", path: payload.path });
        else if (payload.type === "update-status") onUpdateStatus(payload);
      });
    }
  }
}

function fmtBytes(n) {
  const b = Number(n || 0);
  if (b >= 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + " MB";
  if (b >= 1024) return (b / 1024).toFixed(0) + " KB";
  return Math.round(b) + " B";
}
function onUpdateStatus(ev) {
  const box = $("update-toast");
  if (!box) return;
  const ver = ev.version || "新版本";
  const titleEl = $("ut-title");
  const pctEl = $("ut-pct");
  const fillEl = $("ut-fill");
  const detEl = $("ut-detail");
  const instBtn = $("ut-install");
  const relBtn = $("ut-releases");

  box.classList.remove("hidden", "done", "err", "indeterminate");
  instBtn.classList.add("hidden");
  relBtn.classList.add("hidden");

  if (ev.state === "downloading") {
    const pct = Math.max(0, Math.min(100, Number(ev.percent || 0)));
    const total = Number(ev.total || 0);
    const transferred = Number(ev.transferred || 0);
    const bps = Number(ev.bytesPerSecond || 0);
    titleEl.textContent = `正在下载更新 ${ver}`;
    if (total > 0) {
      pctEl.textContent = `${Math.round(pct)}%`;
      fillEl.style.width = `${pct.toFixed(1)}%`;
      detEl.textContent = `${fmtBytes(transferred)} / ${fmtBytes(total)}${bps > 0 ? " · " + fmtBytes(bps) + "/s" : ""}`;
    } else {
      box.classList.add("indeterminate");
      pctEl.textContent = "…";
      detEl.textContent = "正在连接 GitHub Releases 获取更新包…";
    }
  } else if (ev.state === "downloaded") {
    box.classList.add("done");
    titleEl.textContent = `更新 ${ver} 已下载完成`;
    pctEl.textContent = "100%";
    fillEl.style.width = "100%";
    detEl.textContent = "点击右侧按钮立即重启并安装";
    instBtn.classList.remove("hidden");
  } else if (ev.state === "error") {
    box.classList.add("err");
    titleEl.textContent = `更新 ${ver} 下载失败`;
    pctEl.textContent = "失败";
    detEl.textContent = ev.message || "网络异常，可手动前往发布页下载";
    relBtn.classList.remove("hidden");
  }
}
$("ut-close")?.addEventListener("click", () => $("update-toast")?.classList.add("hidden"));
$("ut-install")?.addEventListener("click", () => {
  const api = desktopApi();
  if (api && typeof api.installUpdate === "function") api.installUpdate();
});
$("ut-releases")?.addEventListener("click", () => {
  const api = desktopApi();
  const url = "https://github.com/muamuauh/codelet/releases";
  if (api && typeof api.openExternal === "function") api.openExternal(url);
  else window.open(url, "_blank", "noopener");
});

// ---------- API & Model Settings Modal (with Desktop DPAPI vault) ----------
const SETTINGS_PRESETS = {
  openai: { name: "openai", provider: "openai", base_url: "https://api.openai.com/v1", model: "gpt-4.1", api_key_env: "OPENAI_API_KEY" },
  anthropic: { name: "anthropic", provider: "anthropic", base_url: "https://api.anthropic.com", model: "claude-sonnet-4-5", api_key_env: "ANTHROPIC_API_KEY" },
  deepseek: { name: "deepseek", provider: "openai", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", api_key_env: "DEEPSEEK_API_KEY" },
  openrouter: { name: "openrouter", provider: "openai", base_url: "https://openrouter.ai/api/v1", model: "anthropic/claude-sonnet-4.5", api_key_env: "OPENROUTER_API_KEY" },
  custom: { name: "custom", provider: "openai", base_url: "", model: "", api_key_env: "LLM_API_KEY" },
};
let settingsState = null;
let settingsVaultInfo = null;
let settingsClearKeyPending = false;
let settingsModelsDraft = [];
let settingsRelaySelected = false;
let discoverContext = null;

const OFFICIAL_HOSTS = new Set(["api.openai.com", "api.anthropic.com", "api.deepseek.com", "openrouter.ai", "localhost", "127.0.0.1"]);

function isRelayEndpoint(baseUrl) {
  if (settingsRelaySelected) return true;
  const raw = (baseUrl || "").trim();
  if (!raw) return false;
  try {
    const host = new URL(raw).hostname.toLowerCase();
    return Boolean(host) && !OFFICIAL_HOSTS.has(host);
  } catch (_) {
    return false;
  }
}

function setSettingsFeedback(msg, kind = "") {
  const box = $("st-feedback");
  if (!msg) { box.className = "st-feedback hidden"; box.textContent = ""; return; }
  box.className = "st-feedback" + (kind ? " " + kind : "");
  box.textContent = msg;
}

function renderSettingsModels() {
  const box = $("st-models-list");
  const sub = $("st-models-sub");
  const cols = $("st-mdir-cols");
  box.innerHTML = "";
  const validCnt = settingsModelsDraft.filter((m) => (m.id || "").trim()).length;
  sub.textContent = validCnt > 0
    ? `已配置 ${validCnt} 个自定义模型（显示名称优先展示于选择器）`
    : "正在使用适配器 / 接口默认模型";
  cols.classList.toggle("hidden", settingsModelsDraft.length === 0);

  let dragIdx = -1;
  settingsModelsDraft.forEach((item, idx) => {
    const row = el("div", "st-mrow");
    row.draggable = true;
    row.addEventListener("dragstart", () => { dragIdx = idx; row.classList.add("dragging"); });
    row.addEventListener("dragend", () => { row.classList.remove("dragging"); box.querySelectorAll(".drag-over").forEach((x) => x.classList.remove("drag-over")); });
    row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("drag-over"); });
    row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      row.classList.remove("drag-over");
      if (dragIdx < 0 || dragIdx === idx) return;
      const [moved] = settingsModelsDraft.splice(dragIdx, 1);
      settingsModelsDraft.splice(idx, 0, moved);
      renderSettingsModels();
    });

    const handle = el("span", "st-mhandle", "≡");
    handle.title = "拖动调整顺序";

    const idInp = document.createElement("input");
    idInp.type = "text";
    idInp.className = "st-mid";
    idInp.placeholder = "实际模型 ID，如 deepseek-flash";
    idInp.value = item.id || "";
    idInp.addEventListener("input", () => { item.id = idInp.value; });

    const nameInp = document.createElement("input");
    nameInp.type = "text";
    nameInp.className = "st-mname";
    nameInp.placeholder = "显示名称，如 DeepSeek-V4.1-Flash";
    nameInp.value = item.display_name || "";
    nameInp.addEventListener("input", () => { item.display_name = nameInp.value; });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "st-mdel";
    del.innerHTML = SVG_TRASH;
    del.title = "删除该模型";
    del.addEventListener("click", () => {
      settingsModelsDraft.splice(idx, 1);
      renderSettingsModels();
    });

    row.appendChild(handle);
    row.appendChild(idInp);
    row.appendChild(nameInp);
    row.appendChild(del);
    box.appendChild(row);
  });
}

function collectSettingsModels() {
  const out = [];
  const seen = new Set();
  settingsModelsDraft.forEach((m) => {
    const id = (m.id || "").trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    const display_name = (m.display_name || "").trim() || id;
    out.push({ id, display_name });
  });
  return out;
}

function populateSettingsForm(prof) {
  settingsClearKeyPending = false;
  settingsRelaySelected = false;
  const p = prof || {};
  $("st-name").value = p.name && p.name !== "_env" ? p.name : "default";
  $("st-provider").value = p.provider === "anthropic" ? "anthropic" : "openai";
  $("st-base-url").value = p.base_url || "";
  $("st-model").value = p.model || "";
  $("st-key-env").value = p.api_key_env || ($("st-provider").value === "anthropic" ? "ANTHROPIC_API_KEY" : "LLM_API_KEY");
  $("st-api-key").value = "";
  $("st-api-key").type = "password";
  $("st-key-eye").textContent = "显示";

  settingsModelsDraft = Array.isArray(p.models)
    ? p.models.map((m) => ({ id: m.id || "", display_name: m.display_name || m.id || "" }))
    : [];
  renderSettingsModels();

  const stEl = $("st-key-status");
  if (p.api_key_configured) {
    stEl.textContent = `已配置 (${p.api_key_preview || "••••"})`;
    stEl.className = "st-key-status mono ok";
  } else {
    stEl.textContent = "未配置";
    stEl.className = "st-key-status mono warn";
  }
  setSettingsFeedback("");
}

function renderSettingsProfiles(selectedName) {
  const sel = $("st-prof-select");
  sel.innerHTML = "";
  const profiles = (settingsState && settingsState.profiles) || {};
  const names = Object.keys(profiles);
  if (!names.length) {
    const opt = document.createElement("option");
    opt.value = "default"; opt.textContent = "default";
    sel.appendChild(opt);
    populateSettingsForm({ name: "default", provider: "openai", api_key_env: "LLM_API_KEY" });
    return;
  }
  names.forEach((n) => {
    const p = profiles[n];
    const opt = document.createElement("option");
    opt.value = n;
    opt.textContent = `${n === "_env" ? "default (env)" : n} · ${p.provider}${p.api_key_configured ? " ✓" : ""}`;
    sel.appendChild(opt);
  });
  const target = selectedName && profiles[selectedName] ? selectedName : (settingsState.active_profile || names[0]);
  sel.value = target;
  populateSettingsForm(profiles[target]);
}

async function openSettingsModal() {
  $("settings-modal").classList.remove("hidden");
  setSettingsFeedback("");
  const api = desktopApi();
  const badge = $("st-vault-badge");
  const note = $("st-security-note");
  if (api && typeof api.getVaultInfo === "function") {
    try {
      settingsVaultInfo = await api.getVaultInfo();
    } catch (_) {
      settingsVaultInfo = null;
    }
  } else {
    settingsVaultInfo = null;
  }
  if (settingsVaultInfo && settingsVaultInfo.available) {
    badge.textContent = settingsVaultInfo.storage === "dpapi" ? "🔒 Windows DPAPI 加密存储" : "🔒 系统钥匙串加密存储";
    badge.className = "vault-badge secure";
    note.textContent = "API Key 通过系统 DPAPI 加密存入本机凭据库，绝不以明文写入磁盘；非敏感配置保存至 ~/.codelet/settings.json。";
  } else {
    badge.textContent = "内存会话 / 环境变量";
    badge.className = "vault-badge";
    note.textContent = "当前为浏览器模式：新输入的 API Key 仅保存在当前后端进程内存中（不写明文文件），非敏感配置保存至 ~/.codelet/settings.json。";
  }

  const data = await safe("/api/settings");
  if (data) {
    settingsState = data;
    renderSettingsProfiles(data.active_profile);
  }
}

$("open-settings").addEventListener("click", openSettingsModal);
$("st-cancel").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
$("settings-modal").addEventListener("click", (e) => { if (e.target === $("settings-modal")) $("settings-modal").classList.add("hidden"); });

$("st-prof-select").addEventListener("change", () => {
  const n = $("st-prof-select").value;
  if (settingsState && settingsState.profiles && settingsState.profiles[n]) {
    populateSettingsForm(settingsState.profiles[n]);
  }
});

$("st-prof-new").addEventListener("click", () => {
  populateSettingsForm({
    name: "custom",
    provider: "openai",
    base_url: "",
    model: "",
    api_key_env: "LLM_API_KEY",
    api_key_configured: false,
  });
  $("st-name").focus();
  $("st-name").select();
});

$("st-prof-del").addEventListener("click", async () => {
  const target = $("st-prof-select").value;
  if (!target || target === "_env") {
    setSettingsFeedback("当前默认环境变量方案不可删除。", "err");
    return;
  }
  const ok = await askConfirm({ title: "删除配置方案？", message: `确定删除配置方案 "${target}" 吗？（不会自动删除其他方案共享的密钥）`, okText: "删除", danger: true });
  if (!ok) return;
  try {
    const r = await (await fetch("/api/settings/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delete_profile: target }),
    })).json();
    if (r.ok) {
      settingsState = r;
      renderSettingsProfiles(r.active_profile);
      setSettingsFeedback(`已删除配置方案 "${target}"`, "ok");
    } else {
      setSettingsFeedback(r.error || "删除失败", "err");
    }
  } catch (err) {
    setSettingsFeedback("请求失败: " + err.message, "err");
  }
});

document.querySelectorAll("#st-presets .st-preset").forEach((btn) => {
  btn.addEventListener("click", () => {
    const p = SETTINGS_PRESETS[btn.dataset.preset];
    if (!p) return;
    settingsRelaySelected = btn.dataset.preset === "custom";
    $("st-name").value = p.name;
    $("st-provider").value = p.provider;
    $("st-base-url").value = p.base_url;
    $("st-model").value = p.model;
    $("st-key-env").value = p.api_key_env;
    setSettingsFeedback(
      settingsRelaySelected
        ? "已选择自定义中转站模板：填好 Base URL、默认模型和 API Key 并保存后，可自动用该模型阅读中转站文档拉取全部可用模型。"
        : `已填充 ${btn.textContent} 模板，请填写对应 API Key 后点击保存。`
    );
  });
});

$("st-add-model").addEventListener("click", () => {
  settingsModelsDraft.push({ id: "", display_name: "" });
  renderSettingsModels();
  const inputs = $("st-models-list").querySelectorAll(".st-mid");
  if (inputs.length) inputs[inputs.length - 1].focus();
});

$("st-fetch-models").addEventListener("click", async () => {
  const btn = $("st-fetch-models");
  btn.disabled = true;
  setSettingsFeedback("正在从接口拉取模型列表…");
  try {
    const r = await (await fetch("/api/settings/discover-models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: $("st-provider").value,
        base_url: $("st-base-url").value.trim(),
        model: $("st-model").value.trim(),
        api_key_env: $("st-key-env").value.trim(),
        api_key: $("st-api-key").value.trim(),
        use_llm: false,
      }),
    })).json();
    if (r.ok && Array.isArray(r.models)) {
      const existingMap = new Map(collectSettingsModels().map((m) => [m.id, m.display_name]));
      r.models.forEach((m) => { if (!existingMap.has(m.id)) existingMap.set(m.id, m.display_name || m.id); });
      settingsModelsDraft = [...existingMap.entries()].map(([id, display_name]) => ({ id, display_name }));
      renderSettingsModels();
      setSettingsFeedback("✓ " + (r.message || `已拉取 ${r.models.length} 个模型`) + "，可编辑显示名称后保存。", "ok");
    } else {
      setSettingsFeedback("✕ " + (r.error || "接口未返回模型列表"), "err");
    }
  } catch (err) {
    setSettingsFeedback("✕ 拉取失败: " + err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

function setDiscoverStatus(msg, kind = "") {
  const box = $("disc-status");
  if (!msg) { box.className = "st-feedback hidden"; box.textContent = ""; return; }
  box.className = "st-feedback" + (kind ? " " + kind : "");
  box.textContent = msg;
}

function openDiscoverModal(ctx) {
  discoverContext = ctx;
  let hostLabel = ctx.base_url || "当前中转站";
  try { hostLabel = new URL(ctx.base_url).host || hostLabel; } catch (_) {}
  $("disc-desc").textContent =
    `检测到你配置了中转站（${hostLabel}）与默认模型 "${ctx.model}"。是否使用该模型自动阅读中转站文档并结合 /models 接口，提取所有可用模型填充到模型目录？`;
  $("disc-docs-url").value = "";
  setDiscoverStatus("");
  $("disc-run").disabled = false;
  $("discover-modal").classList.remove("hidden");
}

$("st-discover-models").addEventListener("click", () => {
  const model = $("st-model").value.trim();
  const base_url = $("st-base-url").value.trim();
  if (!model) {
    setSettingsFeedback("请先填写「默认模型」，再使用该模型自动阅读文档。", "err");
    $("st-model").focus();
    return;
  }
  openDiscoverModal({
    profile: $("st-name").value.trim() || "default",
    provider: $("st-provider").value,
    base_url,
    model,
    api_key_env: $("st-key-env").value.trim(),
    api_key: $("st-api-key").value.trim(),
    autoSave: false,
  });
});

$("disc-skip").addEventListener("click", () => $("discover-modal").classList.add("hidden"));
$("discover-modal").addEventListener("click", (e) => { if (e.target === $("discover-modal")) $("discover-modal").classList.add("hidden"); });

$("disc-run").addEventListener("click", async () => {
  if (!discoverContext) return;
  const btn = $("disc-run");
  btn.disabled = true;
  setDiscoverStatus(`正在探测文档并调用 ${discoverContext.model} 提取可用模型（可能需要数秒）…`);
  try {
    const r = await (await fetch("/api/settings/discover-models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: discoverContext.provider,
        base_url: discoverContext.base_url,
        model: discoverContext.model,
        api_key_env: discoverContext.api_key_env,
        api_key: discoverContext.api_key || undefined,
        docs_url: $("disc-docs-url").value.trim(),
        use_llm: true,
      }),
    })).json();
    if (!r.ok || !Array.isArray(r.models) || !r.models.length) {
      setDiscoverStatus("✕ " + (r.error || "未发现可用模型"), "err");
      btn.disabled = false;
      return;
    }

    // Merge discovered models into Settings modal draft
    const merged = new Map(collectSettingsModels().map((m) => [m.id, m.display_name]));
    r.models.forEach((m) => {
      if (!merged.has(m.id)) merged.set(m.id, m.display_name || m.id);
    });
    const nextModels = [...merged.entries()].map(([id, display_name]) => ({ id, display_name }));
    settingsModelsDraft = nextModels;
    renderSettingsModels();

    // If triggered right after saving a relay, immediately persist the discovered directory too
    if (discoverContext.autoSave) {
      const saveRes = await (await fetch("/api/settings/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile: discoverContext.profile,
          provider: discoverContext.provider,
          base_url: discoverContext.base_url,
          model: discoverContext.model,
          api_key_env: discoverContext.api_key_env,
          models: nextModels,
        }),
      })).json();
      if (saveRes && saveRes.ok) {
        settingsState = saveRes;
        renderSettingsProfiles(saveRes.active_profile);
      }
    }

    $("discover-modal").classList.add("hidden");
    $("settings-modal").classList.remove("hidden");
    setSettingsFeedback(
      "✓ " + (r.message || `已发现 ${r.models.length} 个模型`) + (discoverContext.autoSave ? "，已自动写入模型目录（可继续微调显示名称后再次保存）。" : "，请核对显示名称后点击「保存并立即生效」。"),
      "ok"
    );
    $("st-models-list").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    setDiscoverStatus("✕ 自动发现失败: " + err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

$("st-key-eye").addEventListener("click", () => {
  const inp = $("st-api-key");
  const show = inp.type === "password";
  inp.type = show ? "text" : "password";
  $("st-key-eye").textContent = show ? "隐藏" : "显示";
});

$("st-key-clear").addEventListener("click", () => {
  settingsClearKeyPending = true;
  $("st-api-key").value = "";
  const stEl = $("st-key-status");
  stEl.textContent = "待清除（点击保存后生效）";
  stEl.className = "st-key-status mono warn";
});

$("st-test").addEventListener("click", async () => {
  const btn = $("st-test");
  btn.disabled = true;
  setSettingsFeedback("正在测试接口连通性…");
  try {
    const payload = {
      provider: $("st-provider").value,
      base_url: $("st-base-url").value.trim(),
      model: $("st-model").value.trim(),
      api_key_env: $("st-key-env").value.trim(),
      api_key: $("st-api-key").value.trim(),
    };
    const r = await (await fetch("/api/settings/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })).json();
    if (r.ok) setSettingsFeedback("✓ " + (r.message || "连接成功"), "ok");
    else setSettingsFeedback("✕ " + (r.error || "连接失败"), "err");
  } catch (err) {
    setSettingsFeedback("✕ 测试请求失败: " + err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

$("st-save").addEventListener("click", async () => {
  const btn = $("st-save");
  const profile = $("st-name").value.trim() || "default";
  const provider = $("st-provider").value;
  const base_url = $("st-base-url").value.trim();
  const model = $("st-model").value.trim();
  const api_key_env = $("st-key-env").value.trim() || (provider === "anthropic" ? "ANTHROPIC_API_KEY" : "LLM_API_KEY");
  const rawKey = $("st-api-key").value.trim();

  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(api_key_env)) {
    setSettingsFeedback("密钥标识名必须为合法环境变量名（字母/下划线开头，仅含字母数字下划线）", "err");
    return;
  }

  btn.disabled = true;
  setSettingsFeedback("正在安全保存并应用配置…");
  try {
    const api = desktopApi();
    if (api && settingsVaultInfo && settingsVaultInfo.available) {
      if (settingsClearKeyPending && !rawKey && typeof api.deleteSecret === "function") {
        await api.deleteSecret(api_key_env);
      } else if (rawKey && typeof api.saveSecret === "function") {
        const sr = await api.saveSecret(api_key_env, rawKey);
        if (!sr || !sr.ok) {
          setSettingsFeedback("DPAPI 加密保存失败: " + (sr?.error || "未知错误"), "err");
          btn.disabled = false;
          return;
        }
      }
    }

    const customModels = collectSettingsModels();
    const shouldPromptRelayDiscover =
      isRelayEndpoint(base_url) &&
      Boolean(model) &&
      customModels.length === 0;

    const r = await (await fetch("/api/settings/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile,
        provider,
        base_url,
        model,
        api_key_env,
        api_key: rawKey || undefined,
        clear_api_key: settingsClearKeyPending && !rawKey,
        models: customModels,
      }),
    })).json();

    if (!r.ok) {
      setSettingsFeedback("保存失败: " + (r.error || "未知错误"), "err");
      btn.disabled = false;
      return;
    }

    settingsState = r;
    settingsClearKeyPending = false;
    $("st-api-key").value = "";
    renderSettingsProfiles(r.active_profile);
    if ($("settings-dot")) $("settings-dot").classList.toggle("hidden", !!r.api_key_configured);
    setSettingsFeedback("✓ 配置已保存并立即生效", "ok");
    pushNotice(`已切换配置方案 "${r.active_profile}" (${r.provider}${r.model ? " · " + r.model : ""})`, "info");

    if (shouldPromptRelayDiscover && r.api_key_configured) {
      $("settings-modal").classList.add("hidden");
      openDiscoverModal({
        profile: r.active_profile,
        provider,
        base_url,
        model,
        api_key_env,
        api_key: "",
        autoSave: true,
      });
    } else {
      setTimeout(() => { $("settings-modal").classList.add("hidden"); }, 550);
    }
  } catch (err) {
    setSettingsFeedback("保存请求失败: " + err.message, "err");
  } finally {
    btn.disabled = false;
  }
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
