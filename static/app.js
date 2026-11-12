const $ = (id) => document.getElementById(id);
const COLS = [
  ["created", "Created"], ["last_activity", "Last activity"], ["title", "Session"], ["workspace", "Workspace"],
  ["src", "Source"], ["model", "Model"], ["messages", "Msgs"], ["tps", "TPS"], ["input", "Input"], ["output", "Output"],
  ["cached", "Cached"], ["price", "Est. cost"], ["hit", "Cache hit"],
  ["quota_day", "Daily %"], ["quota_week", "Weekly %"],
];
const savedCols = () => { try { const v = JSON.parse(localStorage.getItem("ledger.cols")); return Array.isArray(v) ? v : []; } catch { return []; } };
const state = { rows: [], sortKey: "created", asc: false, multi: new Set(), models: new Set(), limit: 2000,
  quota: null, runway: null, runwayReceivedAt: 0, runwaySig: "",
  hiddenCols: new Set(savedCols().filter((k) => COLS.some(([c]) => c === k))) };
const fmt = new Intl.NumberFormat();
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const fmtPrice = (p) => p >= 1000 ? "$" + compact.format(p) : p >= 1 ? "$" + p.toFixed(2) : p >= 0.0001 ? "$" + p.toFixed(4) : p > 0 ? "<$0.0001" : "$0";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const base = (p) => (p || "").split(/[\\/]/).filter(Boolean).pop() || p || "—";
const dt = (s) => {
  const d = new Date(s * 1000);
  return `${d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" })}<small>${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</small>`;
};
const dayStart = (v) => (v ? new Date(v + "T00:00:00").getTime() / 1000 : null);

function filtered() {
  const q = $("q").value.trim().toLowerCase();
  const ws = $("f-ws").value, hidden = $("f-hidden").checked;
  const from = dayStart($("f-from").value), to = dayStart($("f-to").value);
  return state.rows.filter((r) =>
    (hidden || !r.hidden) &&
    (!state.models.size || state.models.has(r.model)) &&
    (!ws || r.workspace === ws) &&
    (from == null || r.created >= from) &&
    (to == null || r.created < to + 86400) &&
    (!q || `${r.title} ${r.id} ${r.workspace} ${r.src} ${r.model}`.toLowerCase().includes(q)));
}

function sorted(rows) {
  const k = state.sortKey, dir = state.asc ? 1 : -1;
  return rows.sort((a, b) => {
    const x = a[k], y = b[k];
    const c = x == null ? (y == null ? 0 : 1) : y == null ? -1 : typeof x === "number" ? x - y : String(x).localeCompare(String(y));
    return (c || a.created - b.created) * dir;
  });
}

const card = (lbl, val, sub, sw = "", i = 0) =>
  `<div class="card" style="animation-delay:${i * 60}ms"><div class="lbl">${sw ? `<i class="sw ${sw}"></i>` : ""}${lbl}</div><div class="val">${val}</div><div class="sub">${sub}</div></div>`;

function renderTotals(rows) {
  const t = rows.reduce((a, r) => ({ input: a.input + r.input, output: a.output + r.output, cached: a.cached + r.cached, messages: a.messages + r.messages, price: a.price + (r.price || 0), gen_ms: a.gen_ms + (r.gen_ms || 0) }),
    { input: 0, output: 0, cached: 0, messages: 0, price: 0, gen_ms: 0 });
  const sessions = new Set(rows.map((r) => r.id)).size;
  const hit = t.input + t.cached ? (t.cached / (t.input + t.cached)) * 100 : 0;
  $("totals").innerHTML = [
    card("Sessions", fmt.format(sessions), `${fmt.format(rows.length)} model rows`, "", 0),
    card("Responses", compact.format(t.messages), fmt.format(t.messages), "", 1),
    card("Input", compact.format(t.input), fmt.format(t.input), "in", 2),
    card("Output", compact.format(t.output), fmt.format(t.output), "out", 3),
    card("Avg TPS", t.gen_ms ? (t.output * 1000 / t.gen_ms).toFixed(1) : "—", "output ÷ generation time", "", 4),
    card("Cached", compact.format(t.cached), fmt.format(t.cached), "ca", 5),
    card("Cache hit", `${hit.toFixed(1)}%`, "cached ÷ (input + cached)", "", 6),
    card("Est. cost", t.price ? fmtPrice(t.price) : "—", "Devin Enterprise rates", "", 7),
  ].join("");
}

const hitPct = (r) => (r.input + r.cached ? (r.cached / (r.input + r.cached)) * 100 : null);

