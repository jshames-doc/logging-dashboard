const $ = (id) => document.getElementById(id);
const state = { rows: [], sortKey: "created", asc: false, multi: new Set() };
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
  const model = $("f-model").value, ws = $("f-ws").value, hidden = $("f-hidden").checked;
  const from = dayStart($("f-from").value), to = dayStart($("f-to").value);
  return state.rows.filter((r) =>
    (hidden || !r.hidden) &&
    (!model || r.model === model) &&
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

function renderTotals(rows) {
  const t = rows.reduce((a, r) => ({ input: a.input + r.input, output: a.output + r.output, cached: a.cached + r.cached, messages: a.messages + r.messages, price: a.price + (r.price || 0) }),
    { input: 0, output: 0, cached: 0, messages: 0, price: 0 });
  const sessions = new Set(rows.map((r) => r.id)).size;
  const hit = t.input + t.cached ? (t.cached / (t.input + t.cached)) * 100 : 0;
  const card = (lbl, val, sub, sw = "", i = 0) =>
    `<div class="card" style="animation-delay:${i * 60}ms"><div class="lbl">${sw ? `<i class="sw ${sw}"></i>` : ""}${lbl}</div><div class="val">${val}</div><div class="sub">${sub}</div></div>`;
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
  $("rows").innerHTML = rows.slice(0, 2000).map((r, i) => `
    <tr class="${r.hidden ? "hidden-row" : ""} ${state.multi.has(r.id) ? "multi" : ""}" style="animation-delay:${Math.min(i, 30) * 12}ms">
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
  document.querySelectorAll("thead th[data-k]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === state.sortKey);
    th.classList.toggle("asc", th.dataset.k === state.sortKey && state.asc);
  });
}

function fillSelect(el, values, label) {
  const cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` + values.map((v) => `<option value="${esc(v)}">${esc(label.includes("workspace") ? base(v) + "  —  " + v : v)}</option>`).join("");
  el.value = values.includes(cur) ? cur : "";
}

function setData(data) {
  state.rows = data.rows;
  const counts = {};
  data.rows.forEach((r) => (counts[r.id] = (counts[r.id] || 0) + 1));
  state.multi = new Set(Object.keys(counts).filter((k) => counts[k] > 1));
  $("dbpath").textContent = data.db;
  fillSelect($("f-model"), [...new Set(data.rows.map((r) => r.model))].sort(), "All models");
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
["q", "f-model", "f-ws", "f-from", "f-to", "f-hidden"].forEach((id) => $(id).addEventListener("input", render));
$("clear").addEventListener("click", () => {
  ["q", "f-model", "f-ws", "f-from", "f-to"].forEach((id) => ($(id).value = ""));
  $("f-hidden").checked = false;
  render();
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

load().catch((e) => setStatus({ error: e.message }));
