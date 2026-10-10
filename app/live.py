"""LIVE execution path — browser-wallet signed, gated, capped. The server NEVER holds a key.

Pipeline (every step writes the_record with a FORM layer):
  paper-approved entry (open paper position) / paper exit
    -> prepare: gates (enabled, sender allow-list, kill switch, trades/day, watched+PASS source wallets, cluster,
       signal freshness) + live V2 pool read (liquidity floor, impact cap) + $500 cap
       -> UNSIGNED Uniswap V2 Router02 calldata with amountOutMin (slippage cap) + deadline     [live_prepare]
    -> simulate: eth_call + eth_estimateGas of that exact tx from the user's address (dry run)  [live_simulate]
    -> confirm (UI click): re-checks EVERYTHING + LIVE_ARMED (config) + runtime arm session (ARM_CODE + phrase,
       expires) + LIVE_BROADCAST. Only then is the tx released for signing                     [live_sign_request]
    -> the browser wallet (MetaMask / WalletConnect) signs + broadcasts with eth_sendTransaction
    -> submitted: server accepts the tx hash only for an order in sign_requested and verifies the on-chain tx
       (from / to / value / input must match what was prepared; mismatch latches the kill switch) [live_broadcast]
    -> poll receipt: status, gas, tokens / ETH actually received; daily P&L -> kill switch       [live_confirm]

Router: Uniswap V2 Router02 (simple, auditable calldata). Buys: swapExactETHForTokens(ETH -> token).
Exits: swapExactTokensForETH(token -> ETH), preceded when needed by an exact-amount approve(router, amount)
(never infinite). Exits are never gated by wallets/cluster/kill switch/liquidity floor (reducing risk is
always allowed) but still obey the $500 cap, impact cap and slippage cap, and still need arming.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timezone

from .config import HARD_MAX_TRADE_USD, Settings, assert_live_dark
from .gates import score_wallet
from .live_signals import TRANSFER, WETH_WITHDRAWAL
from .record import Record
from .rpc import RpcClient, RpcError, dec_addr, enc_addr, enc_uint, words
from .store import Store
from .uniswap import WETH, QuoteUnavailable, Uniswap

V2_ROUTER = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"   # Uniswap V2 Router02, Ethereum mainnet
SEL_SWAP_EXACT_ETH_FOR_TOKENS = "0x7ff36ab5"   # swapExactETHForTokens(uint256,address[],address,uint256)
SEL_SWAP_EXACT_TOKENS_FOR_ETH = "0x18cbafe5"   # swapExactTokensForETH(uint256,uint256,address[],address,uint256)
SEL_GET_AMOUNTS_OUT = "0xd06ca61f"             # getAmountsOut(uint256,address[])
SEL_APPROVE = "0x095ea7b3"                     # approve(address,uint256)
SEL_ALLOWANCE = "0xdd62ed3e"                   # allowance(address,address)
SEL_BALANCE_OF = "0x70a08231"                  # balanceOf(address)
CHAIN_ID_HEX = "0x1"

ACTIVE = ("sign_requested", "submitted")       # an order in flight blocks a new one for the same ref
COUNTED = ("sign_requested", "submitted", "confirmed", "failed", "mismatch")
MAX_ARM_FAILURES = 5


class LiveRefused(Exception):
    """A gate said no. status = HTTP status for the API; layer = FORM layer of the refusal."""

    def __init__(self, reason: str, status: int = 409, layer: str = "inference", checks: list | None = None):
        super().__init__(reason)
        self.reason, self.status, self.layer, self.checks = reason, status, layer, checks or []

    def detail(self) -> dict:
        return {"error": "live refused", "reason": self.reason, "layer": self.layer, "checks": self.checks}


# ---------------------------------------------------------------- ABI (static + one address[] tail)
def _path_tail(path: list[str]) -> str:
    return enc_uint(len(path)) + "".join(enc_addr(a) for a in path)


def enc_swap_eth_for_tokens(amount_out_min: int, path: list[str], to: str, deadline: int) -> str:
    return (SEL_SWAP_EXACT_ETH_FOR_TOKENS + enc_uint(amount_out_min) + enc_uint(0x80) + enc_addr(to)
            + enc_uint(deadline) + _path_tail(path))


def enc_swap_tokens_for_eth(amount_in: int, amount_out_min: int, path: list[str], to: str, deadline: int) -> str:
    return (SEL_SWAP_EXACT_TOKENS_FOR_ETH + enc_uint(amount_in) + enc_uint(amount_out_min) + enc_uint(0xa0)
            + enc_addr(to) + enc_uint(deadline) + _path_tail(path))


def enc_get_amounts_out(amount_in: int, path: list[str]) -> str:
    return SEL_GET_AMOUNTS_OUT + enc_uint(amount_in) + enc_uint(0x40) + _path_tail(path)


def enc_approve(spender: str, amount: int) -> str:
    return SEL_APPROVE + enc_addr(spender) + enc_uint(amount)


def dec_uint_array(hexdata: str) -> list[int]:
    w = words(hexdata)
    if len(w) < 2:
        raise RpcError("ABI: uint[] too short")
    off = w[0] // 32
    n = w[off]
    return w[off + 1: off + 1 + n]


def decode_swap(data: str) -> dict:
    """Decode the calldata this module produces (used by tests and by the on-chain verification)."""
    sel, w = data[:10], words("0x" + data[10:])
    if sel == SEL_SWAP_EXACT_ETH_FOR_TOKENS:
        n = w[4]
        return {"fn": "swapExactETHForTokens", "amount_out_min": w[0], "to": dec_addr(w[2]), "deadline": w[3],
                "path": [dec_addr(x) for x in w[5:5 + n]]}
    if sel == SEL_SWAP_EXACT_TOKENS_FOR_ETH:
        n = w[5]
        return {"fn": "swapExactTokensForETH", "amount_in": w[0], "amount_out_min": w[1], "to": dec_addr(w[3]),
                "deadline": w[4], "path": [dec_addr(x) for x in w[6:6 + n]]}
    if sel == SEL_APPROVE:
        return {"fn": "approve", "spender": dec_addr(w[0]), "amount": w[1]}
    raise ValueError("unknown selector")


def _day(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%d")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------------------------------------------------------------- desk
class LiveDesk:
    def __init__(self, s: Settings, store: Store, record: Record, rpc: RpcClient | None, uni: Uniswap | None,
                 clock=time.time):
        self.s, self.store, self.rec, self.rpc, self.uni, self.now = s, store, record, rpc, uni, clock
        self._lock = threading.RLock()
        self._armed_until = 0.0
        self._arm_failures = 0

    # ------------------------------------------------------------ live_state kv (survives paper reset)
    def _get(self, key: str, default=None):
        rows = self.store.q("SELECT value FROM live_state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def _set(self, key: str, value) -> None:
        self.store.x("INSERT INTO live_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    # ------------------------------------------------------------ arming
    @property
    def armed(self) -> bool:
        return (self.s.live_enabled and self.s.live_armed and self.now() < self._armed_until
                and not self.kill_state()["tripped"])

    def arm_phrase(self) -> str:
        return f"ARM LIVE {self.s.live_cap_usd:.0f}"

    def _code_ok(self, code: str) -> bool:
        want = self.s.arm_code_sha256 or (_hash_code(self.s.arm_code) if self.s.arm_code else "")
        return bool(want) and hmac.compare_digest(_hash_code(code or ""), want)

    def arm(self, code: str, phrase: str) -> dict:
        with self._lock:
            self._require_enabled()
            if not self.s.live_armed:
                raise LiveRefused("LIVE_ARMED=false in config: runtime arming is not permitted", 403)
            if self._arm_failures >= MAX_ARM_FAILURES:
                raise LiveRefused("arming locked after repeated wrong codes; restart the server", 423)
            if self.kill_state()["tripped"]:
                raise LiveRefused("kill switch tripped: " + self.kill_state()["reason"], 423)
            if not self._code_ok(code) or (phrase or "").strip() != self.arm_phrase():
                self._arm_failures += 1
                self.rec.append("live_arm_failed", "fact", {"failures": self._arm_failures,
                                                            "locked": self._arm_failures >= MAX_ARM_FAILURES})
                raise LiveRefused("arm code or confirmation phrase wrong", 403)
            self._arm_failures = 0
            self._armed_until = self.now() + self.s.live_arm_ttl_s
            self.rec.append("live_armed", "fact", {"until": int(self._armed_until), "ttl_s": self.s.live_arm_ttl_s,
                                                   "broadcast": self.s.live_broadcast, "cap_usd": self.s.live_cap_usd})
            return self.status()

    def disarm(self, why: str = "operator") -> dict:
        with self._lock:
            was = self._armed_until > self.now()
            self._armed_until = 0.0
            if was:
                self.rec.append("live_disarmed", "fact", {"why": why})
            return self.status()

    # ------------------------------------------------------------ kill switch + daily counters
    def daily(self, day: str | None = None) -> dict:
        day = day or _day(self.now())
        rows = self.store.q("SELECT kind, status, gas_usd, pnl_usd FROM live_orders WHERE day=?", (day,))
        gas = sum(r["gas_usd"] or 0 for r in rows)
        pnl = sum(r["pnl_usd"] or 0 for r in rows)
        trades = sum(1 for r in rows if r["kind"] == "swap" and r["status"] in COUNTED)
        net = pnl - gas
        return {"day": day, "realized_pnl_usd": round(pnl, 2), "gas_usd": round(gas, 2), "net_usd": round(net, 2),
                "loss_usd": round(max(0.0, -net), 2), "loss_limit_usd": self.s.live_daily_loss_limit_usd,
                "trades": trades, "max_trades": self.s.live_max_trades_per_day,
                "note": "UTC day; realized P&L + gas of confirmed live txs. Open-position drawdown NOT counted"}

    def kill_state(self) -> dict:
        latch = self._get("kill_latch")
        d = self.daily()
        if latch:
            return {"tripped": True, "reason": latch.get("reason", "latched"), "latched": True, "since": latch.get("t")}
        if d["loss_usd"] >= self.s.live_daily_loss_limit_usd:
            return {"tripped": True, "latched": False,
                    "reason": f"daily loss ${d['loss_usd']:.2f} >= limit ${self.s.live_daily_loss_limit_usd:.2f}"}
        return {"tripped": False, "latched": False, "reason": None}

    def trip_kill(self, reason: str) -> dict:
        with self._lock:
            self._set("kill_latch", {"reason": reason, "t": int(self.now())})
            self._armed_until = 0.0
            self.rec.append("live_kill_switch", "fact", {"tripped": True, "reason": reason, "latched": True})
            return self.status()

    def reset_kill(self, code: str) -> dict:
        with self._lock:
            self._require_enabled()
            if not self._code_ok(code):
                self._arm_failures += 1
                raise LiveRefused("arm code wrong", 403)
            self.store.x("DELETE FROM live_state WHERE key='kill_latch'")
            self.rec.append("live_kill_switch", "fact", {"tripped": False, "reset": True,
                                                         "note": "latch cleared; daily-loss trip still applies"})
            return self.status()

    def _maybe_trip(self) -> None:
        k = self.kill_state()
        if k["tripped"] and not k["latched"] and self._armed_until:
            self._armed_until = 0.0
            self.rec.append("live_kill_switch", "fact", {"tripped": True, "reason": k["reason"], "latched": False})

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        s = self.s
        armed = self.armed
        mode = ("OFF" if not s.live_enabled else "DISARMED" if not armed
                else "ARMED_BROADCAST" if s.live_broadcast else "ARMED_DRY_RUN")
        return {
            "mode": mode, "live_enabled": s.live_enabled, "live_armed_config": s.live_armed,
            "armed": armed, "armed_until": int(self._armed_until) if armed else None,
            "broadcast": s.live_broadcast, "dry_run": not s.live_broadcast,
            "can_broadcast": bool(armed and s.live_broadcast),
            "arm_code_configured": s.arm_code_configured, "arm_locked": self._arm_failures >= MAX_ARM_FAILURES,
            "arm_phrase": self.arm_phrase() if s.live_enabled else None,
            "rpc_configured": bool(self.uni), "custody": "browser wallet signs; server holds no keys",
            "router": V2_ROUTER, "chain_id": 1,
            "caps": {"trade_usd": min(s.live_trade_usd, s.live_cap_usd), "max_trade_usd": s.live_cap_usd,
                     "hard_max_trade_usd": HARD_MAX_TRADE_USD, "max_impact_pct": s.live_max_impact_pct,
                     "max_slippage_bps": s.live_max_slippage_bps,
                     "liquidity_floor_usd": max(s.live_liquidity_floor_usd, s.liquidity_floor_usd),
                     "daily_loss_limit_usd": s.live_daily_loss_limit_usd, "max_trades_per_day": s.live_max_trades_per_day,
                     "quote_ttl_s": s.live_quote_ttl_s, "deadline_s": s.live_deadline_s, "arm_ttl_s": s.live_arm_ttl_s,
                     "cluster_min_wallets": self._need_wallets(), "allowed_senders": list(s.live_allowed_senders)},
            "kill_switch": self.kill_state(), "daily": self.daily(),
        }

    # ------------------------------------------------------------ helpers
    def _require_enabled(self) -> None:
        assert_live_dark(self.s)
        if not self.s.live_enabled:
            raise LiveRefused("LIVE OFF (LIVE_ENABLED=false). Paper only.", 403)

    def _need_uni(self) -> Uniswap:
        if not self.uni or not self.rpc:
            raise LiveRefused("ETH_RPC_URL not configured", 503, "unknown")
        return self.uni

    def _need_wallets(self) -> int:
        if self.s.live_min_cluster_wallets > 0:
            return self.s.live_min_cluster_wallets
        return self.s.cluster_min_wallets if self.s.cluster_required else 1

    def _order(self, oid: str) -> dict:
        rows = self.store.q("SELECT * FROM live_orders WHERE id=?", (oid,))
        if not rows:
            raise LiveRefused("order not found", 404)
        o = rows[0]
        for k in ("tx_json", "quote_json", "checks_json", "sim_json", "receipt_json"):
            o[k[:-5]] = json.loads(o.pop(k)) if o.get(k) else None
        return o

    def _upd(self, oid: str, **cols) -> None:
        cols["updated_at"] = int(self.now())
        sets = ", ".join(f"{k}=?" for k in cols)
        self.store.x(f"UPDATE live_orders SET {sets} WHERE id=?", tuple(
            json.dumps(v) if k.endswith("_json") else v for k, v in cols.items()) + (oid,))

    @staticmethod
    def _check(checks: list, name: str, ok: bool, detail=None, layer: str = "inference", blocking: bool = True) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "layer": layer, "blocking": blocking})
        return ok or not blocking

    def _refuse(self, kind: str, ref: str, reason: str, checks: list, layer: str = "inference", status: int = 409,
                **extra):
        self.rec.append("live_refused", layer, {"stage": kind, "ref": ref, "reason": reason, "checks": checks, **extra})
        raise LiveRefused(reason, status, layer, checks)

    def _sender_checks(self, sender: str, checks: list, ref: str) -> str:
        sender = (sender or "").lower()
        if not (len(sender) == 42 and sender.startswith("0x") and all(c in "0123456789abcdef" for c in sender[2:])):
            self._refuse("prepare", ref, "sender must be the connected wallet address (0x + 40 hex)", checks, status=422)
        allow = self.s.live_allowed_senders
        if not self._check(checks, "sender allow-list", not allow or sender in allow,
                           "any" if not allow else f"{len(allow)} allowed"):
            self._refuse("prepare", ref, "sender not in LIVE_ALLOWED_SENDERS", checks, status=403)
        return sender

    def _expire_stale(self) -> None:
        """A sign request the browser never reported back: once the calldata deadline (+60 s) has passed the swap
        can no longer execute, so the ref is freed. (Approvals have no deadline; same timeout, recorded.)"""
        cutoff = int(self.now()) - (self.s.live_deadline_s + self.s.live_quote_ttl_s + 60)
        for r in self.store.q("SELECT id FROM live_orders WHERE status='sign_requested' AND updated_at < ?", (cutoff,)):
            self._upd(r["id"], status="expired")
            self.rec.append("live_expired", "unknown", {"order": r["id"], "note": "sign request never reported back; "
                            "if it was broadcast anyway, check the wallet's activity"})

    # ------------------------------------------------------------ candidates
    def candidates(self) -> list[dict]:
        """Paper-approved entries not yet copied live + open live positions (exit candidates)."""
        self._expire_stale()
        out = []
        live_paper = {r["paper_position_id"] for r in self.store.q("SELECT paper_position_id FROM live_positions")}
        busy = {r["ref"]: r["status"] for r in self.store.q(
            f"SELECT ref, status FROM live_orders WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE)}
        for p in self.store.q("SELECT * FROM positions WHERE status='open' ORDER BY id DESC"):
            if p["id"] in live_paper:
                continue
            ref = f"buy:{p['id']}"
            out.append({"ref": ref, "side": "buy", "token": p["token"], "symbol": p["symbol"],
                        "paper_position_id": p["id"], "paper_size_usd": p["size_usd"], "paper_entry_t": p["entry_t"],
                        "source_wallets": json.loads(p["source_wallets"]), "in_flight": busy.get(ref),
                        "why": "paper entry approved (gates + liquidity + cluster passed in paper)"})
        for lp in self.store.q("SELECT * FROM live_positions WHERE status='open' ORDER BY id DESC"):
            pp = self.store.q("SELECT status FROM positions WHERE id=?", (lp["paper_position_id"],))
            paper_closed = not pp or pp[0]["status"] == "closed"
            ref = f"sell:{lp['id']}"
            out.append({"ref": ref, "side": "sell", "token": lp["token"], "symbol": lp["symbol"],
                        "live_position_id": lp["id"], "owner": lp["owner"], "qty_raw": lp["qty_raw"],
                        "cost_usd": lp["cost_usd"], "in_flight": busy.get(ref),
                        "why": "paper mirror-exit fired" if paper_closed else "manual exit (paper still open)",
                        "suggested": paper_closed})
        return out

    # ------------------------------------------------------------ prepare
    def prepare(self, ref: str, sender: str) -> dict:
        with self._lock:
            self._require_enabled()
            checks: list[dict] = []
            sender = self._sender_checks(sender, checks, ref)
            self._expire_stale()
            side, _, rid = (ref or "").partition(":")
            if side not in ("buy", "sell") or not rid.isdigit():
                raise LiveRefused("ref must be buy:<paper_position_id> or sell:<live_position_id>", 422)
            if self.store.q(f"SELECT 1 FROM live_orders WHERE ref=? AND status IN ({','.join('?' * len(ACTIVE))})",
                            (ref, *ACTIVE)):
                self._refuse("prepare", ref, "an order for this candidate is already in flight", checks)
            self.store.x("UPDATE live_orders SET status='expired', updated_at=? WHERE ref=? AND status='prepared'",
                         (int(self.now()), ref))
            d = self.daily()
            if not self._check(checks, "trades today", d["trades"] < self.s.live_max_trades_per_day,
                               f"{d['trades']}/{self.s.live_max_trades_per_day}"):
                self._refuse("prepare", ref, "max live trades per day reached", checks)
            uni = self._need_uni()
            return self._prepare_buy(int(rid), ref, sender, checks, uni) if side == "buy" else \
                self._prepare_sell(int(rid), ref, sender, checks, uni)

    def _snapshot(self, token: str, ref: str, checks: list):
        try:
            snap = self.uni.v2_snapshot(token)
            ethusd = self.uni.eth_usd()["eth_usd"]
        except QuoteUnavailable as e:
            self._check(checks, "uniswap v2 pool read", False, e.reason, "unknown")
            self._refuse("prepare", ref, f"pool data unknown -> fail closed ({e.reason})", checks, "unknown")
        self._check(checks, "uniswap v2 pool read", True, {"pair": snap.pair, "block": snap.block}, "fact")
        return snap, ethusd

    def _amounts_out(self, amount_in: int, path: list[str], block: int) -> int:
        out = dec_uint_array(self.rpc.eth_call(V2_ROUTER, enc_get_amounts_out(amount_in, path), block))
        if len(out) != len(path) or out[-1] <= 0:
            raise RpcError("getAmountsOut returned nothing")
        return out[-1]

    def _prepare_buy(self, pid: int, ref: str, sender: str, checks: list, uni: Uniswap) -> dict:
        s = self.s
        rows = self.store.q("SELECT * FROM positions WHERE id=?", (pid,))
        if not self._check(checks, "paper approval", bool(rows) and rows[0]["status"] == "open",
                           "paper position open" if rows else "no such paper position"):
            self._refuse("prepare", ref, "no open paper-approved position for this ref", checks, status=404)
        p = rows[0]
        token = p["token"].lower()
        if not self._check(checks, "not already live", not self.store.q(
                "SELECT 1 FROM live_positions WHERE paper_position_id=?", (pid,))):
            self._refuse("prepare", ref, "this paper entry was already copied live", checks)
        k = self.kill_state()
        if not self._check(checks, "kill switch", not k["tripped"], k["reason"] or "clear", "fact"):
            self._refuse("prepare", ref, "kill switch tripped: " + k["reason"], checks, status=423)
        # watched + PASS *now* (a wallet may have been parked since the paper entry)
        srcs = json.loads(p["source_wallets"])
        passing = []
        for a in srcs:
            w = self.store.get_wallet(a)
            if w and w["status"] == "watched" and score_wallet(w, s)["verdict"] == "PASS":
                passing.append(a)
        need = self._need_wallets()
        self._check(checks, "source wallets watched + PASS", len(passing) >= 1, {"passing": passing, "sources": srcs})
        if not self._check(checks, "cluster rule", len(passing) >= need, f"{len(passing)}/{need} passing wallets"):
            self._refuse("prepare", ref, f"needs >= {need} watched+PASS source wallets, have {len(passing)}", checks)
        # freshness: a fixture-tape (sim clock) approval is only acceptable for a dry run
        et = p["entry_t"] or 0
        if et > 1e9:
            age = self.now() - et
            fresh_ok, detail, layer = age <= s.signal_max_age_s, f"age {age:.0f}s (max {s.signal_max_age_s}s)", "fact"
        else:
            fresh_ok, detail, layer = False, "paper approval came from the fixture tape (sim clock), not a live signal", "unknown"
        if not self._check(checks, "signal freshness", fresh_ok, detail, layer, blocking=s.live_broadcast):
            self._refuse("prepare", ref, "stale / non-live paper approval", checks, layer)

        snap, ethusd = self._snapshot(token, ref, checks)
        floor = max(s.live_liquidity_floor_usd, s.liquidity_floor_usd)
        if not self._check(checks, "liquidity floor", snap.liquidity_usd >= floor,
                           f"${snap.liquidity_usd:,.0f} vs floor ${floor:,.0f}"):
            self._refuse("prepare", ref, "below live liquidity floor", checks)
        size_usd = min(s.live_trade_usd, s.live_cap_usd)
        value_wei = int(size_usd / ethusd * 10 ** 18)
        notional = value_wei / 1e18 * ethusd
        if not self._check(checks, "per-trade cap", 0 < notional <= s.live_cap_usd + 1e-9,
                           f"${notional:.2f} <= ${s.live_cap_usd:.0f}"):
            self._refuse("prepare", ref, "trade exceeds $ cap", checks)
        impact = snap.impact_pct("buy", notional)
        if not self._check(checks, "price impact cap", impact <= s.live_max_impact_pct,
                           f"{impact:.3f}% vs cap {s.live_max_impact_pct}%"):
            self._refuse("prepare", ref, "price impact above cap", checks)
        path = [WETH, token]
        try:
            expected = self._amounts_out(value_wei, path, snap.block)
            q_layer = "fact"
        except RpcError as e:
            self._check(checks, "router quote", False, str(e), "unknown")
            self._refuse("prepare", ref, "router getAmountsOut failed -> fail closed", checks, "unknown")
        min_out = expected * (10_000 - s.live_max_slippage_bps) // 10_000
        self._check(checks, "slippage cap", min_out > 0, f"amountOutMin = quote - {s.live_max_slippage_bps} bps")
        deadline = int(self.now()) + s.live_deadline_s
        tx = {"from": sender, "to": V2_ROUTER, "value": hex(value_wei), "chainId": CHAIN_ID_HEX,
              "data": enc_swap_eth_for_tokens(min_out, path, sender, deadline)}
        quote = {"venue": "uniswap-v2", "router": V2_ROUTER, "pair": snap.pair, "block": snap.block,
                 "eth_usd": ethusd, "price_usd": snap.price_usd, "liquidity_usd": round(snap.liquidity_usd, 2),
                 "amount_in_wei": str(value_wei), "notional_usd": round(notional, 2),
                 "expected_out_raw": str(expected), "min_out_raw": str(min_out), "decimals": snap.decimals,
                 "impact_pct": round(impact, 4), "slippage_bps": s.live_max_slippage_bps, "deadline": deadline,
                 "quote_layer": q_layer}
        return self._save_prepared("swap", "buy", token, snap.symbol, ref, sender, notional, tx, quote, checks)

    def _prepare_sell(self, lpid: int, ref: str, sender: str, checks: list, uni: Uniswap) -> dict:
        s = self.s
        rows = self.store.q("SELECT * FROM live_positions WHERE id=? AND status='open'", (lpid,))
        if not self._check(checks, "open live position", bool(rows)):
            self._refuse("prepare", ref, "no open live position", checks, status=404)
        lp = rows[0]
        token = lp["token"]
        if not self._check(checks, "sender owns position", lp["owner"] == sender, lp["owner"]):
            self._refuse("prepare", ref, "connect the wallet that holds this position", checks, status=403)
        self._check(checks, "exit gating", True, "exits skip wallet/cluster/kill-switch/floor gates (risk-reducing)",
                    blocking=False)
        snap, ethusd = self._snapshot(token, ref, checks)
        try:
            bal = words(self.rpc.eth_call(token, SEL_BALANCE_OF + enc_addr(sender), snap.block))[0]
            allowance = words(self.rpc.eth_call(token, SEL_ALLOWANCE + enc_addr(sender) + enc_addr(V2_ROUTER),
                                                snap.block))[0]
        except (RpcError, IndexError) as e:
            self._check(checks, "token balance/allowance read", False, str(e), "unknown")
            self._refuse("prepare", ref, "balance/allowance unknown -> fail closed", checks, "unknown")
        amount = min(int(lp["qty_raw"]), bal)
        if not self._check(checks, "wallet balance", amount > 0, f"position {lp['qty_raw']} / wallet {bal}", "fact"):
            self._refuse("prepare", ref, "wallet holds none of this token", checks)
        px = snap.price_usd
        value_usd = amount / 10 ** snap.decimals * px
        partial = False
        if value_usd > s.live_cap_usd:
            amount = int(s.live_cap_usd * 0.999 / px * 10 ** snap.decimals)
            value_usd, partial = amount / 10 ** snap.decimals * px, True
        if not self._check(checks, "per-trade cap", 0 < value_usd <= s.live_cap_usd + 1e-9,
                           f"${value_usd:.2f} <= ${s.live_cap_usd:.0f}" + (" (partial exit)" if partial else "")):
            self._refuse("prepare", ref, "trade exceeds $ cap", checks)
        impact = snap.impact_pct("sell", value_usd)
        if not self._check(checks, "price impact cap", impact <= s.live_max_impact_pct,
                           f"{impact:.3f}% vs cap {s.live_max_impact_pct}%"):
            self._refuse("prepare", ref, "price impact above cap", checks)
        if allowance < amount:   # exact-amount approval first (never infinite)
            self._check(checks, "router allowance", False, f"{allowance} < {amount}: approve tx prepared first",
                        "fact", blocking=False)
            tx = {"from": sender, "to": token, "value": "0x0", "chainId": CHAIN_ID_HEX,
                  "data": enc_approve(V2_ROUTER, amount)}
            quote = {"router": V2_ROUTER, "block": snap.block, "eth_usd": ethusd, "approve_amount_raw": str(amount),
                     "notional_usd": round(value_usd, 2), "note": "exact-amount approval; re-prepare the exit after it confirms"}
            return self._save_prepared("approve", "sell", token, snap.symbol, ref, sender, 0.0, tx, quote, checks)
        path = [token, WETH]
        try:
            expected = self._amounts_out(amount, path, snap.block)
        except RpcError as e:
            self._check(checks, "router quote", False, str(e), "unknown")
            self._refuse("prepare", ref, "router getAmountsOut failed -> fail closed", checks, "unknown")
        min_out = expected * (10_000 - s.live_max_slippage_bps) // 10_000
        self._check(checks, "slippage cap", min_out > 0, f"amountOutMin = quote - {s.live_max_slippage_bps} bps")
        deadline = int(self.now()) + s.live_deadline_s
        tx = {"from": sender, "to": V2_ROUTER, "value": "0x0", "chainId": CHAIN_ID_HEX,
              "data": enc_swap_tokens_for_eth(amount, min_out, path, sender, deadline)}
        quote = {"venue": "uniswap-v2", "router": V2_ROUTER, "pair": snap.pair, "block": snap.block,
                 "eth_usd": ethusd, "price_usd": px, "amount_in_raw": str(amount), "notional_usd": round(value_usd, 2),
                 "expected_out_wei": str(expected), "min_out_wei": str(min_out), "impact_pct": round(impact, 4),
                 "slippage_bps": s.live_max_slippage_bps, "deadline": deadline, "partial": partial,
                 "decimals": snap.decimals}
        return self._save_prepared("swap", "sell", token, snap.symbol, ref, sender, value_usd, tx, quote, checks)

    def _simulate(self, tx: dict) -> dict:
        """Dry run of the exact unsigned tx from the user's address (eth_call + eth_estimateGas)."""
        call = {k: tx[k] for k in ("from", "to", "value", "data")}
        try:
            out = self.rpc.simulate(call)
            gas = self.rpc.estimate_gas(call)
            gp = self.rpc.gas_price()
            ethusd = self.uni.eth_usd()["eth_usd"]
            res = {"ok": True, "layer": "fact", "gas": gas, "gas_price_wei": gp,
                   "gas_usd_est": round(gas * gp / 1e18 * ethusd, 2), "return": out[:200]}
            if tx["data"].startswith((SEL_SWAP_EXACT_ETH_FOR_TOKENS, SEL_SWAP_EXACT_TOKENS_FOR_ETH)):
                res["amounts"] = [str(x) for x in dec_uint_array(out)]
            return res
        except (RpcError, QuoteUnavailable, ValueError, IndexError) as e:
            return {"ok": False, "layer": "unknown", "error": str(getattr(e, "reason", e))[:300],
                    "note": "simulation failed/reverted (insufficient ETH/allowance, honeypot, stale quote?) -> "
                            "signing refused"}

    def _save_prepared(self, kind, side, token, symbol, ref, sender, notional, tx, quote, checks) -> dict:
        oid = "lo-" + secrets.token_hex(6)
        now = int(self.now())
        sim = self._simulate(tx)
        self._check(checks, "simulation (eth_call + estimateGas)", sim["ok"], sim.get("error") or sim.get("gas"),
                    sim["layer"], blocking=False)
        self.store.x("INSERT INTO live_orders(id,created_at,day,kind,side,token,symbol,ref,sender,status,usd_notional,"
                     "tx_json,quote_json,checks_json,sim_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (oid, now, _day(now), kind, side, token, symbol, ref, sender, "prepared", notional,
                      json.dumps(tx), json.dumps(quote), json.dumps(checks), json.dumps(sim), now))
        self.rec.append("live_prepare", "inference", {
            "order": oid, "kind": kind, "side": side, "token": token, "symbol": symbol, "ref": ref, "sender": sender,
            "notional_usd": round(notional, 2), "tx": tx, "quote": quote, "checks": checks,
            "note": "UNSIGNED tx returned to the browser; server holds no key"})
        self.rec.append("live_simulate", sim["layer"], {"order": oid, **sim})
        return self.order_view(oid)

    def order_view(self, oid: str) -> dict:
        o = self._order(oid)
        st = self.status()
        o["ready_to_sign"] = bool(o["status"] == "prepared" and (o["sim"] or {}).get("ok") and st["can_broadcast"]
                                  and self.now() - o["created_at"] <= self.s.live_quote_ttl_s)
        o["expires_at"] = o["created_at"] + self.s.live_quote_ttl_s
        o["blocked_by"] = [] if o["ready_to_sign"] else self._blockers(o, st)
        return o

    def _blockers(self, o: dict, st: dict) -> list[str]:
        b = []
        if o["status"] != "prepared":
            b.append(f"order is {o['status']}")
        if not (o["sim"] or {}).get("ok"):
            b.append("simulation did not pass")
        if self.now() - o["created_at"] > self.s.live_quote_ttl_s:
            b.append("quote expired — prepare again")
        if not st["live_armed_config"]:
            b.append("LIVE_ARMED=false (config)")
        elif not st["armed"]:
            b.append("not armed (ARM_CODE + phrase)")
        if not st["broadcast"]:
            b.append("LIVE_BROADCAST=false (dry run)")
        if st["kill_switch"]["tripped"]:
            b.append("kill switch tripped")
        return b

    # ------------------------------------------------------------ sign request (UI confirm click)
    def confirm(self, oid: str, sender: str, user_confirmed: bool) -> dict:
        with self._lock:
            self._require_enabled()
            o = self._order(oid)
            checks: list[dict] = []
            st = self.status()
            fails = self._blockers(o, st)
            if not user_confirmed:
                fails.append("user confirmation missing")
            if (sender or "").lower() != o["sender"]:
                fails.append("connected wallet differs from the order's sender")
            if o["side"] == "buy" and o["kind"] == "swap":
                d = self.daily()
                if d["trades"] >= self.s.live_max_trades_per_day:
                    fails.append("max live trades per day reached")
            # independent re-check of the cap from the tx itself (value for buys, quote notional for sells)
            tx, q = o["tx"], o["quote"] or {}
            usd = int(tx["value"], 16) / 1e18 * q.get("eth_usd", 0) if o["side"] == "buy" else (o["usd_notional"] or 0)
            if usd > self.s.live_cap_usd + 1e-6:
                fails.append(f"notional ${usd:.2f} above cap")
            if tx["to"] not in (V2_ROUTER, o["token"]) or tx.get("chainId") != CHAIN_ID_HEX:
                fails.append("tx target/chain not allowed")
            for f in fails:
                self._check(checks, f, False)
            if fails:
                self._refuse("sign_request", o["ref"], "; ".join(fails), checks, order=oid)
            self._upd(oid, status="sign_requested")
            self.rec.append("live_sign_request", "fact", {
                "order": oid, "sender": o["sender"], "to": tx["to"], "value": tx["value"], "kind": o["kind"],
                "side": o["side"], "notional_usd": round(usd, 2), "user_confirmed": True,
                "note": "tx released to the browser wallet for the user's signature"})
            return {"order": oid, "tx": tx, "status": "sign_requested"}

    def rejected(self, oid: str, reason: str) -> dict:
        with self._lock:
            o = self._order(oid)
            if o["status"] not in ("sign_requested", "prepared"):
                raise LiveRefused(f"order is {o['status']}", 409)
            self._upd(oid, status="rejected")
            self.rec.append("live_sign_rejected", "fact", {"order": oid, "reason": (reason or "")[:200]})
            return self.order_view(oid)

    # ------------------------------------------------------------ broadcast report + verification
    def submitted(self, oid: str, tx_hash: str) -> dict:
        with self._lock:
            self._require_enabled()
            o = self._order(oid)
            h = (tx_hash or "").lower()
            if not (len(h) == 66 and h.startswith("0x") and all(c in "0123456789abcdef" for c in h[2:])):
                raise LiveRefused("tx_hash must be 0x + 64 hex", 422)
            if o["status"] != "sign_requested":
                self.rec.append("live_refused", "fact", {"stage": "broadcast", "order": oid, "tx_hash": h,
                                                         "reason": f"order is {o['status']}, not sign_requested"})
                raise LiveRefused(f"tx hash accepted only after a confirmed sign request (order is {o['status']})", 409)
            if self.store.q("SELECT 1 FROM live_orders WHERE tx_hash=?", (h,)):
                raise LiveRefused("tx hash already recorded", 409)
            self._upd(oid, status="submitted", tx_hash=h)
            self._verify(oid)
            return self.order_view(oid)

    def _verify(self, oid: str) -> None:
        o = self._order(oid)
        if o["verified"]:
            return
        try:
            tx = self.rpc.get_tx(o["tx_hash"]) if self.rpc else None
        except RpcError as e:
            tx = None
            err = str(e)
        else:
            err = None
        if not tx:
            if not self._get(f"bc:{oid}"):
                self._set(f"bc:{oid}", 1)
                self.rec.append("live_broadcast", "unknown", {"order": oid, "tx_hash": o["tx_hash"],
                                                              "note": "client-reported hash; not yet visible on chain",
                                                              "error": err})
            return
        want = o["tx"]
        got = {"from": (tx.get("from") or "").lower(), "to": (tx.get("to") or "").lower(),
               "value": int(tx.get("value") or "0x0", 16), "data": (tx.get("input") or tx.get("data") or "").lower()}
        exp = {"from": want["from"], "to": want["to"], "value": int(want["value"], 16), "data": want["data"].lower()}
        diff = [k for k in exp if exp[k] != got[k]]
        if diff:
            self._upd(oid, status="mismatch", verified=1)
            self.rec.append("live_broadcast", "fact", {"order": oid, "tx_hash": o["tx_hash"], "verified": False,
                                                       "mismatch": diff, "note": "on-chain tx differs from prepared tx"})
            self.trip_kill(f"broadcast mismatch on {oid}: {','.join(diff)}")
            return
        self._upd(oid, verified=1)
        self.rec.append("live_broadcast", "fact", {"order": oid, "tx_hash": o["tx_hash"], "verified": True,
                                                   "from": got["from"], "to": got["to"], "nonce": tx.get("nonce")})

    def poll(self, oid: str) -> dict:
        """Check verification + receipt for a submitted order. Read-only RPC."""
        with self._lock:
            o = self._order(oid)
            if o["status"] != "submitted":
                return self.order_view(oid)
            self._verify(oid)
            o = self._order(oid)
            if o["status"] != "submitted" or not o["verified"]:
                return self.order_view(oid)
            try:
                rc = self.rpc.get_receipt(o["tx_hash"])
            except RpcError:
                return self.order_view(oid)
            if not rc:
                return self.order_view(oid)
            self._settle(o, rc)
            return self.order_view(oid)

    def _settle(self, o: dict, rc: dict) -> None:
        ok = int(rc.get("status") or "0x0", 16) == 1
        try:
            ethusd = self.uni.eth_usd()["eth_usd"]
        except (QuoteUnavailable, AttributeError):
            ethusd = (o["quote"] or {}).get("eth_usd") or 0
        gas_wei = int(rc.get("gasUsed") or "0x0", 16) * int(rc.get("effectiveGasPrice") or "0x0", 16)
        gas_usd = gas_wei / 1e18 * ethusd
        sender, token = o["sender"], o["token"]
        now = int(self.now())
        logs = rc.get("logs") or []
        fill: dict = {"gas_usd": round(gas_usd, 4), "eth_usd": ethusd}
        pnl = 0.0
        if ok and o["kind"] == "swap" and o["side"] == "buy":
            got = sum(int(lg["data"], 16) for lg in logs
                      if lg.get("address", "").lower() == token and lg["topics"][0] == TRANSFER
                      and dec_addr(int(lg["topics"][2], 16)) == sender)
            cost = int(o["tx"]["value"], 16) / 1e18 * ethusd
            ref_pid = int(o["ref"].split(":")[1])
            self.store.x("INSERT INTO live_positions(token,symbol,decimals,owner,status,paper_position_id,qty_raw,"
                         "cost_usd,opened_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (token, o["symbol"], (o["quote"] or {}).get("decimals", 18), sender, "open", ref_pid,
                          str(got), cost, now))
            fill.update(tokens_received_raw=str(got), cost_usd=round(cost, 2))
        elif ok and o["kind"] == "swap" and o["side"] == "sell":
            eth_out = sum(int(lg["data"], 16) for lg in logs
                          if lg.get("address", "").lower() == WETH and lg["topics"][0] == WETH_WITHDRAWAL)
            sold = int((o["quote"] or {}).get("amount_in_raw", "0"))
            proceeds = eth_out / 1e18 * ethusd
            lpid = int(o["ref"].split(":")[1])
            lp = self.store.q("SELECT * FROM live_positions WHERE id=?", (lpid,))[0]
            qty = int(lp["qty_raw"])
            basis = lp["cost_usd"] * min(1.0, sold / qty) if qty else lp["cost_usd"]
            pnl = proceeds - basis
            left = max(0, qty - sold)
            self.store.x("UPDATE live_positions SET qty_raw=?, cost_usd=?, realized_usd=realized_usd+?, status=?, "
                         "closed_at=? WHERE id=?", (str(left), lp["cost_usd"] - basis, pnl,
                                                    "open" if left else "closed", None if left else now, lpid))
            fill.update(eth_out_wei=str(eth_out), proceeds_usd=round(proceeds, 2), basis_usd=round(basis, 2),
                        pnl_usd=round(pnl, 2), remaining_raw=str(left))
        self._upd(o["id"], status="confirmed" if ok else "failed", receipt_json={
            "status": rc.get("status"), "block": rc.get("blockNumber"), "gasUsed": rc.get("gasUsed"),
            "effectiveGasPrice": rc.get("effectiveGasPrice")}, gas_usd=gas_usd, pnl_usd=pnl, day=_day(now))
        self.rec.append("live_confirm", "fact", {"order": o["id"], "tx_hash": o["tx_hash"], "success": ok,
                                                 "block": rc.get("blockNumber"), "gas_used": rc.get("gasUsed")})
        self.rec.append("live_fill", "inference", {"order": o["id"], "kind": o["kind"], "side": o["side"],
                                                   "token": token, "symbol": o["symbol"], "success": ok, **fill,
                                                   "note": "USD values use current V2 ETH/USD"})
        self._maybe_trip()

    def orders(self, limit: int = 50) -> list[dict]:
        ids = [r["id"] for r in self.store.q("SELECT id FROM live_orders ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                              (limit,))]
        return [self._order(i) for i in ids]

    def positions(self) -> list[dict]:
        return self.store.q("SELECT * FROM live_positions ORDER BY id DESC")