const CELLS = {
  created: (r) => `<td class="dt">${dt(r.created)}</td>`,
  last_activity: (r) => `<td class="dt">${dt(r.last_activity)}</td>`,
  title: (r) => `<td class="sess"><div class="t">${esc(r.title || "Untitled")}${r.hidden ? '<span class="tag">hidden</span>' : ""}</div><div class="id">${esc(r.id)}</div></td>`,
  workspace: (r) => `<td class="ws" title="${esc(r.workspace)}">${esc(base(r.workspace))}</td>`,
  src: (r) => `<td class="src">${esc(r.src || "—")}</td>`,
  model: (r) => `<td class="model" title="Used ${esc(new Date(r.first * 1000).toLocaleString())} → ${esc(new Date(r.last * 1000).toLocaleString())}">${esc(r.model)}</td>`,
  messages: (r) => `<td class="num">${fmt.format(r.messages)}</td>`,
  input: (r) => `<td class="num in">${fmt.format(r.input)}</td>`,
  output: (r) => `<td class="num out">${fmt.format(r.output)}</td>`,
  tps: (r) => `<td class="num" title="Output tokens ÷ generation time (token-weighted)">${r.tps == null ? "—" : r.tps.toFixed(1)}</td>`,
  cached: (r) => `<td class="num ca">${fmt.format(r.cached)}</td>`,
  price: (r) => `<td class="num price" title="${esc(r.price_model || "no published rate")}">${r.price == null ? "—" : fmtPrice(r.price)}</td>`,
  hit: (r) => `<td class="num">${r.hit == null ? "—" : r.hit.toFixed(1) + "%"}</td>`,
  quota_day: (r) => `<td class="num" title="${esc(quotaTitle(r, "daily"))}">${r.quota_day == null ? "—" : r.quota_day + "%"}</td>`,
  quota_week: (r) => `<td class="num" title="${esc(quotaTitle(r, "weekly"))}">${r.quota_week == null ? "—" : r.quota_week + "%"}</td>`,
};

function quotaTitle(r, which) {
  const label = which === "daily" ? "Daily" : "Weekly";
  const v = which === "daily" ? r.quota_day : r.quota_week;
  if (v != null && r.quota_t != null)
    return `${label} quota remaining · sampled ${new Date(r.quota_t * 1000).toLocaleString()}`;
  if (r.quota_t != null)
    return `${label} quota unknown · the last sample before this session ended (${new Date(r.quota_t * 1000).toLocaleString()}) predates a quota reset`;
  const q = state.quota;
  if (q && q.error) return `Quota unavailable: ${q.error}`;
  return `No ${label.toLowerCase()} quota sample at or before this session's last activity`;
}

/* quota runway widget: countdowns use the server's `generated` clock plus
   elapsed client time, so a skewed client clock can't corrupt them. */
