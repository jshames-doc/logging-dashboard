const $ = (id) => document.getElementById(id);
const state = { rows: [], sortKey: "created", asc: false, multi: new Set(), models: new Set(), limit: 2000 };
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
    (!q || `${r.title} ${r.id} ${r.workspace} ${r.model}`.toLowerCase().includes(q)));
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
  const t = rows.reduce((a, r) => ({ input: a.input + r.input, output: a.output + r.output, cached: a.cached + r.cached, messages: a.messages + r.messages, price: a.price + (r.price || 0) }),
    { input: 0, output: 0, cached: 0, messages: 0, price: 0 });
  const sessions = new Set(rows.map((r) => r.id)).size;
  const hit = t.input + t.cached ? (t.cached / (t.input + t.cached)) * 100 : 0;
  $("totals").innerHTML = [
    card("Sessions", fmt.format(sessions), `${fmt.format(rows.length)} model rows`, "", 0),
    card("Responses", compact.format(t.messages), fmt.format(t.messages), "", 1),
    card("Input", compact.format(t.input), fmt.format(t.input), "in", 2),
    card("Output", compact.format(t.output), fmt.format(t.output), "out", 3),
    card("Cached", compact.format(t.cached), fmt.format(t.cached), "ca", 4),
    card("Cache hit", `${hit.toFixed(1)}%`, "cached ÷ (input + cached)", "", 5),
    card("Est. cost", t.price ? fmtPrice(t.price) : "—", "Enterprise SaaS rates", "", 6),
  ].join("");
}

function mix(r) {
  const tot = r.input + r.output + r.cached;
  if (!tot) return `<div class="mixbar"></div>`;
  const p = (v) => ((v / tot) * 100).toFixed(2);
  return `<div class="mixbar" title="in ${p(r.input)}% · out ${p(r.output)}% · cached ${p(r.cached)}%"><i class="in" style="width:${p(r.input)}%"></i><i class="out" style="width:${p(r.output)}%"></i><i class="ca" style="width:${p(r.cached)}%"></i></div>`;
}

function render() {
  const rows = sorted(filtered());
  renderTotals(rows);
  $("empty").hidden = rows.length > 0;
  const shown = rows.slice(0, state.limit);
  $("rows").innerHTML = shown.map((r, i) => `
    <tr data-session-id="${esc(r.id)}" tabindex="0" aria-haspopup="dialog" title="Double-click or press Enter to view session details" class="${r.hidden ? "hidden-row" : ""} ${state.multi.has(r.id) ? "multi" : ""}" style="animation-delay:${Math.min(i, 30) * 12}ms">
      <td class="dt">${dt(r.created)}</td>
      <td class="dt">${dt(r.last_activity)}</td>
      <td class="sess"><div class="t">${esc(r.title || "Untitled")}${r.hidden ? '<span class="tag">hidden</span>' : ""}</div><div class="id">${esc(r.id)}</div></td>
      <td class="ws" title="${esc(r.workspace)}">${esc(base(r.workspace))}</td>
      <td class="model" title="Used ${esc(new Date(r.first * 1000).toLocaleString())} → ${esc(new Date(r.last * 1000).toLocaleString())}">${esc(r.model)}</td>
      <td class="num">${fmt.format(r.messages)}</td>
      <td class="num in">${fmt.format(r.input)}</td>
      <td class="num out">${fmt.format(r.output)}</td>
      <td class="num ca">${fmt.format(r.cached)}</td>
      <td class="num price" title="${esc(r.price_model || "no published rate")}">${r.price == null ? "—" : fmtPrice(r.price)}</td>
      <td>${mix(r)}</td>
    </tr>`).join("");
  const rest = rows.length - shown.length;
  $("more").hidden = !rest;
  if (rest)
    $("more").textContent = `Showing first ${fmt.format(shown.length)} of ${fmt.format(rows.length)} rows — click to show ${fmt.format(Math.min(state.limit, rest))} more, or narrow the filters`;
  document.querySelectorAll("thead th[data-k]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === state.sortKey);
    th.classList.toggle("asc", th.dataset.k === state.sortKey && state.asc);
  });
}

let sessionOpener = null;

