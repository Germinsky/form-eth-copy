const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd = (v) => (v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2));
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const short = (a) => (a ? a.slice(0, 8) + "…" + a.slice(-4) : "");
const layer = (l) => `<span class="layer ${esc(l)}">${esc(l)}</span>`;
const px = (v) => (v == null ? "—" : Number(v).toPrecision(5));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(JSON.stringify(j.detail ?? j));
  return j;
};

async function loadStatus() {
  const h = await api("/api/health");
  $("#status").innerHTML = `chain <b>mainnet</b> · live <b class="neg">OFF</b> · data <b>${esc(h.effective_data_mode || h.data_mode)}</b> (signals ${esc(h.signal_source)}, prices ${esc(h.market || "fixture")}) · ${h.live_signals ? `live cursor block <b>${esc(h.live_signals.cursor_block)}</b> (${esc(h.live_signals.scan_mode)}, ${esc(h.live_signals.tracked_wallets)} wallets, signals ${esc(h.live_signals.counts.signal || 0)}, rejected ${esc(h.live_signals.counts.rejected || 0)})` : `sim t=<b>${h.sim_t}s</b>/${h.sim_end_t}s ${h.tape_done ? "· <b>tape done</b>" : ""}`} · rpc: ${esc(h.rpc.note || (h.rpc.ok ? "ok block " + h.rpc.block : "n/a"))}`;
}

async function loadPortfolio() {
  const p = await api("/api/portfolio");
  $("#pf-stats").innerHTML = [
    ["Equity", usd(p.equity_usd), cls(p.equity_usd - p.start_usd)], ["Cash", usd(p.cash_usd), ""],
    ["Realized", usd(p.realized_pnl_usd), cls(p.realized_pnl_usd)], ["Unrealized", usd(p.unrealized_pnl_usd), cls(p.unrealized_pnl_usd)],
    ["Start", usd(p.start_usd), "muted"],
  ].map(([k, v, c]) => `<div><span class="muted">${k}</span><b class="${c}">${v}</b></div>`).join("") +
    `<div class="muted" style="flex-basis:100%">${esc(p.note)}</div>`;
  $("#pf-open").innerHTML = p.open_positions.length ? `<table><tr><th>token</th><th>size</th><th>entry</th><th>mark</th><th>uPnL</th><th>source wallets</th></tr>${
    p.open_positions.map((x) => `<tr><td>${esc(x.symbol)}</td><td>${usd(x.size_usd)}</td><td>${px(x.entry_price)}</td><td>${px(x.mark_price)}</td><td class="${cls(x.unrealized_usd)}">${usd(x.unrealized_usd)}</td><td>${x.source_wallets.map(short).join(", ")}</td></tr>`).join("")}</table>` : `<p class="muted">none</p>`;
  $("#pf-closed").innerHTML = p.closed_positions.length ? `<table><tr><th>token</th><th>size</th><th>entry</th><th>exit</th><th>gas</th><th>PnL</th></tr>${
    p.closed_positions.map((x) => `<tr><td>${esc(x.symbol)}</td><td>${usd(x.size_usd)}</td><td>${px(x.entry_price)}</td><td>${px(x.exit_price)}</td><td>${usd(x.gas_usd)}</td><td class="${cls(x.pnl_usd)}">${usd(x.pnl_usd)}</td></tr>`).join("")}</table>` : `<p class="muted">none</p>`;
}