const rwDur = (s) => {
  s = Math.max(0, Math.round(s));
  if (s < 60) return "<1m";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m >= 60 ? `${h + 1}h` : m ? `${h}h ${m}m` : `${h}h`;
};
const rwDurWords = (s) => {
  s = Math.round(s);
  if (s < 60) return "under a minute";
  if (s < 3600) { const m = Math.round(s / 60); return `${m} minute${m === 1 ? "" : "s"}`; }
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return `${h} hour${h === 1 ? "" : "s"}${m ? ` ${m} minutes` : ""}`;
};
const rwAgo = (s) => (s < 45 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${rwDur(s)} ago`);
const rwClock = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const rwNow = () => (state.runway.generated || Date.now() / 1000) + (Date.now() - state.runwayReceivedAt) / 1000;

function runwayCardHTML(name, c, now) {
  const pct = c.remaining == null ? null : Math.round(Math.min(100, Math.max(0, c.remaining)));
  const cls = pct == null || pct <= 0 ? "empty" : pct < 20 ? "low" : pct <= 50 ? "warn" : "good";
  const resetLine = c.reset == null ? "Reset time unknown"
    : c.reset <= now ? "Reset time passed — waiting for a fresh sample"
    : `Resets in ${rwDur(c.reset - now)} · ${rwClock(c.reset)}`;
  const exhaustIn = c.exhausts_at == null ? null : Math.max(0, c.exhausts_at - now);
  const conf = { high: "High confidence", medium: "Estimated", low: "Rough estimate" }[c.confidence];
  const meta = conf
    ? `${conf} · ${c.samples || 0} sample${c.samples === 1 ? "" : "s"} this cycle${c.sampled_at != null ? ` · as of ${rwAgo(now - c.sampled_at)}` : ""}`
    : "";
  const aria = pct == null ? 'aria-valuetext="Quota unknown"'
    : `aria-valuenow="${pct}" aria-valuetext="${pct} percent remaining${c.status === "ok" && exhaustIn ? `; projected to run out in ${rwDurWords(exhaustIn)}` : ""}"`;
  return `<div class="runway-card">
    <div class="rw-top"><span class="rw-lbl">${name[0].toUpperCase() + name.slice(1)} quota</span><span class="rw-val">${pct == null ? "—" : `${pct}% <span class="rw-left">left</span>`}</span></div>
    <div class="meter ${cls}" role="meter" aria-valuemin="0" aria-valuemax="100" ${aria}><i style="width:${pct || 0}%"></i></div>
    <div class="rw-reset">${resetLine}</div>
    ${meta ? `<div class="rw-meta">${meta}</div>` : ""}
  </div>`;
}

function setRunway(r) {
  if (!r || typeof r !== "object") return;
  const same = state.runway && JSON.stringify(state.runway) === JSON.stringify(r);
  state.runway = r;
  state.runwayReceivedAt = Date.now();
  if (!same) renderRunway();
}

function renderRunway() {
  const el = $("runway"), r = state.runway;
  if (!r) { el.hidden = true; return; }
  const now = rwNow();
  const counters = [["daily", r.daily], ["weekly", r.weekly]].filter(([, c]) => c && typeof c === "object");
  const usable = counters.filter(([, c]) => c.status !== "unknown");
  if (r.error && !usable.length) {
    el.innerHTML = `<div class="runway-note">Quota unavailable — ${esc(r.error)}</div>`;
    el.hidden = false;
    return;
  }
  if (!usable.length) {
    if (r.plan) {
      el.innerHTML = `<div class="runway-note">Quota is not reported on this plan.</div>`;
      el.hidden = false;
    } else {
      el.innerHTML = "";
      el.hidden = true;
    }
    return;
  }
  el.innerHTML = counters.map(([n, c]) => runwayCardHTML(n, c, now)).join("") +
    (r.error ? `<div class="runway-note">Quota fetch failed — showing the last sample from ${r.history && r.history.newest ? rwAgo(now - r.history.newest) : "earlier"}.</div>` : "");
  el.hidden = false;
  const sig = counters.map(([n, c]) => `${n}:${c.status}`).join(",");
  if (sig !== state.runwaySig)
    $("runway-status").textContent = counters.map(([n, c]) => `${n} quota ${c.status}`).join("; ");
  state.runwaySig = sig;
}

function render() {
  const rows = sorted(filtered());
  renderTotals(rows);
  $("empty").hidden = rows.length > 0;
  const shown = rows.slice(0, state.limit);
  const cols = COLS.filter(([k]) => !state.hiddenCols.has(k));
  $("rows").innerHTML = shown.map((r, i) => `
    <tr data-session-id="${esc(r.id)}" tabindex="0" aria-haspopup="dialog" title="Double-click or press Enter to view session details" class="${r.hidden ? "hidden-row" : ""} ${state.multi.has(r.id) ? "multi" : ""}" style="animation-delay:${Math.min(i, 30) * 12}ms">${cols.map(([k]) => CELLS[k](r)).join("")}</tr>`).join("");
  const rest = rows.length - shown.length;
  $("more").hidden = !rest;
  if (rest)
    $("more").textContent = `Showing first ${fmt.format(shown.length)} of ${fmt.format(rows.length)} rows — click to show ${fmt.format(Math.min(state.limit, rest))} more, or narrow the filters`;
  document.querySelectorAll("thead th[data-k]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === state.sortKey);
    th.classList.toggle("asc", th.dataset.k === state.sortKey && state.asc);
  });
  saveView();
}

/* table filters + sort persist in the URL hash so views survive reload and can be bookmarked */
function saveView() {
  try {
    const p = [];
    const put = (k, v) => p.push(`${k}=${encodeURIComponent(v)}`);
    if ($("q").value.trim()) put("q", $("q").value.trim());
    if (state.models.size) put("models", JSON.stringify([...state.models]));
    if ($("f-ws").value) put("ws", $("f-ws").value);
    if ($("f-from").value) put("from", $("f-from").value);
    if ($("f-to").value) put("to", $("f-to").value);
    if ($("f-hidden").checked) put("hidden", "1");
    if (state.sortKey !== "created" || state.asc) {
      put("sort", state.sortKey);
      if (state.asc) put("asc", "1");
    }
    history.replaceState(null, "", p.length ? "#" + p.join("&") : location.pathname + location.search);
  } catch {}
}

let viewLoaded = false;

function applyView() {
  let h;
  try { h = location.hash.slice(1); } catch { return; }
  if (!h) return;
  const p = {};
  for (const kv of h.split("&")) {
    const i = kv.indexOf("=");
    if (i < 1) continue;
    try { p[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1)); } catch {}
  }
  if (p.q != null) $("q").value = p.q;
  if (p.from) $("f-from").value = p.from;
  if (p.to) $("f-to").value = p.to;
  $("f-hidden").checked = p.hidden === "1";
  if (p.models) {
    try {
      const avail = new Set(state.rows.map((r) => r.model));
      state.models = new Set(JSON.parse(p.models).filter((m) => avail.has(m)));
      updateModelFilter();
    } catch {}
  }
  if (p.ws) {
    $("f-ws").value = p.ws;
    if ($("f-ws").value !== p.ws) $("f-ws").value = "";  // workspace no longer in the data
  }
  if (p.sort && COLS.some(([k]) => k === p.sort)) {
    state.sortKey = p.sort;
    state.asc = p.asc === "1";
  }
}

let sessionOpener = null;

function showSession(id, opener = null) {
  const models = state.rows.filter((r) => r.id === id).sort((a, b) => a.first - b.first);
  if (!models.length) return;
  const session = models[0];
  const totals = models.reduce((a, r) => ({
    messages: a.messages + r.messages, input: a.input + r.input,
    output: a.output + r.output, cached: a.cached + r.cached, gen_ms: a.gen_ms + (r.gen_ms || 0),
    price: a.price + (r.price ?? 0), priced: a.priced + (r.price != null ? 1 : 0),
  }), { messages: 0, input: 0, output: 0, cached: 0, gen_ms: 0, price: 0, priced: 0 });
  const hit = totals.input + totals.cached ? totals.cached / (totals.input + totals.cached) * 100 : 0;
  const unpriced = models.length - totals.priced;
  const detail = (label, value, cls = "") => `<div class="${cls}"><dt>${label}</dt><dd>${value}</dd></div>`;
  const stat = (label, value, cls = "", title = "") =>
    `<div class="session-stat ${cls}"${title ? ` title="${esc(title)}"` : ""}><div class="lbl">${label}</div><div class="val">${value}</div></div>`;
  $("session-title").textContent = session.title || "Untitled";
  $("session-meta").innerHTML = [
    detail("Session ID", esc(session.id)),
    detail("Visibility", session.hidden ? "Hidden" : "Visible"),
    detail("Workspace", esc(session.workspace || "—"), "wide"),
    detail("Source", esc(session.src || "—")),
    detail("Created", dt(session.created), "dt"),
    detail("Last activity", dt(session.last_activity), "dt"),
  ].join("");
  $("session-stats").innerHTML = [
    stat("Responses", fmt.format(totals.messages)),
    stat("Input · includes cache write", fmt.format(totals.input), "in"),
    stat("Output", fmt.format(totals.output), "out"),
    stat("Avg TPS", totals.gen_ms ? (totals.output * 1000 / totals.gen_ms).toFixed(1) : "—"),
    stat("Cached", fmt.format(totals.cached), "ca"),
    stat("Cache hit", `${hit.toFixed(1)}%`),
    stat("Daily quota", session.quota_day != null ? session.quota_day + "%" : "—", "", quotaTitle(session, "daily")),
    stat("Weekly quota", session.quota_week != null ? session.quota_week + "%" : "—", "", quotaTitle(session, "weekly")),
    stat(unpriced && totals.priced ? "Est. cost · partial" : "Est. cost", totals.priced ? fmtPrice(totals.price) : "—"),
  ].join("");
  $("session-models").innerHTML = models.map((r) => `
    <tr>
      <td>${esc(r.model)}</td>
      <td class="num">${fmt.format(r.messages)}</td>
      <td class="num in">${fmt.format(r.input)}</td>
      <td class="num">${fmt.format(r.cache_write || 0)}</td>
      <td class="num out">${fmt.format(r.output)}</td>
      <td class="num" title="Output tokens ÷ generation time (token-weighted)">${r.tps == null ? "—" : r.tps.toFixed(1)}</td>
      <td class="num ca">${fmt.format(r.cached)}</td>
      <td class="num price" title="${esc(r.price_model || "no published rate")}">${r.price == null ? "—" : fmtPrice(r.price)}</td>
      <td class="dt">${dt(r.first)}</td>
      <td class="dt">${dt(r.last)}</td>
    </tr>`).join("");
  $("session-cost-note").textContent = !totals.priced ? "No published rates for this session's models; cost is unavailable."
    : `${unpriced ? `Partial estimate: ${unpriced} model${unpriced === 1 ? "" : "s"} without published rates excluded. ` : ""}Estimated USD at configured model rates; not an invoice.`;
  sessionOpener = opener;
  if (opener) opener.focus();
  if (!$("session-details").open) $("session-details").showModal();
}

$("rows").addEventListener("dblclick", (e) => {
  const row = e.target.closest("tr[data-session-id]");
  if (row) showSession(row.dataset.sessionId, row);
});
$("rows").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const row = e.target.closest("tr[data-session-id]");
  if (!row) return;
  e.preventDefault();
  showSession(row.dataset.sessionId, row);
});
$("session-close").addEventListener("click", () => $("session-details").close());
$("session-details").addEventListener("click", (e) => {
  if (e.target !== $("session-details")) return;
  const rect = $("session-details").getBoundingClientRect();
  if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) $("session-details").close();
});
$("session-details").addEventListener("close", () => {
  if (sessionOpener) sessionOpener.focus();
  sessionOpener = null;
});

function fillSelect(el, values, label) {
  const cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` + values.map((v) => `<option value="${esc(v)}">${esc(label.includes("workspace") ? base(v) + "  —  " + v : v)}</option>`).join("");
  el.value = values.includes(cur) ? cur : "";
}