function showSession(id, opener = null) {
  const models = state.rows.filter((r) => r.id === id).sort((a, b) => a.first - b.first);
  if (!models.length) return;
  const session = models[0];
  const totals = models.reduce((a, r) => ({
    messages: a.messages + r.messages, input: a.input + r.input,
    output: a.output + r.output, cached: a.cached + r.cached,
    price: a.price + (r.price ?? 0), priced: a.priced + (r.price != null ? 1 : 0),
  }), { messages: 0, input: 0, output: 0, cached: 0, price: 0, priced: 0 });
  const hit = totals.input + totals.cached ? totals.cached / (totals.input + totals.cached) * 100 : 0;
  const unpriced = models.length - totals.priced;
  const detail = (label, value, cls = "") => `<div class="${cls}"><dt>${label}</dt><dd>${value}</dd></div>`;
  const stat = (label, value, cls = "") => `<div class="session-stat ${cls}"><div class="lbl">${label}</div><div class="val">${value}</div></div>`;
  $("session-title").textContent = session.title || "Untitled";
  $("session-meta").innerHTML = [
    detail("Session ID", esc(session.id)),
    detail("Visibility", session.hidden ? "Hidden" : "Visible"),
    detail("Workspace", esc(session.workspace || "—"), "wide"),
    detail("Created", dt(session.created), "dt"),
    detail("Last activity", dt(session.last_activity), "dt"),
  ].join("");
  $("session-stats").innerHTML = [
    stat("Responses", fmt.format(totals.messages)),
    stat("Input · includes cache write", fmt.format(totals.input), "in"),
    stat("Output", fmt.format(totals.output), "out"),
    stat("Cached", fmt.format(totals.cached), "ca"),
    stat("Cache hit", `${hit.toFixed(1)}%`),
    stat(unpriced && totals.priced ? "Est. cost · partial" : "Est. cost", totals.priced ? fmtPrice(totals.price) : "—"),
  ].join("");
  $("session-models").innerHTML = models.map((r) => `
    <tr>
      <td>${esc(r.model)}</td>
      <td class="num">${fmt.format(r.messages)}</td>
      <td class="num in">${fmt.format(r.input)}</td>
      <td class="num">${fmt.format(r.cache_write || 0)}</td>
      <td class="num out">${fmt.format(r.output)}</td>
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

function setData(data) {
  state.rows = data.rows;
  state.limit = 2000;
  const counts = {};
  data.rows.forEach((r) => (counts[r.id] = (counts[r.id] || 0) + 1));
  state.multi = new Set(Object.keys(counts).filter((k) => counts[k] > 1));
  $("dbpath").textContent = data.db;
  fillModels([...new Set(data.rows.map((r) => r.model))].sort());
  fillSelect($("f-ws"), [...new Set(data.rows.map((r) => r.workspace))].sort((a, b) => base(a).localeCompare(base(b))), "All workspaces");
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
document.addEventListener("click", (e) => {
  if (!$("f-model").contains(e.target)) $("f-model").open = false;
  if (!$("report-models").contains(e.target)) $("report-models").open = false;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("f-model").open) {
    $("f-model").open = false;
    $("f-model-label").focus();
  }
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
const report = { models: [], excluded: new Set(), rows: [], totals: null, group: "model", sortKey: "price", asc: false };
const GROUP_LABEL = { model: "Model", day: "Day", week: "Week", month: "Month" };

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
  return cols.concat([["messages", "Responses"], ["input", "Input"], ["output", "Output"], ["cached", "Cached"], ["price", "Est. cost"]]);
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
      <td${report.group === "model" ? ` class="model" title="${esc(r.price_model || "no published rate")}"` : ""}>${esc(r.label)}</td>
      <td class="num">${fmt.format(r.sessions)}</td>
      ${report.group !== "model" ? `<td class="num">${fmt.format(r.models)}</td>` : ""}
      <td class="num">${fmt.format(r.messages)}</td>
      <td class="num in">${fmt.format(r.input)}</td>
      <td class="num out">${fmt.format(r.output)}</td>
      <td class="num ca">${fmt.format(r.cached)}</td>
      <td class="num price"${r.unpriced ? ` title="excludes ${r.unpriced} unpriced model${r.unpriced === 1 ? "" : "s"}"` : ""}>${r.price == null ? "—" : fmtPrice(r.price)}</td>
    </tr>`).join("");
  const t = report.totals;
  if (t) {
    const hit = t.input + t.cached ? (t.cached / (t.input + t.cached)) * 100 : 0;
    $("report-totals").innerHTML = [
      card("Sessions", fmt.format(t.sessions), `${fmt.format(t.models)} models`, "", 0),
      card("Responses", compact.format(t.messages), fmt.format(t.messages), "", 1),
      card("Input", compact.format(t.input), fmt.format(t.input), "in", 2),
      card("Output", compact.format(t.output), fmt.format(t.output), "out", 3),
      card("Cached", compact.format(t.cached), fmt.format(t.cached), "ca", 4),
      card("Cache hit", `${hit.toFixed(1)}%`, "", "", 5),
      card("Est. cost", t.price ? fmtPrice(t.price) : "—", t.unpriced ? `${t.unpriced} unpriced model${t.unpriced === 1 ? "" : "s"}` : "", "", 6),
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
    report.sortKey = data.group === "model" ? "price" : "key";
    report.asc = data.group !== "model";
    renderReport();
    $("r-csv").disabled = !report.rows.length;
  } finally {
    $("r-run").disabled = false;
  }
}

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

setInterval(() => fetch("/api/ping").catch(() => {}), 20000);
load().catch((e) => setStatus({ error: e.message }));
