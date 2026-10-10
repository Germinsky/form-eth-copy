/* FORM · ETH Copy Desk — operator console (vanilla JS, no build). Paper by default.
   LIVE path: the server returns UNSIGNED calldata; YOUR browser wallet (window.ethereum) signs + broadcasts.
   This page never asks for, stores or transmits a private key / seed phrase. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LAYERS = ["fact", "inference", "hypothesis", "unknown"];

// ---------------------------------------------------------------- formatting
const fmtUsd = (v, d = 2) => (v == null || isNaN(v) ? "—" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const fmtSigned = (v, d = 2) => (v == null || isNaN(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const fmtCompact = (v) => (v == null ? "—" : "$" + Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(v));
const fmtPct = (v, d = 2, signed = true) => (v == null || isNaN(v) ? "—" : (signed && v > 0 ? "+" : "") + v.toFixed(d) + "%");
const fmtPx = (v) => {
  if (v == null || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (a >= 1) return v.toFixed(4);
  const z = Math.max(0, -Math.floor(Math.log10(a)) - 1);
  return v.toFixed(Math.min(z + 4, 14));
};
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "—");
const layerTag = (l) => `<span class="layer ${esc(l)}">${esc(l)}</span>`;
const isUnix = (t) => t > 1e9;
const fmtSimT = (t) => {
  if (t == null) return "—";
  if (isUnix(t)) return new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return `T+${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}${s ? ":" + String(s).padStart(2, "0") : ""}`;
};
const fmtClock = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); } catch { return ""; } };

// ---------------------------------------------------------------- state
const S = {
  health: null, config: null, wallets: [], portfolio: null, stats: null, signals: [], trades: [], live: [], records: [],
  walletFilter: "all", posTab: "open", flowTab: "signals", openWallets: new Set(), openRecs: new Set(),
  layers: new Set(LAYERS), kind: "", search: "", auto: true, recPaused: false, seenRecs: new Set(), seenSig: new Set(),
  first: true, busy: false, lastKpi: {},
};
try { const saved = JSON.parse(localStorage.getItem("form-desk-ui") || "{}"); if (saved.layers) S.layers = new Set(saved.layers); if (saved.auto === false) S.auto = false; } catch {}
const persist = () => localStorage.setItem("form-desk-ui", JSON.stringify({ layers: [...S.layers], auto: S.auto }));

// ---------------------------------------------------------------- api + toasts
function errText(j, status) {
  const d = j?.detail ?? j;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ");
  if (d && typeof d === "object") {
    const parts = [d.error, d.reason].filter(Boolean);
    if (d.reasons?.length) parts.push(d.reasons.join(" · "));
    return parts.join(" — ") || JSON.stringify(d);
  }
  return `HTTP ${status}`;
}
async function api(path, opts = {}) {
  opts.headers = { "X-Form-Desk": "1", ...(opts.headers || {}) };   // CSRF guard header (server requires it on live POSTs)
  if (opts.json !== undefined) { opts.body = JSON.stringify(opts.json); opts.headers["Content-Type"] = "application/json"; delete opts.json; }
  let r;
  try { r = await fetch(path, opts); } catch (e) { const err = new Error("Desk API unreachable — is uvicorn running on :8787?"); err.network = true; throw err; }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(errText(j, r.status)); e.status = r.status; e.body = j; throw e; }
  return j;
}
function toast(kind, title, msg = "", ms = 4200) {
  const icons = { ok: "✓", err: "!", info: "i", warn: "!" };
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="ti">${icons[kind] || "i"}</span><div><b>${esc(title)}</b>${msg ? `<div class="msg">${esc(msg)}</div>` : ""}</div><button aria-label="dismiss">✕</button>`;
  const kill = () => { el.classList.add("out"); setTimeout(() => el.remove(), 200); };
  el.querySelector("button").onclick = kill;
  $("#toasts").appendChild(el);
  if ($("#toasts").children.length > 4) $("#toasts").firstElementChild.remove();
  setTimeout(kill, kind === "err" ? Math.max(ms, 7000) : ms);
}
async function withBusy(btn, fn) {
  if (btn?.classList.contains("busy")) return;
  btn?.classList.add("busy");
  try { return await fn(); } finally { btn?.classList.remove("busy"); }
}

// ---------------------------------------------------------------- tooltip
const tip = $("#tip");
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest("[data-tip]");
  if (!t) { tip.classList.remove("on"); return; }
  tip.innerHTML = t.dataset.tip; tip.classList.add("on");
  const r = t.getBoundingClientRect();
  const x = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2));
  const y = r.top - tip.offsetHeight - 6 < 4 ? r.bottom + 6 : r.top - tip.offsetHeight - 6;
  tip.style.left = x + "px"; tip.style.top = y + "px";
});

// ---------------------------------------------------------------- header chips
function renderChips() {
  const h = S.health; if (!h) return;
  const rpc = h.rpc || {};
  const mode = h.effective_data_mode || h.data_mode;
  const live = h.live_signals;
  const lm = S.liveSt?.mode || "OFF";
  const LCHIP = { OFF: ["chip-danger", "LIVE OFF · PAPER", "Live is off. Paper is the default. LIVE_ENABLED=true without an ARM_CODE refuses to start."],
    DISARMED: ["chip-amber", "LIVE DISARMED", "LIVE_ENABLED, but not armed: prepare + simulate only."],
    ARMED_DRY_RUN: ["chip-blue", "LIVE ARMED · DRY RUN", "Armed, LIVE_BROADCAST=false: nothing can be sent."],
    ARMED_BROADCAST: ["chip-hot", "LIVE ARMED · BROADCAST", "Armed + broadcast: your wallet can sign real trades (≤ cap) after you confirm each one."] }[lm];
  const chips = [`<span class="chip ${LCHIP[0]}" data-tip="${esc(LCHIP[2])}"><i class="dot"></i>${LCHIP[1]}</span>`];
  chips.push(`<span class="chip" data-tip="Chain ID ${esc(h.chain_id)} verified before every client's first read"><span class="k">chain</span><b>Ethereum</b><span class="k">#${esc(h.chain_id)}</span></span>`);
  if (rpc.configured === false) chips.push(`<span class="chip chip-warn" data-tip="${esc(rpc.note || "")}"><i class="dot"></i><span class="k">rpc</span><b>off</b></span>`);
  else if (rpc.ok) chips.push(`<span class="chip chip-ok" data-tip="Latest block (probe cached ≤30s) · ${esc(rpc.calls ?? 0)} RPC calls this session"><i class="dot"></i><span class="k">block</span><b>${Number(rpc.block).toLocaleString("en-US")}</b></span>`);
  else chips.push(`<span class="chip chip-bad" data-tip="${esc(rpc.note || rpc.error || "RPC probe failed")}"><i class="dot"></i><span class="k">rpc</span><b>down</b></span>`);
  const modeTip = `requested ${esc(h.data_mode)} · effective ${esc(mode)} · market ${esc(h.market)} · signals ${esc(h.signal_source)} (requested ${esc(h.signal_source_requested)})`;
  chips.push(`<span class="chip ${mode !== h.data_mode ? "chip-warn" : ""}" data-tip="${modeTip}"><span class="k">data</span><b>${esc(mode)}</b><span class="k">· sig</span><b>${esc(h.signal_source)}</b></span>`);
  if (live) {
    const c = live.counts || {};
    chips.push(`<span class="chip chip-ok" data-tip="cursor block ${esc(live.cursor_block)} · ${esc(live.scan_mode)} scan · poll ${esc(live.poll_seconds)}s · 429s ${esc(live.rpc_throttled_429)}"><i class="dot"></i><span class="k">live feed</span><b>${esc(live.tracked_wallets)}w</b><span class="k">sig</span><b>${esc(c.signal || 0)}</b><span class="k">rej</span><b>${esc(c.rejected || 0)}</b></span>`);
  } else {
    const pct = h.sim_end_t ? Math.min(100, (100 * h.sim_t) / h.sim_end_t) : 0;
    chips.push(`<span class="chip progress" data-tip="Paper sim clock over the fixture tape"><span class="k">sim</span><b>${fmtSimT(h.sim_t)}</b><span class="k">/ ${fmtSimT(h.sim_end_t)}</span>${h.tape_done ? '<span class="k">· done</span>' : ""}<span class="bar" style="width:${pct}%"></span></span>`);
  }
  chips.push(`<span class="chip" data-tip="the_record session id"><span class="k">session</span><b>${esc((h.session || "").replace(/^s-/, "").slice(-13))}</b></span>`);
  $("#chips").innerHTML = chips.join("");
  const pollBtn = $("#btn-poll");
  pollBtn.disabled = !live;
  pollBtn.title = live ? "Scan new blocks for watched-wallet swaps without running the engine (P)" : "Live signals inactive — needs SIGNAL_SOURCE=live, DATA_MODE=rpc|hybrid and ETH_RPC_URL";
  $("#btn-run").title = live ? "One live poll + process signals (R)" : "Run the remaining demo tape to the end (R)";
  $("#btn-tick").disabled = !live && h.tape_done;
  $("#btn-tick").title = !live && h.tape_done ? "Tape finished — Reset paper to replay" : "Advance the paper engine one tick (T)";
  $("#tab-live").disabled = !live;
}

// ---------------------------------------------------------------- KPIs
function sparkPath(vals, w, h, pad = 2) {
  if (vals.length < 2) return null;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  return vals.map((v, i) => `${i ? "L" : "M"}${(pad + (i * (w - 2 * pad)) / (vals.length - 1)).toFixed(1)},${(h - pad - ((v - lo) * (h - 2 * pad)) / span).toFixed(1)}`).join("");
}
function renderKpis() {
  const p = S.portfolio, st = S.stats;
  if (!p || !st) { $("#kpis").innerHTML = Array(7).fill('<div class="kpi skel" style="height:78px"></div>').join(""); return; }
  const eqDelta = p.equity_usd - p.start_usd, eqPct = (100 * eqDelta) / p.start_usd;
  const series = [p.start_usd, ...st.equity_series.map((x) => x.equity_usd)];
  const sp = sparkPath(series, 200, 34);
  const col = eqDelta >= 0 ? "var(--green)" : "var(--red)";
  const sig = st.signals || {};
  const winTxt = st.win_rate == null ? "—" : (st.win_rate * 100).toFixed(0) + "%";
  const exposure = p.open_positions.reduce((a, x) => a + (x.mark_value_usd || 0), 0);
  const k = [
    { id: "eq", cls: "main " + (eqDelta > 0 ? "good" : eqDelta < 0 ? "bad" : ""), lbl: "Equity", tag: layerTag("inference"), val: fmtUsd(p.equity_usd),
      sub: `<span class="${cls(eqDelta)}">${fmtSigned(eqDelta)} (${fmtPct(eqPct)})</span> vs ${fmtUsd(p.start_usd, 0)}`,
      extra: sp ? `<svg class="spark" viewBox="0 0 200 34" preserveAspectRatio="none"><path d="${sp}" fill="none" stroke="${col}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>` : "" },
    { id: "cash", cls: "info", lbl: "Cash", val: fmtUsd(p.cash_usd), sub: `exposure ${fmtUsd(exposure, 0)} · ${p.open_positions.length} open` },
    { id: "upnl", cls: p.unrealized_pnl_usd > 0 ? "good" : p.unrealized_pnl_usd < 0 ? "bad" : "", lbl: "Open PnL", val: `<span class="${cls(p.unrealized_pnl_usd)}">${fmtSigned(p.unrealized_pnl_usd)}</span>`, sub: "marks: mid − impact haircut" },
    { id: "rpnl", cls: p.realized_pnl_usd > 0 ? "good" : p.realized_pnl_usd < 0 ? "bad" : "", lbl: "Realized PnL", val: `<span class="${cls(p.realized_pnl_usd)}">${fmtSigned(p.realized_pnl_usd)}</span>`, sub: `gas paid ${fmtUsd(st.gas_usd_total)}` },
    { id: "win", cls: st.win_rate == null ? "" : st.win_rate >= 0.5 ? "good" : "bad", lbl: "Win rate", val: winTxt, sub: `${st.wins}W / ${st.losses}L · ${st.closed_positions} closed` },
    { id: "gap", cls: st.fill_gap_bps_avg == null ? "" : st.fill_gap_bps_avg > 100 ? "bad" : "info", lbl: "Fill gap", tip: "Average adverse gap between our simulated fill and the source wallet's price, in basis points (lag drift + impact). Positive = worse than the copied wallet.",
      val: st.fill_gap_bps_avg == null ? "—" : `${st.fill_gap_bps_avg.toFixed(0)}<span class="muted" style="font-size:13px"> bps</span>`,
      sub: st.trades ? `max ${st.fill_gap_bps_max?.toFixed(0)} · impact ${st.impact_pct_avg?.toFixed(2)}% · ${st.trades} fills` : "no fills yet" },
    { id: "sig", cls: "", lbl: "Signals", val: String(sig.total || 0), sub: `<span class="pos">${sig.entry || 0} in</span> · <span class="neg">${sig.exit || 0} out</span> · <span class="warn">${sig.pending || 0} pend</span> · ${sig.skip || 0} skip` },
  ];
  $("#kpis").innerHTML = k.map((x) => `<div class="kpi ${x.cls}" ${x.tip ? `data-tip="${esc(x.tip)}"` : ""}>
      <div class="lbl"><span>${x.lbl}</span>${x.tag || ""}</div>
      <div class="val ${S.lastKpi[x.id] !== undefined && S.lastKpi[x.id] !== x.val ? "flash" : ""}">${x.val}</div>
      <div class="sub">${x.sub}</div>${x.extra || ""}</div>`).join("");
  k.forEach((x) => (S.lastKpi[x.id] = x.val));
}

// ---------------------------------------------------------------- equity chart
function renderEquity() {
  const el = $("#equity-chart"), st = S.stats, p = S.portfolio;
  if (!st || !p) { el.innerHTML = '<div class="skel" style="height:100%;border-radius:6px"></div>'; return; }
  const pts = [{ sim_t: 0, equity_usd: p.start_usd }, ...st.equity_series];
  if (pts.length < 2) {
    el.innerHTML = `<div class="empty" style="padding:30px 0">No marks yet — <b>Run tick</b> or <b>Run cycle</b> to draw the curve.</div>`;
    $("#eq-range").textContent = ""; return;
  }
  const W = el.clientWidth || 600, H = el.clientHeight || 118, padL = 4, padR = 58, padT = 10, padB = 16;
  const ys = pts.map((x) => x.equity_usd), xs = pts.map((x) => x.sim_t);
  let lo = Math.min(...ys, p.start_usd), hi = Math.max(...ys, p.start_usd);
  const m = (hi - lo) * 0.15 || 10; lo -= m; hi += m;
  const x0 = xs[0], x1 = xs[xs.length - 1] || 1;
  const X = (t) => padL + ((t - x0) / (x1 - x0 || 1)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  const line = pts.map((q, i) => `${i ? "L" : "M"}${X(q.sim_t).toFixed(1)},${Y(q.equity_usd).toFixed(1)}`).join("");
  const last = pts[pts.length - 1], up = last.equity_usd >= p.start_usd, col = up ? "#2fd18f" : "#ff5a6a";
  const area = `${line}L${X(last.sim_t).toFixed(1)},${H - padB}L${X(x0).toFixed(1)},${H - padB}Z`;
  const ticks = [lo + (hi - lo) * 0.15, (lo + hi) / 2, hi - (hi - lo) * 0.15];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="equity curve">
    <defs><linearGradient id="eqg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".28"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    ${ticks.map((v) => `<line x1="${padL}" x2="${W - padR}" y1="${Y(v)}" y2="${Y(v)}" stroke="#1c2533" stroke-dasharray="2 4"/><text x="${W - padR + 6}" y="${Y(v) + 3}" fill="#6f7b8e" font-size="10" font-family="JetBrains Mono, monospace">${fmtUsd(v, 0)}</text>`).join("")}
    <line x1="${padL}" x2="${W - padR}" y1="${Y(p.start_usd)}" y2="${Y(p.start_usd)}" stroke="#4a5567" stroke-dasharray="4 3"/>
    <path d="${area}" fill="url(#eqg)"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="1.8" stroke-linejoin="round"/>
    <circle cx="${X(last.sim_t)}" cy="${Y(last.equity_usd)}" r="3.5" fill="${col}"/><circle cx="${X(last.sim_t)}" cy="${Y(last.equity_usd)}" r="7" fill="${col}" opacity=".18"/>
    <text x="${padL}" y="${H - 3}" fill="#6f7b8e" font-size="10" font-family="JetBrains Mono, monospace">${fmtSimT(x0)}</text>
    <text x="${W - padR}" y="${H - 3}" fill="#6f7b8e" font-size="10" text-anchor="end" font-family="JetBrains Mono, monospace">${fmtSimT(x1)}</text>
    <line id="eq-x" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="#5aa8ff" stroke-opacity=".5" style="display:none"/>
    <rect x="0" y="0" width="${W}" height="${H}" fill="transparent" id="eq-hit"/></svg><div class="eq-tip" id="eq-tip"></div>`;
  $("#eq-range").textContent = `${fmtUsd(Math.min(...ys))} – ${fmtUsd(Math.max(...ys))}`;
  const hit = $("#eq-hit"), tipEl = $("#eq-tip"), xl = $("#eq-x");
  hit.onmousemove = (e) => {
    const r = el.getBoundingClientRect(), mx = ((e.clientX - r.left) / r.width) * W;
    let best = pts[0]; for (const q of pts) if (Math.abs(X(q.sim_t) - mx) < Math.abs(X(best.sim_t) - mx)) best = q;
    const bx = X(best.sim_t);
    xl.setAttribute("x1", bx); xl.setAttribute("x2", bx); xl.style.display = "";
    tipEl.style.display = "block"; tipEl.style.left = (bx / W) * 100 + "%";
    tipEl.innerHTML = `${fmtSimT(best.sim_t)} · <span class="${cls(best.equity_usd - p.start_usd)}">${fmtUsd(best.equity_usd)}</span>`;
  };
  hit.onmouseleave = () => { tipEl.style.display = "none"; xl.style.display = "none"; };
}

// ---------------------------------------------------------------- wallets
const GATE_SHORT = { "completed round-trips": "trips", "win rate": "win", "median hold (min)": "hold", "spray (buys/day)": "spray", "leftover/unsold share": "left", "top-coin profit concentration": "conc", "independence note": "indep" };
function renderGateLegend() {
  const c = S.config; if (!c) return;
  $("#gate-legend").innerHTML = `<span>gates:</span><span>trips <b>≥${c.gate_min_round_trips}</b></span><span>win <b>≥${c.gate_min_win_rate}</b></span><span>hold <b>≥${c.gate_min_median_hold_min}m</b></span><span>spray <b>≤${c.gate_max_buys_per_day}/d</b></span><span>left <b>≤${c.gate_max_leftover_pct}</b></span><span>conc <b>≤${c.gate_max_top_coin_profit_share}</b></span>${c.gate_require_independence_note ? "<span>indep <b>req</b></span>" : ""}<span>· cluster <b>≥${c.cluster_min_wallets}</b> in ${c.cluster_window_s / 60}m</span>`;
}
function pipsHtml(checks) {
  return `<span class="pips">${checks.map((c) => {
    const st = c.value == null && !c.ok ? "unk" : c.ok ? "ok" : "bad";
    const v = c.value == null ? "missing" : typeof c.value === "number" ? +c.value.toFixed(3) : "set";
    return `<i class="pip ${st}" data-tip="<b>${esc(c.gate)}</b><br>${esc(v)} (need ${esc(c.threshold)})${c.reason ? "<br>" + esc(c.reason) : ""}"></i>`;
  }).join("")}</span>`;
}
function checksTable(g) {
  return `<table class="checks">${g.checks.map((c) => {
    const st = c.value == null && !c.ok ? "unk" : c.ok ? "ok" : "bad";
    const ic = st === "ok" ? '<span class="icon-ok">✓</span>' : st === "bad" ? '<span class="icon-bad">✕</span>' : '<span class="icon-unk">?</span>';
    const v = c.value == null ? '<span class="icon-unk">missing</span>' : typeof c.value === "number" ? esc(+c.value.toFixed(3)) : `<span title="${esc(c.value)}">${esc(String(c.value).slice(0, 26))}${String(c.value).length > 26 ? "…" : ""}</span>`;
    return `<tr><td>${ic}</td><td>${esc(c.gate)}</td><td class="v">${v}</td><td class="th">${esc(c.threshold)}</td></tr>`;
  }).join("")}</table>`;
}
function renderWallets() {
  const all = S.wallets;
  const watched = all.filter((w) => w.status === "watched").length;
  $("#wallet-count").textContent = `${watched}/${all.length} watched`;
  const ws = all.filter((w) => S.walletFilter === "all" || w.status === S.walletFilter)
    .sort((a, b) => (a.status === b.status ? (a.gate.verdict === b.gate.verdict ? (a.label || "").localeCompare(b.label || "") : a.gate.verdict === "PASS" ? -1 : 1) : a.status === "watched" ? -1 : 1));
  if (!all.length) { $("#wallets").innerHTML = `<div class="empty"><div class="big">◎</div>No wallets yet.<br><button class="btn btn-sm btn-primary" data-act="add">+ Add wallet</button></div>`; return; }
  if (!ws.length) { $("#wallets").innerHTML = `<div class="empty">No ${esc(S.walletFilter)} wallets.</div>`; return; }
  $("#wallets").innerHTML = ws.map((w) => {
    const g = w.gate, s = w.stats || {}, open = S.openWallets.has(w.address);
    const unk = g.checks.filter((c) => c.value == null && !c.ok).length;
    const n = (v, d = 0, suf = "") => (v == null ? '<span class="icon-unk">?</span>' : `<b>${esc(typeof v === "number" ? +v.toFixed(d) : v)}${suf}</b>`);
    const action = w.status === "watched"
      ? `<button class="btn btn-xs" data-act="park" data-a="${esc(w.address)}" title="Stop copying this wallet">Unwatch</button>`
      : `<button class="btn btn-xs ${g.verdict === "PASS" ? "btn-primary" : ""}" data-act="watch" data-a="${esc(w.address)}" ${g.verdict !== "PASS" ? `title="Gate FAIL — watching will be refused"` : 'title="Start copying this wallet"'}>Watch</button>`;
    return `<div class="wallet ${esc(w.status)} ${open ? "open" : ""}" data-w="${esc(w.address)}">
      <div class="w-main" data-act="toggle" data-a="${esc(w.address)}" tabindex="0" role="button" aria-expanded="${open}">
        <div class="w-title"><span class="chev">▶</span><div class="w-name"><div class="w-l1"><span class="w-label" title="${esc(w.label || "")}">${esc(w.label || "unlabelled")}</span>${w.demo ? '<span class="tag demo">demo</span>' : ""}</div>
          <span class="addr copy" data-copy="${esc(w.address)}" data-tip="click to copy">${short(w.address)}</span></div></div>
        <div class="w-actions"><span class="tag ${esc(w.status)}">${esc(w.status)}</span><span class="tag ${g.verdict}">${g.verdict}</span>${action}
          <button class="btn btn-xs btn-icon" data-act="del" data-a="${esc(w.address)}" title="Remove wallet" aria-label="remove">✕</button></div>
        <div class="w-meta">${pipsHtml(g.checks)}
          <span class="w-stats"><span>trips ${n(s.round_trips)}</span><span>win ${n(s.win_rate == null ? null : s.win_rate * 100, 0, "%")}</span><span>hold ${n(s.median_hold_min, 0, "m")}</span><span>${n(s.buys_per_day, 1)}/d</span></span></div>
        ${g.reasons.length ? `<div class="w-reason"><span>✕</span><span>${esc(g.reasons[0])}${g.reasons.length > 1 ? ` <span class="muted">+${g.reasons.length - 1} more</span>` : ""}${unk ? ` ${layerTag("unknown")}` : ""}</span></div>` : ""}
      </div>
      <div class="w-detail">${checksTable(g)}
        <ul class="claims">${g.claims.map((c) => `<li>${layerTag(c.layer)}<span>${esc(c.claim)}</span></li>`).join("")}</ul>
        ${w.independence_note ? `<div class="muted sm" style="margin-top:6px">independence: ${esc(w.independence_note)}</div>` : ""}
        <div class="addr" style="margin-top:6px">${esc(w.address)} · source ${esc(s.source || "?")}</div>
      </div></div>`;
  }).join("");
}
$("#wallets").addEventListener("click", async (e) => {
  const cp = e.target.closest("[data-copy]");
  if (cp) { e.stopPropagation(); navigator.clipboard?.writeText(cp.dataset.copy).then(() => toast("info", "Copied", cp.dataset.copy, 1800)); return; }
  const b = e.target.closest("[data-act]"); if (!b) return;
  const a = b.dataset.a, act = b.dataset.act;
  if (act === "add") return openAddWallet();
  if (act === "toggle") { S.openWallets.has(a) ? S.openWallets.delete(a) : S.openWallets.add(a); renderWallets(); return; }
  e.stopPropagation();
  const w = S.wallets.find((x) => x.address === a);
  const name = w?.label || short(a);
  if (act === "del") {
    if (!(await confirmDlg("Remove wallet", `Remove <b>${esc(name)}</b> <span class="addr">${esc(a)}</span> from the desk? Recorded history in the_record is kept.`, "Remove"))) return;
    return withBusy(b, () => api("/api/wallets/" + a, { method: "DELETE" }).then(() => { toast("ok", "Wallet removed", name); refresh(); }).catch((err) => toast("err", "Remove failed", err.message)));
  }
  const status = act === "watch" ? "watched" : "parked";
  return withBusy(b, () => api("/api/wallets/" + a, { method: "PATCH", json: { status } })
    .then(() => toast("ok", status === "watched" ? "Now watching" : "Unwatched (parked)", name))
    .catch((err) => toast("err", status === "watched" ? "Watch refused — gate FAIL" : "Update failed", err.message))
    .finally(refresh));
});
$("#wallets").addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target.matches(".w-main")) { e.preventDefault(); e.target.click(); } });
$("#wallet-filter").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.walletFilter = b.dataset.f; setSeg("#wallet-filter", b); renderWallets(); };

// ---------------------------------------------------------------- positions
function renderPositions() {
  const p = S.portfolio;
  if (!p) { $("#positions").innerHTML = skelRows(4); return; }
  $("#n-open").textContent = p.open_positions.length; $("#n-closed").textContent = p.closed_positions.length;
  const label = (a) => S.wallets.find((w) => w.address === a)?.label || short(a);
  if (S.posTab === "open") {
    if (!p.open_positions.length) { $("#positions").innerHTML = `<div class="empty"><div class="big">∅</div>No open paper positions.<div class="muted sm">Entries need a cluster of ≥${S.config?.cluster_min_wallets ?? 2} passing watched wallets + liquidity ≥ ${fmtCompact(S.config?.liquidity_floor_usd)}.</div></div>`; return; }
    $("#positions").innerHTML = `<table class="t"><thead><tr><th>Token</th><th class="r">Size</th><th class="r">Entry</th><th class="r">Mark</th><th class="r">uPnL</th><th class="r">%</th><th>Sources</th><th>Mark src</th></tr></thead><tbody>${
      p.open_positions.map((x) => { const pct = (100 * x.unrealized_usd) / x.size_usd; return `<tr>
        <td><span class="sym">${esc(x.symbol || short(x.token))}</span><div class="addr">opened ${fmtSimT(x.entry_t)}</div></td>
        <td class="r num">${fmtUsd(x.size_usd, 0)}</td><td class="r num">${fmtPx(x.entry_price)}</td><td class="r num">${fmtPx(x.mark_price)}</td>
        <td class="r num ${cls(x.unrealized_usd)}">${fmtSigned(x.unrealized_usd)}</td><td class="r num ${cls(pct)}">${fmtPct(pct)}</td>
        <td>${x.source_wallets.map((a) => `<span class="addr" data-tip="${esc(a)}">${esc(label(a))}</span>`).join(", ")}</td>
        <td>${x.mark_layer === "unknown" ? `<span class="tag pending" data-tip="${esc(x.mark_note || "")}">stale</span>` : `<span class="muted sm">${esc(x.mark_source || "")}</span>`}</td></tr>`; }).join("")}</tbody></table>`;
  } else {
    if (!p.closed_positions.length) { $("#positions").innerHTML = `<div class="empty">No closed positions yet. Exits mirror the source wallet's sell.</div>`; return; }
    $("#positions").innerHTML = `<table class="t"><thead><tr><th>Token</th><th class="r">Size</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">Gas</th><th class="r">PnL</th><th class="r">%</th><th class="r">Held</th></tr></thead><tbody>${
      p.closed_positions.map((x) => { const pct = (100 * x.pnl_usd) / x.size_usd; const held = x.exit_t && x.entry_t ? Math.round((x.exit_t - x.entry_t) / 60) + "m" : "—"; return `<tr>
        <td><span class="sym">${esc(x.symbol || short(x.token))}</span></td><td class="r num">${fmtUsd(x.size_usd, 0)}</td>
        <td class="r num">${fmtPx(x.entry_price)}</td><td class="r num">${fmtPx(x.exit_price)}</td><td class="r num muted">${fmtUsd(x.gas_usd)}</td>
        <td class="r num ${cls(x.pnl_usd)}">${fmtSigned(x.pnl_usd)}</td><td class="r num ${cls(pct)}">${fmtPct(pct)}</td><td class="r num muted">${held}</td></tr>`; }).join("")}</tbody></table>`;
  }
}
$("#pos-tabs").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.posTab = b.dataset.t; setSeg("#pos-tabs", b); renderPositions(); };

// ---------------------------------------------------------------- flow: signals / trades / live
function renderFlow() {
  $("#n-sig").textContent = S.signals.length; $("#n-tr").textContent = S.trades.length; $("#n-live").textContent = S.live.length;
  const el = $("#flow");
  if (S.flowTab === "signals") {
    if (!S.signals.length) { el.innerHTML = `<div class="empty"><div class="big">⌁</div>No signals processed this paper run.<br><button class="btn btn-sm" data-run="tick">Run tick</button> <button class="btn btn-sm btn-primary" data-run="run">Run cycle</button></div>`; return; }
    el.innerHTML = `<table class="t"><thead><tr><th>Time</th><th>Wallet</th><th>Side</th><th>Token</th><th>Outcome</th></tr></thead><tbody>${S.signals.map((s) => {
      const isNew = !S.first && !S.seenSig.has(s.id);
      return `<tr class="${isNew ? "new" : ""}"><td class="num muted">${fmtSimT(s.t)}</td>
        <td class="wcell" title="${esc(s.wallet_label || "")} ${esc(s.wallet)}"><span>${esc(s.wallet_label || short(s.wallet))}</span>${s.wallet_passed ? "" : ' <span class="tag FAIL" data-tip="wallet not passing/watched at signal time">gate</span>'}</td>
        <td><span class="tag ${esc(s.side)}">${esc(s.side)}</span></td><td><span class="sym">${esc(s.symbol || short(s.token))}</span></td>
        <td class="outc"><span class="tag ${esc(s.kind)}">${esc(s.kind)}</span> <span class="muted sm">${esc(s.outcome.replace(/^skip \((.*)\)$/, "$1"))}</span></td></tr>`;
    }).join("")}</tbody></table>`;
    S.signals.forEach((s) => S.seenSig.add(s.id));
  } else if (S.flowTab === "trades") {
    if (!S.trades.length) { el.innerHTML = `<div class="empty"><div class="big">⇄</div>No paper fills yet.</div>`; return; }
    el.innerHTML = `<table class="t"><thead><tr><th>#</th><th>Fill</th><th>Side</th><th>Token</th><th class="r">Size</th><th class="r">Signal px</th><th class="r">Fill px</th><th class="r" data-tip="adverse gap vs the source wallet's price">Gap</th><th class="r">Impact</th><th>Trigger</th></tr></thead><tbody>${S.trades.map((x) => {
      const gap = x.signal_price && x.fill_price ? ((x.side === "buy" ? x.fill_price / x.signal_price - 1 : 1 - x.fill_price / x.signal_price) * 1e4) : null;
      return `<tr><td class="num muted">${x.id}</td><td class="num muted">${fmtSimT(x.t_fill)}</td><td><span class="tag ${esc(x.side)}">${esc(x.side)}</span></td>
        <td><span class="sym">${esc(x.symbol || short(x.token))}</span></td><td class="r num">${fmtUsd(x.size_usd, 0)}</td>
        <td class="r num">${fmtPx(x.signal_price)}</td><td class="r num">${fmtPx(x.fill_price)}</td>
        <td class="r num ${gap == null ? "" : gap > 0 ? "neg" : "pos"}">${gap == null ? "—" : gap.toFixed(0) + "bp"}</td>
        <td class="r num">${x.impact_pct == null ? "—" : x.impact_pct.toFixed(2) + "%"}</td><td class="addr">${esc(x.trigger_signal)}</td></tr>`;
    }).join("")}</tbody></table>`;
  } else {
    if (!S.health?.live_signals) { el.innerHTML = `<div class="empty">Live signal mode is off.<div class="muted sm">Set SIGNAL_SOURCE=live, DATA_MODE=hybrid|rpc and ETH_RPC_URL, then restart.</div></div>`; return; }
    if (!S.live.length) { el.innerHTML = `<div class="empty">No decoded live txs yet. <button class="btn btn-sm" data-run="poll">Poll now</button></div>`; return; }
    el.innerHTML = `<table class="t"><thead><tr><th>Block</th><th>Status</th><th>Wallet</th><th>Side</th><th>Token</th><th>Reason / size</th><th>Tx</th></tr></thead><tbody>${S.live.map((x) => `<tr>
      <td class="num muted">${esc(x.block)}</td><td><span class="tag ${esc(x.status)}">${esc(x.status)}</span></td><td class="addr">${short(x.wallet)}</td>
      <td>${x.side ? `<span class="tag ${esc(x.side)}">${esc(x.side)}</span>` : ""}</td><td><span class="sym">${esc(x.signal?.symbol || short(x.token))}</span></td>
      <td class="muted sm" style="white-space:normal">${x.signal ? fmtUsd(x.signal.size_usd) + " " + esc(x.signal.base_asset || "") : esc(x.reason || "")}</td>
      <td><a class="addr" href="https://etherscan.io/tx/${esc(x.tx)}" target="_blank" rel="noopener">${short(x.tx)}</a></td></tr>`).join("")}</tbody></table>`;
  }
}
$("#flow-tabs").onclick = (e) => { const b = e.target.closest("button"); if (!b || b.disabled) return; S.flowTab = b.dataset.t; setSeg("#flow-tabs", b); renderFlow(); };
$("#flow").addEventListener("click", (e) => { const b = e.target.closest("[data-run]"); if (b) ops[b.dataset.run](b); });

// ---------------------------------------------------------------- the_record
function recSummary(r) {
  const c = r.content || {}, lbl = (a) => S.wallets.find((w) => w.address === a)?.label || short(a);
  const tok = (x) => x.symbol || short(x.token);
  switch (r.kind) {
    case "signal": return `${lbl(c.wallet)} ${c.side} ${tok(c)}${c.price_usd ? " @ " + fmtPx(c.price_usd) : ""}${c.size_usd ? " · " + fmtUsd(c.size_usd, 0) : ""}`;
    case "skip": return `${c.signal || ""} ${c.side || ""} ${short(c.token)} — ${c.reason}`;
    case "cluster": return `${c.symbol || short(c.token)}: ${(c.wallets || []).map(lbl).join(" + ")} (${c.rule})`;
    case "cluster_pending": return `${c.symbol || short(c.token)}: ${c.have}/${c.need} wallets — ${c.note || ""}`;
    case "paper_fill": return `${(c.side || "").toUpperCase()} ${c.symbol} ${fmtUsd(c.size_usd)} @ ${fmtPx(c.fill_price)} · impact ${c.impact_pct}% · lag ${c.lag_ms / 1000}s`;
    case "paper_exit": return `${c.symbol} closed · PnL ${fmtSigned(c.pnl_usd)} · ${c.trigger}`;
    case "gate_eval": return `${lbl(c.address)} ${c.verdict} → ${c.status}${c.reasons?.length ? " · " + c.reasons[0] : ""}`;
    case "tick": return `${fmtSimT(c.sim_t_from)} → ${fmtSimT(c.sim_t_to)} · ${c.signals} signal(s)`;
    case "mtm": return `equity ${fmtUsd(c.equity_usd)} · realized ${fmtSigned(c.realized_usd)} · open ${fmtSigned(c.unrealized_usd)}`;
    case "session_start": return `data ${c.effective_data_mode} · signals ${c.signal_source} · market ${c.market} · live ${c.live_enabled ? "ON" : "off"}`;
    case "wallet_add": return `${c.label || ""} ${short(c.address)} added`;
    default: {
      if (c.reason || c.note || c.error) return c.reason || c.error || c.note;
      return Object.entries(c).filter(([, v]) => v == null || typeof v !== "object").slice(0, 5).map(([k, v]) => `${k}=${v}`).join(" · ");
    }
  }
}
function renderLayerChips() {
  const counts = Object.fromEntries(LAYERS.map((l) => [l, 0]));
  S.records.forEach((r) => (counts[r.layer] = (counts[r.layer] || 0) + 1));
  $("#layer-chips").innerHTML = LAYERS.map((l, i) => `<button class="lchip ${l}" data-l="${l}" aria-pressed="${S.layers.has(l)}" title="toggle ${l} (${i + 1})">${l}<span class="c">${counts[l]}</span></button>`).join("");
  const kinds = [...new Set(S.records.map((r) => r.kind))].sort();
  const sel = $("#f-kind"), cur = S.kind;
  const want = ["", ...kinds];
  if (sel.options.length !== want.length || want.some((k, i) => sel.options[i]?.value !== k)) {
    sel.innerHTML = want.map((k) => `<option value="${esc(k)}">${k ? esc(k) : "all kinds"}</option>`).join("");
    sel.value = kinds.includes(cur) ? cur : "";
  }
}
function renderRecords() {
  renderLayerChips();
  const q = S.search.toLowerCase();
  const rs = S.records.filter((r) => S.layers.has(r.layer) && (!S.kind || r.kind === S.kind) && (!q || (r.kind + " " + JSON.stringify(r.content)).toLowerCase().includes(q)));
  if (!S.records.length) { $("#records").innerHTML = skelRows(6); return; }
  if (!rs.length) { $("#records").innerHTML = `<div class="empty">No records match the current filters.</div>`; return; }
  $("#records").innerHTML = rs.slice(0, 250).map((r) => {
    const open = S.openRecs.has(r.id), isNew = !S.first && !S.seenRecs.has(r.id);
    return `<div class="rec ${open ? "open" : ""} ${isNew ? "new" : ""}" data-id="${esc(r.id)}" tabindex="0">
      <div class="time">${fmtClock(r.t_event)}</div>
      <div class="line1"><span class="kind k-${esc(r.kind)}">${esc(r.kind)}</span>${layerTag(r.layer)}<span class="rid">${esc(r.id)}</span></div>
      <div class="sum">${esc(recSummary(r))}</div>
      ${open ? `<pre>${esc(JSON.stringify(r.content, null, 2))}\n\nsession ${esc(r.session)} · ${esc(r.t_event)}</pre>` : ""}</div>`;
  }).join("");
  S.records.forEach((r) => S.seenRecs.add(r.id));
}
$("#records").addEventListener("click", (e) => {
  const el = e.target.closest(".rec"); if (!el || e.target.closest("pre")) return;
  const id = el.dataset.id; S.openRecs.has(id) ? S.openRecs.delete(id) : S.openRecs.add(id); renderRecords();
});
$("#records").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches(".rec")) e.target.click(); });
$("#layer-chips").onclick = (e) => { const b = e.target.closest("[data-l]"); if (b) toggleLayer(b.dataset.l); };
function toggleLayer(l) {
  if (S.layers.has(l) && S.layers.size === 1) S.layers = new Set(LAYERS); else S.layers.has(l) ? S.layers.delete(l) : S.layers.add(l);
  persist(); renderRecords();
}
$("#f-kind").onchange = (e) => { S.kind = e.target.value; renderRecords(); };
let searchT; $("#f-search").oninput = (e) => { clearTimeout(searchT); searchT = setTimeout(() => { S.search = e.target.value.trim(); renderRecords(); }, 120); };
$("#rec-pause").onclick = () => { S.recPaused = !S.recPaused; $("#rec-pause").setAttribute("aria-pressed", S.recPaused); $("#rec-pause").textContent = S.recPaused ? "▶" : "❚❚"; $("#rec-pause").title = S.recPaused ? "Resume feed" : "Pause feed"; if (!S.recPaused) refresh(); };

// ---------------------------------------------------------------- quote
const ADDR = /^0x[0-9a-fA-F]{40}$/;
async function runQuote() {
  const token = $("#q-token").value.trim(), size = Number($("#q-size").value) || 500;
  if (!ADDR.test(token)) { $("#q-token").classList.add("invalid"); toast("warn", "Invalid token address", "Expected 0x + 40 hex characters."); $("#q-token").focus(); return; }
  $("#q-token").classList.remove("invalid");
  $("#quote").innerHTML = `<div class="qgrid">${Array(6).fill('<div class="qcell skel" style="height:46px"></div>').join("")}</div><div class="muted sm" style="margin-top:8px">reading Uniswap pools at the latest block…</div>`;
  await withBusy($("#q-go"), async () => {
    try {
      const q = await api(`/api/quote/${token}?size_usd=${size}`);
      const v2 = q.v2, best = q.v3?.best, gate = q.paper_gate;
      const floorOk = gate?.passes_floor;
      $("#quote").innerHTML = `<div class="qhead"><div><span class="sym">${esc(q.symbol || "?")}</span> <span class="addr copy" data-copy="${esc(q.token)}">${short(q.token)}</span></div>
        <span class="muted sm mono">block ${Number(q.block).toLocaleString("en-US")} · ETH ${fmtUsd(q.eth_usd?.eth_usd)}</span></div>
        <div class="qgrid">
          <div class="qcell"><div class="k"><span>V2 price</span>${layerTag(v2.layers?.price_usd || "inference")}</div><div class="v">$${fmtPx(v2.price_usd)}</div></div>
          <div class="qcell"><div class="k"><span>V2 liquidity</span>${layerTag(v2.layers?.liquidity_usd || "inference")}</div><div class="v ${floorOk ? "pos" : "neg"}">${fmtCompact(v2.liquidity_usd)}</div></div>
          <div class="qcell"><div class="k"><span>Buy impact ${fmtUsd(v2.impact.size_usd, 0)}</span></div><div class="v">${fmtPct(v2.impact.buy_pct, 3, false)}</div></div>
          <div class="qcell"><div class="k"><span>Sell impact</span></div><div class="v">${fmtPct(v2.impact.sell_pct, 3, false)}</div></div>
          <div class="qcell"><div class="k"><span>V3 best ${best ? (best.fee / 1e4).toFixed(2) + "%" : ""}</span></div><div class="v">${best ? "$" + fmtPx(best.price_usd) : "—"}</div></div>
          <div class="qcell"><div class="k"><span>V3 TVL ≈</span></div><div class="v">${best ? fmtCompact(best.tvl_usd) : "—"}</div></div>
        </div>
        <div class="gatebar ${floorOk ? "ok" : "bad"}">${floorOk ? "✓" : "✕"} Liquidity floor ${fmtCompact(gate.liquidity_floor_usd)} — paper entry would ${floorOk ? "pass" : "be refused"} ${layerTag("inference")}</div>
        <div class="muted sm" style="margin-top:6px">pair <span class="addr">${short(v2.pair)}</span> · ${esc(q.note)}</div>`;
    } catch (err) {
      const d = err.body?.detail;
      const unk = d && typeof d === "object" && d.layer === "unknown";
      $("#quote").innerHTML = `<div class="gatebar ${unk ? "unk" : "bad"}">${unk ? "?" : "✕"} ${esc(err.message)} ${unk ? layerTag("unknown") : ""}</div>
        ${d?.paper_entry ? `<div class="muted sm" style="margin-top:6px">paper entry: ${esc(d.paper_entry)}</div>` : ""}
        ${err.status === 503 ? '<div class="muted sm" style="margin-top:6px">Set ETH_RPC_URL (see README) and restart to enable quotes.</div>' : ""}`;
      toast(unk ? "warn" : "err", "Quote unavailable", err.message);
    }
  });
}
$("#quote-form").onsubmit = (e) => { e.preventDefault(); runQuote(); };
$("#q-quick").onclick = (e) => { const b = e.target.closest("[data-t]"); if (!b) return; $("#q-token").value = b.dataset.t; runQuote(); };
$("#q-token").addEventListener("paste", () => setTimeout(() => { if (ADDR.test($("#q-token").value.trim())) runQuote(); }, 0));
$("#quote").addEventListener("click", (e) => { const cp = e.target.closest("[data-copy]"); if (cp) navigator.clipboard?.writeText(cp.dataset.copy).then(() => toast("info", "Copied", cp.dataset.copy, 1800)); });
function renderQuoteIdle() {
  const rpcOn = S.health?.rpc?.configured !== false;
  $("#quote").innerHTML = rpcOn
    ? `<div class="empty" style="padding:14px 4px">Paste any ERC-20 address to read its live Uniswap V2/V3 price, liquidity and impact vs the paper floor. Nothing is signed.</div>`
    : `<div class="gatebar unk">? ETH_RPC_URL not configured — quotes disabled ${layerTag("unknown")}</div>`;
}

// ---------------------------------------------------------------- add wallet dialog + gate preview
const dlgW = $("#dlg-wallet"), formW = $("#add-wallet");
const STAT_KEYS = ["round_trips", "win_rate", "median_hold_min", "buys_per_day", "leftover_pct", "top_coin_profit_share"];
function walletPayload() {
  const f = new FormData(formW), stats = {};
  for (const k of STAT_KEYS) { const v = f.get(k); if (v !== "" && v != null && !isNaN(Number(v))) stats[k] = Number(v); }
  return { address: (f.get("address") || "").trim(), label: (f.get("label") || "").trim() || null,
           independence_note: (f.get("independence_note") || "").trim() || null, stats, watch: f.get("watch") === "on" };
}
let prevT, prevSeq = 0;
async function updatePreview() {
  const body = walletPayload(), seq = ++prevSeq;
  const addrEl = formW.elements.address;
  addrEl.classList.toggle("invalid", !!body.address && !ADDR.test(body.address));
  try {
    const g = await api("/api/gates/preview", { method: "POST", json: { address: body.address || null, independence_note: body.independence_note, stats: body.stats } });
    if (seq !== prevSeq) return;
    const willWatch = body.watch && g.verdict === "PASS";
    $("#gate-preview").innerHTML = `<div class="verdict ${g.verdict}"><span>${g.verdict === "PASS" ? "✓ PASS" : "✕ FAIL"}</span><span class="sm">${g.verdict === "PASS" ? `${g.checks.length}/${g.checks.length} gates` : `${g.reasons.length} failing`}</span></div>
      ${checksTable(g)}
      <div class="muted sm" style="margin-top:8px">On save → <b class="${willWatch ? "pos" : ""}">${willWatch ? "watched" : "parked"}</b>${g.exists ? ' · <span class="neg">address already on desk</span>' : ""}${body.address && !g.address_valid ? ' · <span class="neg">address invalid</span>' : ""}</div>`;
    $("#dw-submit").textContent = willWatch ? "Add & watch" : "Add (parked)";
  } catch (err) { if (seq === prevSeq) $("#gate-preview").innerHTML = `<div class="gatebar bad">${esc(err.message)}</div>`; }
}
formW.addEventListener("input", () => { clearTimeout(prevT); prevT = setTimeout(updatePreview, 150); });
function openAddWallet() { formW.reset(); updatePreview(); dlgW.showModal(); setTimeout(() => formW.elements.address.focus(), 30); }
$("#btn-add-wallet").onclick = openAddWallet;
dlgW.addEventListener("click", (e) => { if (e.target.closest("[data-close]") || e.target === dlgW) dlgW.close(); });
formW.onsubmit = (e) => {
  e.preventDefault();
  const body = walletPayload();
  if (!ADDR.test(body.address)) { formW.elements.address.classList.add("invalid"); formW.elements.address.focus(); toast("warn", "Invalid address", "Expected 0x + 40 hex characters."); return; }
  withBusy($("#dw-submit"), async () => {
    try {
      const w = await api("/api/wallets", { method: "POST", json: body });
      dlgW.close();
      toast(w.status === "watched" ? "ok" : "info", w.status === "watched" ? "Wallet added & watched" : "Wallet added (parked)", `${w.label || short(w.address)} · gate ${w.gate.verdict}`);
      S.openWallets.add(w.address); refresh();
    } catch (err) { toast("err", "Add failed", err.message); }
  });
};

// ---------------------------------------------------------------- confirm dialog
function confirmDlg(title, html, yes = "Confirm") {
  const d = $("#dlg-confirm");
  $("#dc-title").textContent = title; $("#dc-body").innerHTML = html; $("#dc-yes").textContent = yes;
  d.returnValue = ""; d.showModal(); setTimeout(() => $("#dc-yes").focus(), 20);
  return new Promise((res) => d.addEventListener("close", () => res(d.returnValue === "yes"), { once: true }));
}

// ---------------------------------------------------------------- operations
const ops = {
  tick: (btn = $("#btn-tick")) => withBusy(btn, async () => {
    try {
      const r = await api("/api/paper/tick", { method: "POST" });
      if (r.done && !r.processed && !S.health?.live_signals) toast("info", "Tape finished", "Reset paper to replay the demo tape.");
      else {
        const outs = r.outcomes || [];
        const fills = outs.filter((o) => /PAPER (BUY|SELL)/.test(o));
        toast(fills.length ? "ok" : "info", `Tick → ${fmtSimT(r.sim_t)}`, `${r.processed} signal(s)` + (fills.length ? ` · ${fills.map((f) => f.split(": ")[1]).join(" · ")}` : outs.length ? ` · ${outs[0].split(": ")[1]}` : ""));
      }
    } catch (err) { toast("err", "Tick failed", err.message); }
    await refresh();
  }),
  run: (btn = $("#btn-run")) => withBusy(btn, async () => {
    try {
      const r = await api("/api/paper/run", { method: "POST" });
      if (r.poll) toast(r.poll.ok === false ? "warn" : "ok", "Live poll + process", `${r.poll.new_signals ?? 0} new signal(s) · blocks ${r.poll.from_block ?? "?"}–${r.poll.to_block ?? "?"}`);
      else toast(r.ticks ? "ok" : "info", r.ticks ? "Cycle complete" : "Nothing to run", r.ticks ? `${r.ticks} tick(s) → ${fmtSimT(r.sim_t)}` : "Tape already finished — Reset paper to replay.");
    } catch (err) { toast("err", "Run failed", err.message); }
    await refresh();
  }),
  poll: (btn = $("#btn-poll")) => {
    if (!S.health?.live_signals) { toast("warn", "Live signals inactive", "Needs SIGNAL_SOURCE=live, DATA_MODE=rpc|hybrid and ETH_RPC_URL."); return; }
    return withBusy(btn, async () => {
      try {
        const r = await api("/api/signals/poll", { method: "POST" });
        toast(r.ok === false ? "warn" : "ok", "Polled live signals", `${r.new_signals ?? 0} new · blocks ${r.from_block ?? "?"}–${r.to_block ?? "?"}${r.note ? " · " + r.note : ""}${r.error ? " · " + r.error : ""}`);
      } catch (err) { toast("err", "Poll failed", err.message); }
      await refresh();
    });
  },
  reset: async (btn = $("#btn-reset")) => {
    if (!(await confirmDlg("Reset paper state", "Clears paper positions, trades, signals-seen and the sim clock.<br><span class='muted'>the_record is append-only and is <b>not</b> cleared. Wallets are kept.</span>", "Reset paper"))) return;
    return withBusy(btn, async () => {
      try { await api("/api/paper/reset", { method: "POST" }); toast("ok", "Paper reset", "Clock at T+00:00 · the_record retained"); S.seenSig.clear(); }
      catch (err) { toast("err", "Reset failed", err.message); }
      await refresh();
    });
  },
};
$("#btn-tick").onclick = () => ops.tick();
$("#btn-run").onclick = () => ops.run();
$("#btn-poll").onclick = () => ops.poll();
$("#btn-reset").onclick = () => ops.reset();
$("#btn-help").onclick = () => $("#dlg-help").showModal();
const setAuto = (on) => { S.auto = on; persist(); $("#btn-auto").setAttribute("aria-pressed", on); $("#auto-label").textContent = on ? "Live 5s" : "Paused"; };
$("#btn-auto").onclick = () => { setAuto(!S.auto); toast("info", S.auto ? "Auto-refresh on" : "Auto-refresh paused", "", 1500); };

// ---------------------------------------------------------------- helpers
function setSeg(sel, btn) { $$(sel + " button").forEach((b) => b.setAttribute("aria-selected", b === btn)); }
const skelRows = (n) => Array(n).fill(0).map((_, i) => `<div class="skel skel-row" style="width:${70 + ((i * 37) % 30)}%"></div>`).join("");

// ---------------------------------------------------------------- refresh loop
let refreshing = null, failStreak = 0;
async function refresh() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    const live = !!S.health?.live_signals;
    const jobs = {
      health: api("/api/health"), wallets: api("/api/wallets"), portfolio: api("/api/portfolio"), stats: api("/api/stats"),
      signals: api("/api/signals/recent?limit=200"), trades: api("/api/trades?limit=200"),
      records: S.recPaused ? Promise.resolve(S.records) : api("/api/records?limit=400"),
      live: live ? api("/api/signals/live?limit=100") : Promise.resolve([]),
      config: S.config ? Promise.resolve(S.config) : api("/api/config"),
      liveSt: api("/api/live/status"), liveCands: api("/api/live/candidates"), liveOrders: api("/api/live/orders?limit=8"),
    };
    const keys = Object.keys(jobs), res = await Promise.allSettled(Object.values(jobs));
    const errs = [];
    res.forEach((r, i) => { if (r.status === "fulfilled") S[keys[i]] = r.value; else errs.push(`${keys[i]}: ${r.reason.message}`); });
    if (errs.length) {
      failStreak++;
      if (failStreak === 1 || failStreak % 12 === 0) toast("err", "Refresh failed", errs[0]);
    } else { if (failStreak) toast("ok", "Reconnected", "Desk API is back."); failStreak = 0; }
    renderLive(); renderChips(); renderGateLegend(); renderKpis(); renderEquity(); renderWallets(); renderPositions(); renderFlow(); renderRecords();
    if (S.first && !$("#quote").innerHTML.trim()) renderQuoteIdle();
    S.first = false;
    $("#foot-updated").textContent = `updated ${new Date().toLocaleTimeString([], { hour12: false })}${errs.length ? " · ⚠ partial" : ""}`;
  })().finally(() => (refreshing = null));
  return refreshing;
}
setInterval(() => { if (S.auto && !document.hidden && !document.querySelector("dialog[open]")) refresh(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.auto) refresh(); });
let rzT; window.addEventListener("resize", () => { clearTimeout(rzT); rzT = setTimeout(renderEquity, 120); });

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]")) return;   // native dialogs handle Esc
  const inField = e.target.closest("input, textarea, select");
  if (e.key === "Escape" && inField) { e.target.blur(); return; }
  if (inField || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  const map = {
    t: () => ops.tick(), r: () => ops.run(), p: () => ops.poll(), n: openAddWallet, a: () => $("#btn-auto").click(), g: () => refresh().then(() => toast("info", "Refreshed", "", 1200)),
    q: () => $("#q-token").focus(), "/": () => $("#f-search").focus(), "?": () => $("#dlg-help").showModal(),
    "1": () => toggleLayer("fact"), "2": () => toggleLayer("inference"), "3": () => toggleLayer("hypothesis"), "4": () => toggleLayer("unknown"),
  };
  if (k === "X" && e.shiftKey) { e.preventDefault(); ops.reset(); return; }
  const fn = map[k.length === 1 ? k.toLowerCase() : k];
  if (fn && !(k.length === 1 && k !== k.toLowerCase() && k !== "?" && k !== "/")) { e.preventDefault(); fn(); }
});


// ---------------------------------------------------------------- LIVE execution (browser wallet signs)
const W = { eth: null, account: null, chainId: null };
function detectWallet() { W.eth = typeof window !== "undefined" && window.ethereum ? window.ethereum : null; return W.eth; }
detectWallet();
window.addEventListener("ethereum#initialized", () => { detectWallet(); renderLive(); }, { once: true });
if (W.eth?.on) {
  W.eth.on("accountsChanged", (a) => { W.account = (a && a[0] || "").toLowerCase() || null; renderLive(); });
  W.eth.on("chainChanged", (c) => { W.chainId = c; renderLive(); });
}
async function connectWallet() {
  if (!detectWallet()) { toast("warn", "No injected wallet", "Install MetaMask (or Rabby / Coinbase Wallet) — see the instructions in the Live panel."); return; }
  try {
    const accts = await W.eth.request({ method: "eth_requestAccounts" });
    W.account = (accts[0] || "").toLowerCase() || null;
    W.chainId = await W.eth.request({ method: "eth_chainId" });
    toast("ok", "Wallet connected", short(W.account) + (W.chainId !== "0x1" ? " · WRONG CHAIN " + W.chainId : " · Ethereum mainnet"));
  } catch (e) { toast("err", "Wallet connection refused", e.message || String(e)); }
  renderLive();
}
const BANNER = {
  OFF: ["lb-off", "LIVE OFF", ["PAPER (DEFAULT)", "SERVER HOLDS NO KEYS", "SIMULATED FILLS ON ETHEREUM MAINNET DATA"]],
  DISARMED: ["lb-disarmed", "LIVE DISARMED", ["PREPARE + SIMULATE ONLY", "ARM WITH ARM_CODE + PHRASE", "SERVER HOLDS NO KEYS"]],
  ARMED_DRY_RUN: ["lb-dry", "LIVE ARMED · DRY RUN", ["LIVE_BROADCAST=false", "NOTHING CAN BE SENT", "SERVER HOLDS NO KEYS"]],
  ARMED_BROADCAST: ["lb-hot", "LIVE ARMED · BROADCAST ON", ["YOUR WALLET SIGNS EVERY TRADE", "CONFIRM EACH ONE", "CAP $CAP / TRADE", "KILL SWITCH ARMED"]],
};
function renderBanner(st) {
  const [c, b, parts] = BANNER[st?.mode || "OFF"];
  const cap = st?.caps?.max_trade_usd ?? 500;
  const el = $("#live-banner"); el.className = c;
  let extra = "";
  if (st?.mode === "ARMED_BROADCAST" || st?.mode === "ARMED_DRY_RUN") extra = `<span class="lb-sep">·</span>DISARMS <span id="lb-ttl"></span>`;
  el.innerHTML = `<span class="lb-dot"></span><b>${b}</b>${parts.map((p) => `<span class="lb-sep">·</span>${esc(p.replace("$CAP", "$" + cap))}`).join("")}${extra}`;
}
function ttlText(until) { const s = Math.max(0, Math.round(until - Date.now() / 1000)); return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`; }
setInterval(() => { const st = S.liveSt; if (st?.armed_until) { const t = ttlText(st.armed_until); const a = $("#lb-ttl"); if (a) a.textContent = "IN " + t; const b = $("#lg-arm-ttl"); if (b) b.textContent = t; } }, 1000);

function gateBox(k, v, cls, tip) { return `<div class="lg ${cls}" ${tip ? `data-tip="${esc(tip)}"` : ""}><span class="k">${k}</span><span class="v">${v}</span></div>`; }
function renderLive() {
  const st = S.liveSt; if (!st) return;
  renderBanner(st);
  const c = st.caps, d = st.daily, k = st.kill_switch;
  const lossPct = Math.min(100, (100 * d.loss_usd) / (c.daily_loss_limit_usd || 1));
  $("#live-gates").innerHTML = [
    gateBox("LIVE_ENABLED", st.live_enabled ? "on" : "off", st.live_enabled ? "on" : "off", "Master switch (.env). true without ARM_CODE refuses to start."),
    gateBox("LIVE_ARMED cfg", st.live_armed_config ? "on" : "off", st.live_armed_config ? "on" : "off", "Config-side consent to allow runtime arming."),
    gateBox("armed", st.armed ? `yes · <span id="lg-arm-ttl">${ttlText(st.armed_until)}</span>` : st.arm_locked ? "LOCKED" : "no", st.armed ? "warn" : st.arm_locked ? "bad" : "off", "Runtime arm: ARM_CODE + typed phrase; expires."),
    gateBox("LIVE_BROADCAST", st.broadcast ? "on" : "off · dry run", st.broadcast ? "warn" : "off", "false = prepare + simulate only; the page never calls eth_sendTransaction."),
    gateBox("kill switch", k.tripped ? "TRIPPED" : "clear", k.tripped ? "bad" : "on", k.reason || "Trips on daily loss ≥ limit, broadcast mismatch, or manually."),
    gateBox("daily loss", `${fmtUsd(d.loss_usd, 0)} / ${fmtUsd(c.daily_loss_limit_usd, 0)}<div class="lossbar"><i style="width:${lossPct}%"></i></div>`, lossPct >= 100 ? "bad" : lossPct > 50 ? "warn" : "on", `UTC ${d.day} · trades ${d.trades}/${c.max_trades_per_day} · ${d.note}`),
    gateBox("cap / trade", `${fmtUsd(c.trade_usd, 0)} ≤ ${fmtUsd(c.max_trade_usd, 0)}`, "on", `hard ceiling $${c.hard_max_trade_usd}`),
    gateBox("impact · slip", `${c.max_impact_pct}% · ${c.max_slippage_bps}bp`, "on", "price impact cap (V2 curve incl. fee) · amountOutMin slippage"),
    gateBox("liq floor · cluster", `${fmtCompact(c.liquidity_floor_usd)} · ≥${c.cluster_min_wallets}w`, "on", "live pool liquidity floor · watched+PASS source wallets required"),
  ].join("");
  // tools
  const tools = [];
  if (st.live_enabled) {
    if (st.armed) tools.push(`<button class="btn btn-sm" data-live="disarm">Disarm</button>`);
    else if (st.live_armed_config && !k.tripped) tools.push(`<button class="btn btn-sm btn-danger" data-live="arm" ${st.arm_locked ? "disabled" : ""}>Arm…</button>`);
    tools.push(k.latched ? `<button class="btn btn-sm" data-live="killreset">Reset kill…</button>` : `<button class="btn btn-sm btn-ghost-danger" data-live="kill" title="Latch the kill switch + disarm">Kill</button>`);
  }
  $("#live-tools").innerHTML = tools.join("");
  // wallet
  const wEl = $("#live-wallet");
  if (!st.live_enabled) {
    wEl.innerHTML = `<div class="grow1"><b>Live is off.</b> <span class="muted">Paper keeps running. To prepare live trades: set <span class="mono">LIVE_ENABLED=true</span>, an <span class="mono">ARM_CODE</span> and <span class="mono">ETH_RPC_URL</span> in <span class="mono">.env</span>, restart (see README › Live trading). Server never holds keys.</span></div>`;
  } else if (!W.eth) {
    wEl.innerHTML = `<div class="grow1"><b>No injected wallet detected.</b> <span class="muted">Open this page in a desktop browser with MetaMask / Rabby / Coinbase Wallet, or in MetaMask Mobile's in-app browser (tunnel 127.0.0.1 yourself). <b>WalletConnect v2</b>: stub only — needs a Reown projectId + the WalletConnect provider script; not bundled (see README). This desk will <u>never</u> ask for a seed phrase or private key.</span></div>`;
  } else if (!W.account) {
    wEl.innerHTML = `<div class="grow1"><span class="muted">Injected wallet found. Connect to prepare + simulate from your address (public address only).</span></div><button class="btn btn-sm btn-primary" data-live="connect">Connect wallet</button>`;
  } else {
    const wrong = W.chainId && W.chainId !== "0x1";
    const allowed = !c.allowed_senders.length || c.allowed_senders.includes(W.account);
    wEl.innerHTML = `<div class="grow1"><span class="k muted">wallet</span> <span class="addr">${esc(W.account)}</span> ${wrong ? `<span class="tag FAIL">chain ${esc(W.chainId)} ≠ 1</span>` : '<span class="tag PASS">mainnet</span>'} ${allowed ? "" : '<span class="tag FAIL">not in LIVE_ALLOWED_SENDERS</span>'}</div><button class="btn btn-xs" data-live="connect">Switch</button>`;
  }
  // candidates + orders
  const cands = S.liveCands || [], orders = S.liveOrders || [];
  const canPrep = st.live_enabled && st.rpc_configured;
  const cRows = cands.slice(0, 6).map((x) => `<div class="lc"><span class="tag ${esc(x.side)}">${esc(x.side)}</span>
      <div><span class="sym">${esc(x.symbol || short(x.token))}</span> <span class="muted sm">${x.side === "buy" ? "paper #" + x.paper_position_id : "live #" + x.live_position_id}</span><div class="why">${esc(x.why)}${x.in_flight ? " · in flight: " + esc(x.in_flight) : ""}</div></div>
      ${canPrep ? `<button class="btn btn-xs ${x.side === "sell" && x.suggested ? "btn-primary" : ""}" data-live="prepare" data-ref="${esc(x.ref)}" ${x.in_flight ? "disabled" : ""} title="Build + simulate the unsigned Uniswap tx">Prepare live trade</button>` : `<span class="muted sm" data-tip="${st.live_enabled ? "ETH_RPC_URL missing" : "LIVE_ENABLED=false"}">live off</span>`}</div>`).join("");
  const oRows = orders.slice(0, 5).map((o) => `<div class="lc"><span class="tag ${esc(o.status)}">${esc(o.status)}</span>
      <div><span class="sym">${esc(o.symbol || short(o.token))}</span> <span class="muted sm">${esc(o.kind)} ${esc(o.side)} ${o.usd_notional ? fmtUsd(o.usd_notional) : ""}</span>
      <div class="why">${esc(o.id)}${o.tx_hash ? ` · <a class="addr" href="https://etherscan.io/tx/${esc(o.tx_hash)}" target="_blank" rel="noopener">${short(o.tx_hash)}</a>` : ""}</div></div>
      <button class="btn btn-xs" data-live="${o.status === "submitted" ? "poll" : "view"}" data-id="${esc(o.id)}">${o.status === "submitted" ? "Check" : "View"}</button></div>`).join("");
  $("#live-cands").innerHTML = `<div class="lh"><span>Candidates (paper-approved)</span><span>${cands.length}</span></div>${cRows || '<div class="muted sm" style="padding:4px 2px">No open paper entries to copy. Live only follows trades the paper engine approved.</div>'}
    ${orders.length ? `<div class="lh" style="margin-top:6px"><span>Recent live orders</span><span>${orders.length}</span></div>${oRows}` : ""}`;
}

function orderHtml(o) {
  const q = o.quote || {}, sim = o.sim || {}, tx = o.tx || {};
  const ethIn = parseInt(tx.value || "0x0", 16) / 1e18;
  const det = (d) => {
    if (d == null) return "";
    if (typeof d !== "object") return String(d);
    if (d.passing) return `${d.passing.length}/${d.sources.length} watched+PASS`;
    if (d.pair) return `pair ${short(d.pair)} @ ${d.block}`;
    return JSON.stringify(d);
  };
  const rows = (o.checks || []).map((c) => { const t = det(c.detail); return `<tr><td>${c.ok ? '<span class="icon-ok">✓</span>' : c.blocking === false ? '<span class="icon-unk">!</span>' : '<span class="icon-bad">✕</span>'}</td><td>${esc(c.check)}</td><td class="v cd" title="${esc(t)}">${esc(t.length > 70 ? t.slice(0, 70) + "…" : t)}</td><td>${layerTag(c.layer)}</td></tr>`; }).join("");
  return `<div class="ord-grid"><div>
      <div class="qhead"><div><span class="tag ${esc(o.side)}">${esc(o.side)}</span> <span class="sym">${esc(o.symbol || short(o.token))}</span> <span class="tag ${esc(o.status)}">${esc(o.status)}</span> <span class="muted sm">${esc(o.kind)}</span></div><span class="muted sm mono">${esc(o.id)}</span></div>
      <div class="qgrid" style="margin-top:8px">
        <div class="qcell"><div class="k"><span>Notional</span>${layerTag("inference")}</div><div class="v">${fmtUsd(o.usd_notional)}</div></div>
        <div class="qcell"><div class="k"><span>${o.side === "buy" ? "ETH in" : "Tokens in (raw)"}</span></div><div class="v">${o.side === "buy" ? ethIn.toFixed(6) : esc(q.amount_in_raw || q.approve_amount_raw || "—")}</div></div>
        <div class="qcell"><div class="k"><span>Impact</span></div><div class="v">${q.impact_pct != null ? q.impact_pct.toFixed(3) + "%" : "—"}</div></div>
        <div class="qcell"><div class="k"><span>Slippage cap</span></div><div class="v">${q.slippage_bps ?? "—"} bp</div></div>
        <div class="qcell"><div class="k"><span>Simulation</span>${layerTag(sim.layer || "unknown")}</div><div class="v ${sim.ok ? "pos" : "neg"}">${sim.ok ? "OK · gas " + sim.gas : "FAILED"}</div></div>
        <div class="qcell"><div class="k"><span>Gas est.</span></div><div class="v">${sim.gas_usd_est != null ? fmtUsd(sim.gas_usd_est) : "—"}</div></div>
      </div>
      ${sim.ok ? "" : `<div class="gatebar unk">? ${esc(sim.error || "")} — ${esc(sim.note || "")}</div>`}
      <div class="muted sm" style="margin-top:8px">to <span class="addr">${esc(tx.to)}</span> ${tx.to === S.liveSt?.router ? "(Uniswap V2 Router02)" : "(token approve)"} · from <span class="addr">${esc(tx.from)}</span> · chain ${esc(tx.chainId)}${q.deadline ? " · deadline " + new Date(q.deadline * 1000).toLocaleTimeString() : ""}</div>
      <div class="muted sm" style="margin:6px 0 3px">Unsigned calldata (what your wallet will be asked to sign)</div><div class="calldata">${esc(tx.data)}</div>
      ${o.blocked_by?.length ? `<ul class="blockers">${o.blocked_by.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : `<div class="gatebar ok">✓ Ready: all gates pass. Clicking confirm asks your wallet to sign — the desk never sees your key.</div>`}
    </div><div><table class="checks ord-checks">${rows}</table></div></div>`;
}
let curOrder = null;
function showOrder(o) {
  curOrder = o;
  $("#do-title").textContent = `Live ${o.kind === "approve" ? "approval" : "trade"} · ${o.symbol || short(o.token)}`;
  $("#do-body").innerHTML = orderHtml(o);
  const st = S.liveSt || {};
  const acts = [`<button class="btn" value="close">Close</button>`];
  if (["prepared", "expired", "rejected"].includes(o.status)) acts.push(`<button type="button" class="btn" id="do-reprep">Re-prepare</button>`);
  if (o.status === "prepared") {
    if (!st.broadcast) acts.push(`<button type="button" class="btn" disabled title="LIVE_BROADCAST=false">Dry run · broadcast disabled</button>`);
    else acts.push(`<button type="button" class="btn btn-danger" id="do-sign" ${o.ready_to_sign && W.account ? "" : "disabled"}>Confirm &amp; sign in wallet</button>`);
  }
  if (o.status === "submitted") acts.push(`<button type="button" class="btn btn-primary" id="do-poll">Check receipt</button>`);
  $("#do-actions").innerHTML = acts.join("");
  $("#do-foot").textContent = st.broadcast ? `expires ${new Date(o.expires_at * 1000).toLocaleTimeString()}` : "DRY RUN: prepare + simulate only";
  const d = $("#dlg-order"); if (!d.open) d.showModal();
  $("#do-reprep")?.addEventListener("click", () => prepareLive(o.ref));
  $("#do-sign")?.addEventListener("click", () => signOrder(o));
  $("#do-poll")?.addEventListener("click", () => pollOrder(o.id, true));
}
async function prepareLive(ref) {
  if (!W.account) { await connectWallet(); if (!W.account) return; }
  try {
    const o = await api("/api/live/prepare", { method: "POST", json: { ref, sender: W.account } });
    toast(o.sim?.ok ? "ok" : "warn", "Live order prepared", `${o.symbol || ""} ${o.kind} · sim ${o.sim?.ok ? "OK" : "FAILED"}${S.liveSt?.broadcast ? "" : " · dry run"}`);
    showOrder(o);
  } catch (err) {
    const d = err.body?.detail;
    toast(d?.layer === "unknown" ? "warn" : "err", "Live prepare refused", err.message);
  }
  refresh();
}
async function signOrder(o) {
  // Client-side guardrails (defence in depth; the server re-checks everything on /confirm)
  try {
    if (!detectWallet()) throw new Error("no injected wallet");
    const chain = await W.eth.request({ method: "eth_chainId" });
    if (chain !== "0x1") throw new Error(`wallet is on chain ${chain}; switch to Ethereum mainnet`);
    const [acct] = await W.eth.request({ method: "eth_accounts" });
    if ((acct || "").toLowerCase() !== o.sender) throw new Error("connected account differs from the order's sender");
    const st = S.liveSt, cap = st.caps.max_trade_usd;
    const usd = o.side === "buy" ? (parseInt(o.tx.value, 16) / 1e18) * (o.quote.eth_usd || 0) : o.usd_notional || 0;
    if (usd > cap + 1e-6) throw new Error(`notional ${fmtUsd(usd)} above cap ${fmtUsd(cap)}`);
    if (![st.router, o.token].includes(o.tx.to)) throw new Error("unexpected tx target");
    $("#dlg-order").close();
    const ok = await confirmDlg("Sign a LIVE transaction?", `<b>${esc(o.kind)} ${esc(o.side)} ${esc(o.symbol || short(o.token))}</b> · ${fmtUsd(usd)} · max slippage ${o.quote.slippage_bps ?? "—"} bp<br><span class="muted">Your wallet will open. Check the amount, the destination <span class="addr">${short(o.tx.to)}</span> and the network before signing. Real funds, irreversible.</span>`, "Confirm & open wallet");
    if (!ok) { toast("info", "Cancelled", "Nothing was signed."); return; }
    const r = await api(`/api/live/orders/${o.id}/confirm`, { method: "POST", json: { sender: acct.toLowerCase(), confirm: true } });
    if (!S.liveSt.broadcast) throw new Error("dry run");   // belt and braces
    let hash;
    try {
      hash = await W.eth.request({ method: "eth_sendTransaction", params: [{ from: r.tx.from, to: r.tx.to, value: r.tx.value, data: r.tx.data }] });
    } catch (e) {
      await api(`/api/live/orders/${o.id}/rejected`, { method: "POST", json: { reason: e.message || String(e) } }).catch(() => {});
      toast("info", "Not signed", e.message || "Rejected in wallet"); refresh(); return;
    }
    const v = await api(`/api/live/orders/${o.id}/submitted`, { method: "POST", json: { tx_hash: hash } });
    toast(v.status === "mismatch" ? "err" : "ok", v.status === "mismatch" ? "Broadcast MISMATCH — kill switch latched" : "Broadcast by your wallet", short(hash));
    pollOrder(o.id);
  } catch (err) { toast("err", "Live sign aborted", err.message); }
  refresh();
}
async function pollOrder(id, once = false) {
  for (let i = 0; i < (once ? 1 : 40); i++) {
    try {
      const o = await api(`/api/live/orders/${id}/poll`, { method: "POST" });
      if (o.status !== "submitted") { toast(o.status === "confirmed" ? "ok" : "err", `Live order ${o.status}`, o.tx_hash ? short(o.tx_hash) : ""); refresh(); if (once) showOrder(o); return; }
      if (once) { toast("info", "Still pending", o.verified ? "seen on chain, awaiting receipt" : "not yet visible on chain"); return; }
    } catch (err) { toast("err", "Poll failed", err.message); return; }
    await new Promise((r) => setTimeout(r, 6000));
  }
}
$("#p-live").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-live]"); if (!b) return;
  const a = b.dataset.live;
  if (a === "connect") return connectWallet();
  if (a === "prepare") return withBusy(b, () => prepareLive(b.dataset.ref));
  if (a === "view") return api(`/api/live/orders/${b.dataset.id}`).then(showOrder).catch((err) => toast("err", "Load failed", err.message));
  if (a === "poll") return withBusy(b, () => pollOrder(b.dataset.id, true));
  if (a === "disarm") return api("/api/live/disarm", { method: "POST" }).then(() => { toast("ok", "Disarmed"); refresh(); });
  if (a === "kill") {
    if (!(await confirmDlg("Trip kill switch", "Latches the kill switch and disarms. New live entries are blocked until you reset it with your ARM_CODE.", "Trip kill switch"))) return;
    return api("/api/live/kill", { method: "POST", json: { reason: "dashboard" } }).then(() => { toast("warn", "Kill switch latched"); refresh(); });
  }
  if (a === "arm" || a === "killreset") {
    const f = $("#arm-form"); f.reset(); f.dataset.mode = a;
    $("#da-title").textContent = a === "arm" ? "Arm live trading" : "Reset kill switch";
    $("#arm-phrase-hint").textContent = a === "arm" ? `"${S.liveSt.arm_phrase}"` : "(not needed)";
    f.elements.phrase.required = a === "arm"; f.elements.phrase.parentElement.style.display = a === "arm" ? "" : "none";
    $("#arm-note").innerHTML = a === "arm" ? `Arms for ${Math.round(S.liveSt.caps.arm_ttl_s / 60)} min, then disarms itself (LIVE_ARM_TTL_S). ${S.liveSt.broadcast ? '<b class="neg">LIVE_BROADCAST is ON: once armed, confirmed trades go to your wallet for signing.</b>' : "LIVE_BROADCAST=false: arming still cannot send anything (dry run)."}` : "Clears a latched kill switch. A daily-loss trip still applies until the next UTC day.";
    $("#dlg-arm").showModal(); setTimeout(() => f.elements.code.focus(), 30);
  }
});
$("#dlg-arm").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) $("#dlg-arm").close(); });
$("#arm-form").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target, code = f.elements.code.value, phrase = f.elements.phrase.value;
  const path = f.dataset.mode === "arm" ? "/api/live/arm" : "/api/live/kill/reset";
  withBusy($("#arm-submit"), async () => {
    try {
      await api(path, { method: "POST", json: f.dataset.mode === "arm" ? { code, phrase } : { code } });
      f.reset(); $("#dlg-arm").close();
      toast("warn", f.dataset.mode === "arm" ? "LIVE ARMED" : "Kill switch reset", f.dataset.mode === "arm" ? (S.liveSt.broadcast ? "Broadcast ON — each trade still needs your confirm + wallet signature." : "Dry run: nothing can be sent.") : "");
    } catch (err) { f.elements.code.value = ""; toast("err", "Refused", err.message); }
    refresh();
  });
};
$("#dlg-order").addEventListener("close", () => { curOrder = null; });

// ---------------------------------------------------------------- boot
setAuto(S.auto);
renderKpis(); renderEquity(); $("#wallets").innerHTML = skelRows(8); $("#positions").innerHTML = skelRows(3); $("#flow").innerHTML = skelRows(5); $("#records").innerHTML = skelRows(8);
refresh();