function updateModelFilter() {
  const models = [...state.models];
  $("f-model-label").textContent = models.length === 1 ? models[0] : models.length ? `${models.length} models selected` : "All models";
  $("f-model-label").title = models.length ? models.join(", ") : "All models";
  $("f-model-options").querySelectorAll("input").forEach((input) => (input.checked = state.models.has(input.value)));
}

function fillModels(values) {
  state.models = new Set([...state.models].filter((model) => values.includes(model)));
  $("f-model-options").innerHTML = values.map((model) => `<label class="model-option"><input type="checkbox" value="${esc(model)}"${state.models.has(model) ? " checked" : ""}><span>${esc(model)}</span></label>`).join("");
  updateModelFilter();
}

const saveCols = () => { try { localStorage.setItem("ledger.cols", JSON.stringify([...state.hiddenCols])); } catch {} };

function applyCols() {
  document.querySelectorAll("#tbl thead th").forEach((th) => (th.hidden = state.hiddenCols.has(th.dataset.k || th.dataset.col)));
}

function updateCols() {
  const shown = COLS.length - state.hiddenCols.size;
  $("f-cols-label").textContent = state.hiddenCols.size ? `${shown} of ${COLS.length} columns` : "Columns";
  $("f-cols-options").querySelectorAll("input").forEach((input) => (input.checked = !state.hiddenCols.has(input.value)));
}

function fillCols() {
  $("f-cols-options").innerHTML = COLS.map(([k, label]) =>
    `<label class="model-option"><input type="checkbox" value="${k}"${state.hiddenCols.has(k) ? "" : " checked"}><span>${label}</span></label>`).join("");
  updateCols();
  applyCols();
}