async function loadWallets() {
  const ws = await api("/api/wallets");
  $("#wallets").innerHTML = `<table><tr><th>wallet</th><th>status</th><th>gate</th><th>key facts</th><th></th></tr>${ws.map((w) => {
    const g = w.gate, s = w.stats || {};
    const facts = `trips ${esc(s.round_trips ?? "?")} · win ${esc(s.win_rate ?? "?")} · hold ${esc(s.median_hold_min ?? "?")}m · ${esc(s.buys_per_day ?? "?")}/day · left ${esc(s.leftover_pct ?? "?")} · top ${esc(s.top_coin_profit_share ?? "?")}`;
    const unknowns = g.claims.filter((c) => c.layer === "unknown").length;
    return `<tr><td>${esc(w.label || "")}${w.demo ? ' <span class="muted">[demo]</span>' : ""}<div class="addr">${esc(w.address)}</div></td>
      <td class="${esc(w.status)}">${esc(w.status)}</td>
      <td><span class="badge ${g.verdict}">${g.verdict}</span> ${layer("inference")}${unknowns ? " " + layer("unknown") : ""}
        ${g.reasons.length ? `<ul class="reasons">${g.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}</td>
      <td class="muted" style="font-size:11.5px;min-width:220px">${facts} ${layer("fact")}</td>
      <td>${w.status === "parked" ? `<button class="ghost" data-act="watch" data-a="${esc(w.address)}">watch</button>` : `<button class="ghost" data-act="park" data-a="${esc(w.address)}">park</button>`}
          <button class="ghost" data-act="del" data-a="${esc(w.address)}">✕</button></td></tr>`;
  }).join("")}</table>`;
}

async function loadTrades() {
  const t = await api("/api/trades");
  $("#trades").innerHTML = t.length ? `<table><tr><th>#</th><th>side</th><th>token</th><th>signal px</th><th>mid @ +lag</th><th>fill px</th><th>impact %</th><th>usd</th><th>trigger</th></tr>${
    t.map((x) => `<tr><td>${x.id}</td><td class="${x.side === "buy" ? "pos" : "neg"}">${esc(x.side)}</td><td>${esc(x.symbol)}</td><td>${px(x.signal_price)}</td><td>${px(x.mid_at_fill)}</td><td>${px(x.fill_price)}</td><td>${x.impact_pct.toFixed(3)}</td><td>${usd(x.size_usd)}</td><td>${esc(x.trigger_signal)}</td></tr>`).join("")}</table>` : `<p class="muted">no paper trades yet — click "Run paper tick"</p>`;
}

async function loadRecords() {
  const q = new URLSearchParams({ limit: 150 });
  if ($("#f-layer").value) q.set("layer", $("#f-layer").value);
  if ($("#f-kind").value) q.set("kind", $("#f-kind").value);
  const rs = await api("/api/records?" + q);
  $("#records").innerHTML = `<table><tr><th>id</th><th>kind</th><th>layer</th><th>content</th></tr>${rs.map((r) => {
    const c = { ...r.content }; delete c.claims;
    return `<tr><td class="muted">${esc(r.id)}<div class="addr">${esc(r.t_event.slice(11, 19))}Z</div></td><td>${esc(r.kind)}</td><td>${layer(r.layer)}</td><td class="rec-content">${esc(JSON.stringify(c))}</td></tr>`;
  }).join("")}</table>`;
}

const refresh = () => Promise.all([loadStatus(), loadPortfolio(), loadWallets(), loadTrades(), loadRecords()]).catch((e) => alert(e.message));
const post = (p) => api(p, { method: "POST" }).then(refresh).catch((e) => alert(e.message));

$("#btn-tick").onclick = () => post("/api/paper/tick");
$("#btn-run").onclick = () => post("/api/paper/run");
$("#btn-reset").onclick = () => confirm("Reset paper positions? (the_record is kept)") && post("/api/paper/reset");
$("#f-layer").onchange = loadRecords; $("#f-kind").onchange = loadRecords;

$("#wallets").onclick = async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const a = b.dataset.a;
  try {
    if (b.dataset.act === "del") { if (!confirm("Remove " + a + "?")) return; await api("/api/wallets/" + a, { method: "DELETE" }); }
    else await api("/api/wallets/" + a, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: b.dataset.act === "watch" ? "watched" : "parked" }) });
  } catch (err) { alert(err.message); }
  refresh();
};

$("#add-wallet").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), stats = {};
  for (const k of ["round_trips", "win_rate", "median_hold_min", "buys_per_day", "leftover_pct", "top_coin_profit_share"]) {
    const v = f.get(k); if (v !== "") stats[k] = Number(v);
  }
  try {
    await api("/api/wallets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      address: f.get("address").trim(), label: f.get("label") || null, independence_note: f.get("independence_note") || null,
      stats, watch: f.get("watch") === "on" }) });
    e.target.reset();
  } catch (err) { alert(err.message); }
  refresh();
};

refresh();
