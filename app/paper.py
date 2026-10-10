"""Paper engine. Simulated fills only — there is no signer, no key, no order routing here.

Flow per tick (sim clock advances TICK_SECONDS through the signal tape):
  signal -> watched? -> wallet gate PASS? (fail closed) -> liquidity floor -> cluster rule
         -> simulated fill with lag + price-impact haircut -> the_record

Live signals (SIGNAL_SOURCE=live, app.live_signals.LiveSource, `realtime`): each tick first polls the chain
for new watched-wallet swaps, then the clock jumps to the scanned head block's timestamp. Live buys older
than SIGNAL_MAX_AGE_S at processing time are skipped as stale; sells (exits) are never gated.

Market data comes from app.market: fixture numbers (demo) or live Uniswap V2 pool reads (rpc/hybrid).
A failed/missing live read is layer=unknown and the entry is refused (fail closed).
"""
from __future__ import annotations

import json
import threading

from .config import Settings, assert_live_dark
from .gates import score_wallet
from .market import FixtureMarket, QuoteUnavailable
from .record import Record
from .signals import DemoSource
from .store import Store


class PaperEngine:
    def __init__(self, s: Settings, store: Store, record: Record, source: DemoSource, market=None):
        self.s, self.store, self.rec, self.src = s, store, record, source
        self.mkt = market or FixtureMarket(source)
        self.realtime = bool(getattr(source, "realtime", False))
        self._tick_lock = threading.Lock()
        self.last_poll: dict | None = None

    # ------------------------------------------------------------------ clock
    @property
    def sim_t(self) -> int:
        return int(self.store.get_state("sim_t", 0))

    @property
    def done(self) -> bool:
        return False if self.realtime else self.sim_t >= self.src.end_t

    # ------------------------------------------------------------------ tick
    def tick(self) -> dict:
        assert_live_dark(self.s)  # belt and braces: refuse to run if someone flipped the flag
        with self._tick_lock:   # button + background loop must not interleave
            return self._tick()

    def _tick(self) -> dict:
        start = self.sim_t
        poll = None
        if self.realtime:
            poll = self.last_poll = self.src.poll()   # never raises; errors are recorded, cursor held
            end = max(start, self.src.end_t)
        else:
            if self.done:
                return {"done": True, "sim_t": start, "processed": 0}
            end = min(start + self.s.tick_seconds, self.src.end_t)
        self._now_t = end
        seen = {r["id"] for r in self.store.q("SELECT id FROM signals_seen")}
        batch = [x for x in self.src.signals_until(end) if x["id"] not in seen]
        outcomes = [self._handle(sig) for sig in batch]
        self.store.set_state("sim_t", end)
        if self.realtime and not batch:   # idle live polls don't spam the_record (errors are recorded by the scanner)
            return {"done": False, "sim_t": end, "processed": 0, "outcomes": [], "poll": poll}
        pf = self.portfolio()
        self.rec.append("tick", "fact", {
            "sim_t_from": start, "sim_t_to": end, "signals": len(batch), "outcomes": outcomes,
            "source": self.src.name, "market": self.mkt.name})
        self.rec.append("mtm", "inference", {
            "sim_t": end, "equity_usd": pf["equity_usd"], "realized_usd": pf["realized_pnl_usd"],
            "unrealized_usd": pf["unrealized_pnl_usd"],
            "note": "marks are simulated mid minus impact haircut, not executable quotes"})
        out = {"done": self.done, "sim_t": end, "processed": len(batch), "outcomes": outcomes}
        if poll is not None:
            out["poll"] = poll
        return out

    def run_all(self, max_ticks: int = 500) -> dict:
        if self.realtime:   # a live feed has no end: "run" = one poll + process
            r = self.tick()
            return {"ticks": 1, "sim_t": self.sim_t, "done": False, "poll": r.get("poll")}
        ticks = 0
        while not self.done and ticks < max_ticks:
            self.tick()
            ticks += 1
        return {"ticks": ticks, "sim_t": self.sim_t, "done": self.done}

    # ------------------------------------------------------------------ signals
    def _mark_seen(self, sig: dict, passed: bool, outcome: str) -> str:
        self.store.x("INSERT OR IGNORE INTO signals_seen(id,wallet,token,side,t,wallet_passed,outcome) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (sig["id"], sig["wallet"], sig["token"], sig["side"], sig["t"], int(passed), outcome))
        return f"{sig['id']}: {outcome}"

    def _skip(self, sig: dict, why: str, layer: str = "inference", passed: bool = False, **extra) -> str:
        self.rec.append("skip", layer, {"signal": sig["id"], "wallet": sig["wallet"], "token": sig["token"],
                                        "side": sig["side"], "reason": why, **extra})
        return self._mark_seen(sig, passed, f"skip ({why})")

    def _handle(self, sig: dict) -> str:
        tok = self.src.token(sig["token"]) or {}
        self.rec.append("signal", "fact", {
            "signal": sig["id"], "wallet": sig["wallet"], "token": sig["token"], "symbol": tok.get("symbol"),
            "side": sig["side"], "t": sig["t"], "price_usd": sig.get("price_usd"),
            "size_usd": sig.get("size_usd"), "tx": sig.get("tx"),
            "source": self.src.name, "demo": self.src.name == "fixture"})

        # Exits first: a sell from a wallet we copied always closes our paper position,
        # even if that wallet has since been parked. Reducing risk is never gated.
        if sig["side"] == "sell":
            return self._sell(sig, tok)

        if self.realtime:
            age = getattr(self, "_now_t", sig["t"]) - sig["t"]
            if age > self.s.signal_max_age_s:
                return self._skip(sig, "stale live signal", age_s=age, max_age_s=self.s.signal_max_age_s)
        wallet = self.store.get_wallet(sig["wallet"])
        if not wallet:
            return self._skip(sig, "wallet unknown to desk")
        gate = score_wallet(wallet, self.s)
        if wallet["status"] != "watched":
            return self._skip(sig, f"wallet parked (gate {gate['verdict']})", gate_reasons=gate["reasons"])
        if gate["verdict"] != "PASS":
            return self._skip(sig, "wallet gate FAIL (fail closed)", gate_reasons=gate["reasons"])
        return self._buy(sig, tok)

    def _open_position(self, token: str) -> dict | None:
        rows = self.store.q("SELECT * FROM positions WHERE token=? AND status='open'", (token,))
        return rows[0] if rows else None

    def _buy(self, sig: dict, tok: dict) -> str:
        pos = self._open_position(sig["token"])
        if pos:
            return self._skip(sig, "already holding token", passed=True)

        live = self.mkt.is_live(sig["token"])
        try:
            liq = self.mkt.liquidity_at(sig["token"], sig["t"])
            pool = self.mkt.pool_info(sig["token"])
        except QuoteUnavailable as e:
            return self._skip(sig, "liquidity unknown -> fail closed", layer="unknown", passed=True,
                              data="uniswap-v2 live read", cause=e.reason)
        if pool:
            self.rec.append("pool_read", "fact", {"signal": sig["id"], "token": sig["token"], **pool,
                                                  "note": "reserves are facts at block; USD values are inference"})
            tok = {**tok, "symbol": tok.get("symbol") or pool.get("symbol")}
        if liq is None:
            return self._skip(sig, "liquidity unknown -> fail closed", layer="unknown", passed=True)
        if liq < self.s.liquidity_floor_usd:
            return self._skip(sig, "below liquidity floor", passed=True, liquidity_usd=round(liq, 2),
                              floor_usd=self.s.liquidity_floor_usd, data="uniswap-v2 live" if live else "fixture")

        # Cluster rule: distinct passing watched wallets buying this token inside the window.
        lo = sig["t"] - self.s.cluster_window_s
        prior = self.store.q("SELECT DISTINCT wallet FROM signals_seen WHERE token=? AND side='buy' "
                             "AND wallet_passed=1 AND t>=? AND t<=?", (sig["token"], lo, sig["t"]))
        wallets = sorted({r["wallet"] for r in prior} | {sig["wallet"]})
        n, need = len(wallets), self.s.cluster_min_wallets
        size = self.s.paper_size_usd
        if n >= need:
            self.rec.append("cluster", "inference", {
                "token": sig["token"], "symbol": tok.get("symbol"), "wallets": wallets,
                "window_s": self.s.cluster_window_s, "rule": f">= {need} passing wallets",
                "note": "co-buying observed; shared intent/independence is NOT proven"})
        elif not self.s.cluster_required:
            size *= self.s.solo_size_fraction
            self.rec.append("cluster", "hypothesis", {
                "token": sig["token"], "wallets": wallets,
                "note": f"solo entry at {self.s.solo_size_fraction:.0%} size; cluster not confirmed"})
        else:
            self.rec.append("cluster_pending", "hypothesis", {
                "token": sig["token"], "symbol": tok.get("symbol"), "wallets": wallets,
                "have": n, "need": need, "note": "possible cluster forming; no entry yet"})
            return self._mark_seen(sig, True, f"cluster pending {n}/{need}")

        cash = self.portfolio()["cash_usd"]
        if cash < size + self.s.paper_gas_usd:
            return self._skip(sig, "insufficient paper cash", passed=True, cash_usd=cash)

        try:
            fill = self._fill(sig, "buy", size, liq)
        except QuoteUnavailable as e:
            return self._skip(sig, "price unknown at fill -> fail closed", layer="unknown", passed=True, cause=e.reason)
        qty = size / fill["fill_price"]
        pid = self.store.x(
            "INSERT INTO positions(token,symbol,status,source_wallets,size_usd,qty,entry_t,entry_price,gas_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sig["token"], tok.get("symbol"), "open", json.dumps(wallets), size, qty,
             fill["t_fill"], fill["fill_price"], self.s.paper_gas_usd))
        self._log_trade(pid, sig, tok, fill, size, qty)
        return self._mark_seen(sig, True, f"PAPER BUY {tok.get('symbol')} ${size:.0f} @ {fill['fill_price']:.8g}")

    def _sell(self, sig: dict, tok: dict) -> str:
        pos = self._open_position(sig["token"])
        if not pos or sig["wallet"] not in json.loads(pos["source_wallets"]):
            return self._skip(sig, "no paper position sourced from this wallet", passed=True)
        try:
            liq = self.mkt.liquidity_at(sig["token"], sig["t"]) or self.s.liquidity_floor_usd
            mid_value = pos["qty"] * (self.mkt.price_at(sig["token"], sig["t"]) or 0)
            fill = self._fill(sig, "sell", mid_value, liq)
        except QuoteUnavailable as e:
            # Exits are never gated, but we cannot invent a price: keep the position open and say so.
            return self._skip(sig, "exit deferred: price unknown", layer="unknown", passed=True, cause=e.reason)
        proceeds = pos["qty"] * fill["fill_price"]
        gas = pos["gas_usd"] + self.s.paper_gas_usd
        pnl = proceeds - pos["size_usd"] - gas
        self.store.x("UPDATE positions SET status='closed', exit_t=?, exit_price=?, gas_usd=?, pnl_usd=? WHERE id=?",
                     (fill["t_fill"], fill["fill_price"], gas, pnl, pos["id"]))
        self._log_trade(pos["id"], sig, tok, fill, proceeds, pos["qty"])
        self.rec.append("paper_exit", "inference", {
            "position_id": pos["id"], "symbol": tok.get("symbol"), "trigger": f"mirror exit: {sig['wallet']} sold",
            "proceeds_usd": round(proceeds, 2), "gas_usd": gas, "pnl_usd": round(pnl, 2), "simulated": True})
        return self._mark_seen(sig, True, f"PAPER SELL {tok.get('symbol')} pnl ${pnl:.2f}")

    # ------------------------------------------------------------------ fills
    def _fill(self, sig: dict, side: str, size_usd: float, liq_usd: float) -> dict:
        """Lag haircut: fill at the mid LAG_MS after the signal (price has moved on).
        Impact haircut: IMPACT_PCT base + size/liquidity share, applied against us."""
        t_fill = sig["t"] + self.s.lag_ms / 1000
        tok = sig["token"]
        live = self.mkt.is_live(tok)
        mid = self.mkt.price_at(tok, t_fill)  # live: latest-block V2 mid (raises QuoteUnavailable)
        if not mid:
            raise QuoteUnavailable("no mid price")
        curve = self.mkt.impact_pct(tok, side, size_usd)
        if curve is None:
            impact, model = self.s.impact_pct + 100 * size_usd / max(liq_usd, 1), "fixture: IMPACT_PCT + size/liquidity"
        else:  # live: real constant-product cost (incl. 0.3% fee) + IMPACT_PCT as MEV/latency buffer
            impact, model = curve + self.s.impact_pct, "uniswap-v2 curve + IMPACT_PCT buffer"
        px = mid * (1 + impact / 100) if side == "buy" else mid * (1 - impact / 100)
        sig_px = sig.get("price_usd") or (None if live else self.mkt.price_at(tok, sig["t"]))
        return {"t_fill": int(t_fill), "signal_price": sig_px, "mid_at_fill": mid, "fill_price": px,
                "impact_pct": round(impact, 4), "impact_model": model, "lag_ms": self.s.lag_ms,
                "price_source": "uniswap-v2 latest block" if live else "fixture",
                "lag_drift_pct": round(100 * (mid - sig_px) / sig_px, 4) if sig_px else None}

    def _log_trade(self, pid: int, sig: dict, tok: dict, fill: dict, size: float, qty: float) -> None:
        self.store.x(
            "INSERT INTO trades(position_id,side,token,symbol,t_signal,t_fill,signal_price,mid_at_fill,fill_price,"
            "lag_ms,impact_pct,size_usd,qty,gas_usd,trigger_signal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, sig["side"], sig["token"], tok.get("symbol"), sig["t"], fill["t_fill"], fill["signal_price"],
             fill["mid_at_fill"], fill["fill_price"], fill["lag_ms"], fill["impact_pct"], size, qty,
             self.s.paper_gas_usd, sig["id"]))
        self.rec.append("paper_fill", "inference", {
            "position_id": pid, "side": sig["side"], "symbol": tok.get("symbol"), "token": sig["token"],
            "size_usd": round(size, 2), "qty": qty, **fill, "gas_usd": self.s.paper_gas_usd,
            "trigger": sig["id"], "simulated": True, "live": False})

    # ------------------------------------------------------------------ portfolio
    def portfolio(self) -> dict:
        t = self.sim_t
        pos = self.store.q("SELECT * FROM positions ORDER BY id DESC")
        realized = sum(p["pnl_usd"] or 0 for p in pos if p["status"] == "closed")
        open_cost = sum(p["size_usd"] + p["gas_usd"] for p in pos if p["status"] == "open")
        cash = self.s.paper_start_usd + realized - open_cost
        open_rows, unreal, mark_total = [], 0.0, 0.0
        for p in pos:
            p["source_wallets"] = json.loads(p["source_wallets"])
            if p["status"] != "open":
                continue
            try:
                mid, p["mark_source"] = self.mkt.price_at(p["token"], t) or 0, (
                    "uniswap-v2" if self.mkt.is_live(p["token"]) else "fixture")
            except QuoteUnavailable as e:  # stale mark beats a made-up one; label it
                mid = getattr(self.mkt, "last_price", lambda _: None)(p["token"]) or p["entry_price"]
                p["mark_source"], p["mark_layer"], p["mark_note"] = "stale", "unknown", e.reason
            mark = p["qty"] * mid * (1 - self.s.impact_pct / 100)
            p["mark_price"], p["mark_value_usd"] = mid, mark
            p["unrealized_usd"] = mark - p["size_usd"] - p["gas_usd"]
            unreal += p["unrealized_usd"]
            mark_total += mark
            open_rows.append(p)
        return {
            "mode": "PAPER", "live_enabled": False, "market": self.mkt.name, "sim_t": t, "sim_end_t": self.src.end_t,
            "start_usd": self.s.paper_start_usd, "cash_usd": round(cash, 2),
            "equity_usd": round(cash + mark_total, 2), "realized_pnl_usd": round(realized, 2),
            "unrealized_pnl_usd": round(unreal, 2),
            "open_positions": open_rows,
            "closed_positions": [p for p in pos if p["status"] == "closed"],
            "layer": "inference", "note": "simulated P&L (lag + impact + gas haircuts); not a track record",
        }