function setData(data) {
  state.quota = data.quota || null;
  if (data.runway) setRunway(data.runway);
  state.rows = data.rows.map((r) => ({ ...r, hit: hitPct(r) }));
  state.limit = 2000;
  const counts = {};
  data.rows.forEach((r) => (counts[r.id] = (counts[r.id] || 0) + 1));
  state.multi = new Set(Object.keys(counts).filter((k) => counts[k] > 1));
  $("dbpath").textContent = data.db;
  fillModels([...new Set(data.rows.map((r) => r.model))].sort());
  fillSelect($("f-ws"), [...new Set(data.rows.map((r) => r.workspace))].sort((a, b) => base(a).localeCompare(base(b))), "All workspaces");
  if (!viewLoaded) { viewLoaded = true; applyView(); }
  render();
}

function setStatus(s) {
  const dot = $("sync-dot"), txt = $("sync-text");
  $("refresh").disabled = s.running;
  dot.className = "dot " + (s.error ? "err" : s.running ? "busy" : "live");
  $("progress").style.width = s.running && s.total ? `${(s.done / s.total) * 100}%` : "0";
  if (s.error) txt.textContent = `Error: ${s.error}`;
  else if (s.running) txt.textContent = s.total ? `Scanning sessions ${s.done}/${s.total}…` : "Opening database…";
  else txt.textContent = s.updated ? `Synced ${new Date(s.updated * 1000).toLocaleTimeString()}` : "Idle";
}

async function load() {
  const data = await (await fetch("/api/data")).json();
  setData(data);
  setStatus(data.status);
  if (data.status.running) poll();
}

async function poll() {
  const s = await (await fetch("/api/status")).json();
  setStatus(s);
  if (s.running) setTimeout(poll, 800);
  else load();
}

document.querySelectorAll("thead th[data-k]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    state.asc = state.sortKey === k ? !state.asc : ["title", "workspace", "model"].includes(k);
    state.sortKey = k;
    render();
  }));
const refilter = () => { state.limit = 2000; render(); };
$("more").addEventListener("click", () => { state.limit += 2000; render(); });
["q", "f-ws", "f-from", "f-to", "f-hidden"].forEach((id) => $(id).addEventListener("input", refilter));
$("f-model-options").addEventListener("change", (e) => {
  if (e.target.type !== "checkbox") return;
  if (e.target.checked) state.models.add(e.target.value);
  else state.models.delete(e.target.value);
  updateModelFilter();
  refilter();
});
$("f-model-all").addEventListener("click", () => {
  state.models.clear();
  updateModelFilter();
  refilter();
});
$("f-cols-options").addEventListener("change", (e) => {
  if (e.target.type !== "checkbox") return;
  if (e.target.checked) state.hiddenCols.delete(e.target.value);
  else if (state.hiddenCols.size < COLS.length - 1) state.hiddenCols.add(e.target.value);
  else { e.target.checked = true; return; }
  saveCols();
  updateCols();
  applyCols();
  render();
});
$("f-cols-all").addEventListener("click", () => {
  state.hiddenCols.clear();
  saveCols();
  updateCols();
  applyCols();
  render();
});
document.addEventListener("click", (e) => {
  if (!$("f-model").contains(e.target)) $("f-model").open = false;
  if (!$("f-cols").contains(e.target)) $("f-cols").open = false;
  if (!$("report-models").contains(e.target)) $("report-models").open = false;
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  [["f-model", "f-model-label"], ["f-cols", "f-cols-label"]].forEach(([d, lbl]) => {
    if ($(d).open) { $(d).open = false; $(lbl).focus(); }
  });
});
$("clear").addEventListener("click", () => {
  ["q", "f-ws", "f-from", "f-to"].forEach((id) => ($(id).value = ""));
  $("f-hidden").checked = false;
  state.models.clear();
  updateModelFilter();
  $("f-model").open = false;
  refilter();
});
$("refresh").addEventListener("click", async () => {
  setStatus(await (await fetch("/api/refresh", { method: "POST" })).json());
  poll();
});

const prices = { data: null, key: "label", asc: true };
const rate = (v) => (v ? "$" + (v < 0.1 ? v.toFixed(3) : v.toFixed(2)) : "—");

function renderPrices() {
  if (!prices.data) return;
  const q = $("pq").value.trim().toLowerCase(), usedOnly = $("p-used").checked;
  const used = new Set(state.rows.map((r) => r.price_model).filter(Boolean));
  const k = prices.key, dir = prices.asc ? 1 : -1;
  const list = prices.data.models
    .filter((m) => (!q || m.label.toLowerCase().includes(q)) && (!usedOnly || used.has(m.label)))
    .sort((a, b) => (k === "label" ? a.label.localeCompare(b.label) : a[k] - b[k] || a.label.localeCompare(b.label)) * dir);
  $("prices-rows").innerHTML = list.map((m) => `
    <tr class="${used.has(m.label) ? "used" : ""}">
      <td title="${esc((m.uids || [m.uid]).join(", "))}">${esc(m.label)}${m.tier && m.tier !== prices.data.tier ? `<span class="tag" title="${m.tier === "Manual" ? `Not published by Devin; entered in pricing_manual.json from ${esc(m.source || "?")}` : `Not published for ${esc(prices.data.tier)}; using ${esc(m.tier)} rate`}">${esc(m.tier)}</span>` : ""}${m.long_prompt ? `<span class="tag" title="Requests with prompts over 100K tokens: ${["input", "output", "cache_write", "cache_read"].map((c) => `${c.replace("_", " ")} ${rate(m.long_prompt[c])}`).join(" · ")}">&gt;100K tier</span>` : ""}</td>
      <td class="num">${rate(m.input)}</td>
      <td class="num">${rate(m.output)}</td>
      <td class="num">${rate(m.cache_write)}</td>
      <td class="num">${rate(m.cache_read)}</td>
    </tr>`).join("");
  $("prices-count").textContent = `${list.length} of ${prices.data.models.length}`;
  document.querySelectorAll("#prices th[data-pk]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.pk === k);
    th.classList.toggle("asc", th.dataset.pk === k && prices.asc);
  });
}

$("prices-btn").addEventListener("click", async () => {
  prices.data = await (await fetch("/api/pricing")).json();
  const src = prices.data.source || "";
  $("prices-src").innerHTML = `${esc(prices.data.tier)} rates · snapshot ${esc(prices.data.fetched)} · <a href="${esc(src)}" target="_blank" rel="noopener">${esc(src.replace(/^https?:\/\//, ""))}</a> · update with update_prices.bat`;
  renderPrices();
  $("prices").showModal();
  $("pq").focus();
});
$("prices-close").addEventListener("click", () => $("prices").close());
$("prices").addEventListener("click", (e) => e.target === $("prices") && $("prices").close());
["pq", "p-used"].forEach((id) => $(id).addEventListener("input", renderPrices));
document.querySelectorAll("#prices th[data-pk]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.pk;
    prices.asc = prices.key === k ? !prices.asc : k === "label";
    prices.key = k;
    renderPrices();
  }));

/* report maker */
const savedReportView = () => { try { return localStorage.getItem("ledger.reportView") === "list" ? "list" : "chart"; } catch { return "chart"; } };
const report = { models: [], excluded: new Set(), rows: [], totals: null, group: "model", sortKey: "price", asc: false,
                 chartMetric: "price", view: savedReportView() };
const GROUP_LABEL = { model: "Model", workspace: "Workspace", day: "Day", week: "Week", month: "Month" };
const TIME_GROUPS = new Set(["day", "week", "month"]);
const CHART_COLORS = ["#e8a33d", "#d4573b", "#6fa89a", "#8f7ab8", "#5b8dc9", "#a8b56f", "#c96f9a"];
const OTHER_COLOR = "#6e6759", OTHER_KEY = "·other·";

function fillReportModels() {
  report.models = [...new Set(state.rows.map((r) => r.model).filter((m) => m !== "—"))].sort();
  report.excluded = new Set([...report.excluded].filter((m) => report.models.includes(m)));
  $("report-model-options").innerHTML = report.models.map((m) =>
    `<label class="model-option"><input type="checkbox" value="${esc(m)}"${report.excluded.has(m) ? "" : " checked"}><span>${esc(m)}</span></label>`).join("");
  updateReportModels();
}

function updateReportModels() {
  const n = report.models.length - report.excluded.size;
  $("report-model-label").textContent = report.excluded.size ? `${n} of ${report.models.length} models` : "All models";
  $("report-model-label").title = report.excluded.size ? `Excluded: ${[...report.excluded].join(", ")}` : "All models";
  const all = $("report-models-all");
  all.checked = report.excluded.size === 0;
  all.indeterminate = report.excluded.size > 0 && n > 0;
}

function reportCols() {
  const cols = [["key", GROUP_LABEL[report.group]], ["sessions", "Sessions"]];
  if (report.group !== "model") cols.push(["models", "Models"]);
  return cols.concat([["messages", "Responses"], ["input", "Input"], ["output", "Output"], ["tps", "TPS"], ["cached", "Cached"], ["price", "Est. cost"]]);
}

function sortedReport() {
  const k = report.sortKey, dir = report.asc ? 1 : -1;
  return [...report.rows].sort((a, b) => {
    const x = a[k], y = b[k];
    if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * dir;
  });
}

function renderReport() {
  $("report-hint").hidden = true;
  $("allowance-note").hidden = true;
  const cols = reportCols();
  $("report-head").innerHTML = cols.map(([k, l], i) => `<th data-rk="${k}"${i ? ' class="num"' : ""}>${l}</th>`).join("");
  document.querySelectorAll("#report-head th[data-rk]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.rk === report.sortKey);
    th.classList.toggle("asc", th.dataset.rk === report.sortKey && report.asc);
  });
  const rows = sortedReport();
  $("report-empty").hidden = rows.length > 0;
  $("report-rows").innerHTML = rows.map((r) => `
    <tr>
      <td${report.group === "model" ? ` class="model" title="${esc(r.price_model || "no published rate")}"` : report.group === "workspace" ? ` class="ws" title="${esc(r.label)}"` : ""}>${esc(report.group === "workspace" ? base(r.label) : r.label)}</td>
      <td class="num">${fmt.format(r.sessions)}</td>
      ${report.group !== "model" ? `<td class="num">${fmt.format(r.models)}</td>` : ""}
      <td class="num">${fmt.format(r.messages)}</td>
      <td class="num in">${fmt.format(r.input)}</td>
      <td class="num out">${fmt.format(r.output)}</td>
      <td class="num" title="Output tokens ÷ generation time (token-weighted)">${r.tps == null ? "—" : r.tps.toFixed(1)}</td>
      <td class="num ca">${fmt.format(r.cached)}</td>
      <td class="num price"${r.unpriced ? ` title="excludes ${r.unpriced} unpriced model${r.unpriced === 1 ? "" : "s"}"` : ""}>${r.price == null ? "—" : fmtPrice(r.price)}</td>
    </tr>`).join("");
  renderChart();
  const t = report.totals;
  if (t) {
    const hit = t.input + t.cached ? (t.cached / (t.input + t.cached)) * 100 : 0;
    $("report-totals").innerHTML = [
      card("Sessions", fmt.format(t.sessions), `${fmt.format(t.models)} models`, "", 0),
      card("Responses", compact.format(t.messages), fmt.format(t.messages), "", 1),
      card("Input", compact.format(t.input), fmt.format(t.input), "in", 2),
      card("Output", compact.format(t.output), fmt.format(t.output), "out", 3),
      card("Avg TPS", t.tps == null ? "—" : t.tps.toFixed(1), "output ÷ generation time", "", 4),
      card("Cached", compact.format(t.cached), fmt.format(t.cached), "ca", 5),
      card("Cache hit", `${hit.toFixed(1)}%`, "", "", 6),
      card("Est. cost", t.price ? fmtPrice(t.price) : "—", t.unpriced ? `${t.unpriced} unpriced model${t.unpriced === 1 ? "" : "s"}` : "", "", 7),
    ].join("");
  }
}

async function runReport() {
  const p = new URLSearchParams({ group: $("r-group").value });
  if ($("r-from").value) p.set("from", $("r-from").value);
  if ($("r-to").value) p.set("to", $("r-to").value);
  if ($("r-hidden").checked) p.set("hidden", "1");
  if (report.excluded.size) p.set("exc", [...report.excluded].join(","));
  $("r-run").disabled = true;
  try {
    const data = await (await fetch("/api/report?" + p)).json();
    report.group = data.group;
    report.rows = data.rows || [];
    report.totals = data.totals;
    const costGroup = data.group === "model" || data.group === "workspace";
    report.sortKey = costGroup ? "price" : "key";
    report.asc = !costGroup;
    renderReport();
    $("r-csv").disabled = !report.rows.length;
  } finally {
    $("r-run").disabled = false;
  }
}

/* stacked usage-over-time chart for day/week/month reports; the View toggle
   switches between it and the sortable list. Rows are ordered by period key
   (chronological), not by the table sort. */
function renderChart() {
  const el = $("report-chart");
  const time = TIME_GROUPS.has(report.group) && report.rows.length > 0;
  const show = time && report.view === "chart";
  $("report-view").hidden = !time;
  $("rv-chart").classList.toggle("on", report.view !== "list");
  $("rv-list").classList.toggle("on", report.view === "list");
  $("report-body").hidden = show;
  el.hidden = !show;
  if (!show) return;
  const rows = [...report.rows].sort((a, b) => String(a.key).localeCompare(String(b.key)));
  const per = new Map();  // model -> {price, tokens} across all periods
  for (const r of rows)
    for (const s of r.series || []) {
      const t = per.get(s.model) || { price: 0, tokens: 0 };
      t.price += s.price || 0;
      t.tokens += s.tokens;
      per.set(s.model, t);
    }
  const hasCost = [...per.values()].some((t) => t.price > 0);
  const metric = report.chartMetric === "price" && hasCost ? "price" : "tokens";
  const order = [...per].sort((a, b) => b[1][metric] - a[1][metric]).map(([k]) => k);
  const named = new Set(order.slice(0, CHART_COLORS.length));
  const keys = [...named, ...(order.length > named.size ? [OTHER_KEY] : [])];
  const color = new Map(keys.map((k, i) => [k, k === OTHER_KEY ? OTHER_COLOR : CHART_COLORS[i]]));
  const segOf = (r, k) => {
    const t = { price: 0, tokens: 0 };
    for (const s of r.series || [])
      if (k === OTHER_KEY ? !named.has(s.model) : s.model === k) {
        t.price += s.price || 0;
        t.tokens += s.tokens;
      }
    return t;
  };
  const cols = rows.map((r) => keys.map((k) => ({ k, ...segOf(r, k) })));
  const totals = cols.map((cs) => cs.reduce((a, s) => a + (metric === "price" ? s.price : s.tokens), 0));
  const max = Math.max(0, ...totals);
  const fmtTotal = (v) => (metric === "price" ? fmtPrice(v) : compact.format(v) + " tok");
  $("chart-legend").innerHTML = keys.map((k) =>
    `<span class="lg"><i style="background:${color.get(k)}"></i>${esc(k === OTHER_KEY ? "other" : k)}</span>`).join("");
  $("chart-plot").innerHTML = cols.map((cs, i) =>
    `<div class="chart-col" title="${esc(rows[i].label)} · ${fmtTotal(totals[i])}">` +
    cs.filter((s) => (metric === "price" ? s.price : s.tokens) > 0).map((s) =>
      `<div class="chart-seg" style="height:${max ? (metric === "price" ? s.price : s.tokens) / max * 100 : 0}%;background:${color.get(s.k)}" title="${esc((s.k === OTHER_KEY ? "other" : s.k) + ": " + (s.price ? fmtPrice(s.price) : "—") + " · " + compact.format(s.tokens) + " tok")}"></div>`).join("") +
    `</div>`).join("");
  const step = Math.ceil(rows.length / 12);
  const short = (l) => l.replace(/, \d{4}$/, "");
  $("chart-x").innerHTML = rows.map((r, i) =>
    `<span title="${esc(r.label)}">${i % step === 0 ? esc(short(r.label)) : ""}</span>`).join("");
  $("chart-m-cost").disabled = !hasCost;
  $("chart-m-cost").classList.toggle("on", metric === "price");
  $("chart-m-tok").classList.toggle("on", metric === "tokens");
}

[["chart-m-cost", "price"], ["chart-m-tok", "tokens"]].forEach(([id, m]) =>
  $(id).addEventListener("click", () => { report.chartMetric = m; renderChart(); }));
[["rv-chart", "chart"], ["rv-list", "list"]].forEach(([id, v]) =>
  $(id).addEventListener("click", () => {
    report.view = v;
    try { localStorage.setItem("ledger.reportView", v); } catch {}
    renderChart();
  }));

async function runAllowance() {
  const p = new URLSearchParams();
  if ($("r-from").value) p.set("from", $("r-from").value);
  if ($("r-to").value) p.set("to", $("r-to").value);
  if ($("r-hidden").checked) p.set("hidden", "1");
  if (report.excluded.size) p.set("exc", [...report.excluded].join(","));
  $("r-allow").disabled = true;
  try {
    const d = await (await fetch("/api/allowance?" + p)).json();
    $("report-hint").hidden = true;
    $("report-empty").hidden = true;
    $("report-chart").hidden = true;
    $("report-view").hidden = true;
    $("report-body").hidden = false;
    $("report-head").innerHTML = "";
    $("report-rows").innerHTML = "";
    $("r-csv").disabled = true;
    const ok = d.weekly_usd != null;
    $("report-totals").innerHTML = [
      card("Weekly quota used", `${d.weekly_delta}%`, `${d.windows} sample windows · ${d.samples_in_range} samples in range`, "", 0),
      card("Est. cost in windows", d.cost ? fmtPrice(d.cost) : "—", `${d.measured_hours}h measured of ${d.from && d.to ? `${d.from} → ${d.to}` : "open range"}`, "", 1),
      card("Weekly allowance ≈", ok ? fmtPrice(d.weekly_usd) : "—", "est. cost ÷ weekly % used", "", 2),
      card("Monthly allowance ≈", ok ? fmtPrice(d.monthly_usd) : "—", "weekly × 4.33", "", 3),
    ].join("");
    const notes = [];
    if (!ok)
      notes.push(d.samples_in_range < 2
        ? "Not enough quota samples in this range — history accrues while the dashboard is open."
        : "No measurable weekly-quota drop within a known reset cycle in this range — widen the dates or wait for more usage.");
    if (d.unpriced) notes.push(`${d.unpriced} model${d.unpriced === 1 ? "" : "s"} without published rates excluded from cost.`);
    notes.push("Quota is account-wide: usage outside these sessions (and any excluded models' drain) still counts toward the %.");
    const note = $("allowance-note");
    note.textContent = notes.join(" ");
    note.hidden = false;
  } finally {
    $("r-allow").disabled = false;
  }
}

$("r-allow").addEventListener("click", () => runAllowance().catch((e) => setStatus({ error: e.message })));

const csvCell = (v) => /[",\n]/.test(v = String(v ?? "")) ? `"${v.replace(/"/g, '""')}"` : v;

$("report-btn").addEventListener("click", () => {
  fillReportModels();
  $("report").showModal();
});
$("report-close").addEventListener("click", () => $("report").close());
$("report").addEventListener("click", (e) => e.target === $("report") && $("report").close());
$("report").addEventListener("cancel", (e) => {
  if ($("report-models").open) {
    e.preventDefault();
    $("report-models").open = false;
  }
});
$("report-models").addEventListener("change", (e) => {
  const t = e.target;
  if (t.type !== "checkbox") return;
  if (t.id === "report-models-all") {
    report.excluded = t.checked ? new Set() : new Set(report.models);
    fillReportModels();
    return;
  }
  t.checked ? report.excluded.delete(t.value) : report.excluded.add(t.value);
  updateReportModels();
});
$("r-run").addEventListener("click", () => runReport().catch((e) => setStatus({ error: e.message })));
$("r-clear").addEventListener("click", () => {
  ["r-from", "r-to"].forEach((id) => ($(id).value = ""));
  $("r-group").value = "model";
  $("r-hidden").checked = false;
  report.excluded.clear();
  fillReportModels();
  $("report-models").open = false;
  runReport().catch((e) => setStatus({ error: e.message }));
});
$("report-head").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-rk]");
  if (!th) return;
  const k = th.dataset.rk;
  report.asc = report.sortKey === k ? !report.asc : k === "key";
  report.sortKey = k;
  renderReport();
});
$("r-csv").addEventListener("click", () => {
  const cols = reportCols();
  const lines = [cols.map(([, l]) => csvCell(l)).join(",")];
  for (const r of sortedReport())
    lines.push(cols.map(([k]) => csvCell(k === "key" ? r.label : k === "price" ? (r.price == null ? "" : r.price.toFixed(6)) : r[k])).join(","));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n") + "\n"], { type: "text/csv" }));
  a.download = `session-report-${report.group}-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});

setInterval(async () => {
  try {
    const res = await fetch("/api/ping");
    if (!res.ok) return;
    const data = await res.json();
    if (data.runway) setRunway(data.runway);
  } catch {}
}, 20000);
setInterval(() => { if (state.runway) renderRunway(); }, 30000);  // countdowns tick between pings
fillCols();
load().catch((e) => setStatus({ error: e.message }));
