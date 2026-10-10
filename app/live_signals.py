"""Live wallet-swap signals from Ethereum mainnet (read-only). PAPER ONLY — nothing is signed or sent.

Pipeline (one `poll()`):
  1. Candidate txs for the tracked wallets in blocks (cursor, head - SIGNAL_CONFIRMATIONS]:
       logs   - eth_getLogs, topic-only filters (no contract address): ERC-20 Transfer FROM any tracked
                wallet + Transfer TO any tracked wallet (2 requests per LOGS_CHUNK_BLOCKS chunk, wallets
                OR-ed in the topic). Needs an Alchemy/Infura-class node; public nodes often refuse these.
       blocks - eth_getBlockByNumber(full txs) and keep txs whose `from` is a tracked wallet. Works on
                any node (no eth_getLogs) but costs 1 big request per block, so it is capped harder.
       auto   - logs; if eth_getLogs is refused, switch to blocks for the rest of the process.
  2. For each new tx hash (dedup: live_txs table, PRIMARY KEY tx hash) fetch the receipt (+ tx for
     msg.value) and decode it with `decode_tx` below.
  3. Signals -> live_txs(status='signal') + the_record `live_signal` (fact). Ambiguous Uniswap txs ->
     live_txs(status='rejected') + the_record `live_signal_rejected` (unknown). Txs with no Uniswap
     V2/V3 swap are `ignored` (stored for dedup, not written to the_record).
  4. The cursor only advances after every candidate in the range decoded or was classified; any
     RpcError aborts the poll WITHOUT advancing the cursor (retry next poll; dedup makes that safe).

Decoding (fail closed — anything not clearly ONE buy or ONE sell of ONE token is rejected):
  * receipt.status must be 1 and receipt.from must be a tracked wallet (the wallet initiated it).
  * >= 1 Swap log (V2 0xd78ad95f… / V3 0xc42079f9…) from a pool whose factory() is the canonical
    Uniswap V2 / V3 factory. Look-alike pools (Sushi, forks, fakes emitting the same event) don't count.
  * The wallet's net ERC-20 deltas from Transfer logs. Base assets = WETH, USDC, USDT, DAI (+ native ETH).
    buy  = exactly one non-base token with net delta > 0, none < 0, and the wallet paid a base asset
           (base Transfer out, or msg.value > 0).
    sell = exactly one non-base token with net delta < 0, none > 0, and base proceeds observed: a base
           Transfer in, OR a WETH Withdrawal in the tx (router-hop sell to native ETH: the router unwraps
           WETH and pays ETH by an internal call that leaves no log — proceeds are inferred).
  * The token must be token0/token1 of one of the verified Uniswap pools swapped in the tx.
  * Paid/received with two different base assets, token-for-token, multi-token batches -> rejected.
"""
from __future__ import annotations

import json
import threading
import time

from .config import Settings
from .record import Record
from .rpc import RpcClient, RpcError, dec_addr, enc_addr, words
from .store import Store
from .uniswap import QuoteUnavailable, Uniswap, SEL_TOKEN0, USDC, USDT, V2_FACTORY, V3_FACTORY, WETH

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH_DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WETH_WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SEL_FACTORY = "0xc45a0155"   # factory()
SEL_TOKEN1 = "0xd21220a7"    # token1()
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
ETH = "eth"                  # pseudo-address for native ETH legs
# base asset -> (unit, decimals). Anything else is a "token" a wallet can buy/sell.
BASE = {WETH: ("ETH", 18), ETH: ("ETH", 18), USDC: ("USD", 6), USDT: ("USD", 6), DAI: ("USD", 18)}
FACTORIES = {V2_FACTORY: "uniswap-v2", V3_FACTORY: "uniswap-v3"}


def _topic_addr(t: str) -> str:
    return "0x" + t[-40:].lower()


def _int(h) -> int:
    return int(h, 16) if isinstance(h, str) else int(h or 0)


class PoolBook:
    """Caches pool -> (venue, token0, token1) or None (not a canonical Uniswap V2/V3 pool). Immutable on-chain."""

    def __init__(self, rpc: RpcClient):
        self.rpc = rpc
        self._c: dict[str, tuple[str, str, str] | None] = {}
        self._lock = threading.Lock()

    def get(self, pool: str) -> tuple[str, str, str] | None:
        pool = pool.lower()
        with self._lock:
            if pool in self._c:
                return self._c[pool]
        out = self.rpc.eth_call(pool, SEL_FACTORY)          # RpcError propagates: transient, retry later
        venue = FACTORIES.get(dec_addr(words(out)[0])) if len(out) >= 66 else None
        val = None
        if venue:
            t0 = dec_addr(words(self.rpc.eth_call(pool, SEL_TOKEN0))[0])
            t1 = dec_addr(words(self.rpc.eth_call(pool, SEL_TOKEN1))[0])
            val = (venue, t0, t1)
        with self._lock:
            self._c[pool] = val
        return val


def decode_tx(receipt: dict, tx: dict | None, wallets: set[str], pools: PoolBook) -> dict:
    """-> {"status": "signal"|"rejected"|"ignored", "reason": str, "wallet":..., ...}. Pure apart from
    pool lookups. `tx` is only needed for msg.value (native-ETH buys)."""
    h = receipt["transactionHash"].lower()
    frm = (receipt.get("from") or "").lower()
    base = {"tx": h, "block": _int(receipt["blockNumber"]), "wallet": frm if frm in wallets else None}
    if _int(receipt.get("status", "0x1")) != 1:
        return {**base, "status": "ignored", "reason": "tx reverted"}
    logs = receipt.get("logs") or []
    venues, pool_tokens = set(), set()
    for lg in logs:
        if lg["topics"] and lg["topics"][0].lower() in (V2_SWAP, V3_SWAP):
            info = pools.get(lg["address"])
            if info:
                venues.add(info[0])
                pool_tokens.update(info[1:])
    if not venues:
        return {**base, "status": "ignored", "reason": "no canonical Uniswap V2/V3 Swap in tx"}
    if frm not in wallets:
        involved = sorted({_topic_addr(x) for lg in logs if lg["topics"] and lg["topics"][0].lower() == TRANSFER
                           for x in lg["topics"][1:3]} & wallets)
        return {**base, "wallet": involved[0] if involved else None, "status": "rejected", "reason": "tracked wallet moved tokens in a Uniswap tx it did not send "
                "(relayed / smart-account / third-party tx) - not attributable"}
    w = frm
    deltas: dict[str, int] = {}
    deposits = withdrawals = 0
    for lg in logs:
        t = [x.lower() for x in lg["topics"]]
        addr = lg["address"].lower()
        if t[0] == TRANSFER and len(t) == 3:
            data = lg.get("data") or "0x"
            if len(data) != 66:   # ERC-20 Transfer carries exactly one uint256; anything else is not trusted
                continue
            v = int(data, 16)
            if _topic_addr(t[1]) == w:
                deltas[addr] = deltas.get(addr, 0) - v
            if _topic_addr(t[2]) == w:
                deltas[addr] = deltas.get(addr, 0) + v
        elif addr == WETH and t[0] == WETH_DEPOSIT:
            deposits += int(lg["data"], 16)
        elif addr == WETH and t[0] == WETH_WITHDRAWAL:
            withdrawals += int(lg["data"], 16)
    value = _int((tx or {}).get("value", 0))
    toks = {a: d for a, d in deltas.items() if a not in BASE and d != 0}
    pos = [a for a, d in toks.items() if d > 0]
    neg = [a for a, d in toks.items() if d < 0]
    if not pos and not neg:
        return {**base, "status": "ignored", "reason": "no non-base token moved for wallet (base<->base swap or pass-through)"}
    if len(pos) + len(neg) != 1:
        return {**base, "status": "rejected", "reason": f"ambiguous: {len(pos)} token(s) in, {len(neg)} token(s) out "
                "(token-for-token / batch)", "tokens_in": pos, "tokens_out": neg}
    side, token = ("buy", pos[0]) if pos else ("sell", neg[0])
    if token not in pool_tokens:
        return {**base, "status": "rejected", "reason": "token not in any verified Uniswap pool swapped in this tx",
                "token": token, "side": side}
    base_d = {a: d for a, d in deltas.items() if a in BASE and d != 0}
    if side == "buy":
        legs = {a: -d for a, d in base_d.items() if d < 0}
        if value > 0:
            spent = deposits - withdrawals if deposits > withdrawals else value  # minus wrapped-then-refunded ETH
            legs[ETH] = legs.get(ETH, 0) + min(spent, value)
        method = "wallet base-asset outflow" if ETH not in legs else "msg.value (net of WETH refund)"
    else:
        legs = {a: d for a, d in base_d.items() if d > 0}
        method = "wallet base-asset inflow"
        if not legs and withdrawals > deposits:
            legs[ETH] = withdrawals - deposits
            method = "router-hop: WETH Withdrawal in tx (native ETH paid by internal call, inferred)"
    if not legs:
        what = "payment" if side == "buy" else "proceeds"
        return {**base, "status": "rejected", "reason": f"no base-asset {what} observed for wallet "
                "(gift/claim, proceeds to another address, or non-base route)", "token": token, "side": side}
    units = {BASE[a][0] for a in legs}
    if len(legs) > 1 and not (legs.keys() <= {WETH, ETH}):
        return {**base, "status": "rejected", "reason": f"ambiguous: multiple base assets {sorted(legs)}",
                "token": token, "side": side}
    asset = next(iter(legs)) if len(legs) == 1 else ETH
    amt = sum(legs.values())
    return {**base, "status": "signal", "reason": method, "side": side, "token": token,
            "token_amount_raw": str(abs(toks[token])), "base_asset": asset, "base_unit": units.pop(),
            "base_amount_raw": str(amt), "venues": sorted(venues)}


class LiveSignalScanner:
    def __init__(self, s: Settings, rpc: RpcClient, store: Store, record: Record, uni: Uniswap | None = None):
        self.s, self.rpc, self.store, self.rec, self.uni = s, rpc, store, record, uni
        self.pools = PoolBook(rpc)
        self.scan_mode = s.signal_scan if s.signal_scan in ("logs", "blocks") else "logs"
        self.auto = s.signal_scan == "auto"
        self._ts: dict[int, int] = {}
        self._lock = threading.Lock()
        self.last: dict = {}

    # ------------------------------------------------------------ inputs
    def tracked_wallets(self) -> set[str]:
        """Watched wallets + any wallet that sourced an open paper position (its sells must still close us out)."""
        ws = {w["address"] for w in self.store.list_wallets() if w["status"] == "watched"}
        for p in self.store.q("SELECT source_wallets FROM positions WHERE status='open'"):
            ws.update(json.loads(p["source_wallets"]))
        return {w.lower() for w in ws}

    def _block_ts(self, n: int) -> int:
        if n not in self._ts:
            b = self.rpc.get_block(n, False)
            if not b:
                raise RpcError(f"block {n} not found")
            self._ts[n] = _int(b["timestamp"])
            if len(self._ts) > 5000:
                self._ts.pop(next(iter(self._ts)))
        return self._ts[n]

    # ------------------------------------------------------------ candidates
    def _candidates_logs(self, wallets: set[str], lo: int, hi: int) -> dict[str, dict | None]:
        topics_w = ["0x" + enc_addr(w) for w in sorted(wallets)]
        out: dict[str, dict | None] = {}
        chunk = max(1, self.s.logs_chunk_blocks)
        a = lo
        while a <= hi:
            b = min(hi, a + chunk - 1)
            for topics in ([TRANSFER, topics_w], [TRANSFER, None, topics_w]):
                for lg in self.rpc.get_logs(a, b, topics):
                    if len(lg.get("topics") or []) == 3 and not lg.get("removed"):
                        out.setdefault(lg["transactionHash"].lower(), None)
                        self._block_hint[lg["transactionHash"].lower()] = _int(lg["blockNumber"])
            a = b + 1
        return out

    def _candidates_blocks(self, wallets: set[str], lo: int, hi: int) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        for n in range(lo, hi + 1):
            b = self.rpc.get_block(n, True)
            if not b:
                raise RpcError(f"block {n} not found")
            self._ts[n] = _int(b["timestamp"])
            for tx in b.get("transactions") or []:
                if isinstance(tx, dict) and (tx.get("from") or "").lower() in wallets:
                    out[tx["hash"].lower()] = tx
                    self._block_hint[tx["hash"].lower()] = n
        return out

    # ------------------------------------------------------------ decode + enrich
    def _enrich(self, d: dict) -> dict:
        """Signal shape the paper engine consumes + optional size/price (inference, current ETH/USD)."""
        sig = {"id": f"tx-{d['tx']}", "t": self._block_ts(d["block"]), "wallet": d["wallet"], "token": d["token"],
               "side": d["side"], "tx": d["tx"], "block": d["block"], "venues": d["venues"], "source": "live",
               "base_asset": d["base_asset"], "base_amount_raw": d["base_amount_raw"],
               "token_amount_raw": d["token_amount_raw"], "size_method": d["reason"],
               "size_usd": None, "price_usd": None, "symbol": None}
        unit, dec = BASE[d["base_asset"]]
        base_amt = int(d["base_amount_raw"]) / 10 ** dec
        try:
            if unit == "USD":
                sig["size_usd"] = base_amt
            elif self.uni:
                sig["size_usd"] = base_amt * self.uni.eth_usd()["eth_usd"]
            if self.uni:
                tdec, sig["symbol"] = self.uni.token_meta(d["token"])
                qty = int(d["token_amount_raw"]) / 10 ** tdec
                if sig["size_usd"] and qty > 0:
                    sig["price_usd"] = sig["size_usd"] / qty
        except QuoteUnavailable:
            pass   # size/price are optional; the paper engine prices fills itself
        if sig["size_usd"] is not None:
            sig["size_usd"] = round(sig["size_usd"], 2)
        return sig

    def _known(self, tx_hash: str) -> bool:
        return bool(self.store.q("SELECT 1 FROM live_txs WHERE tx=?", (tx_hash,)))

    def scan_range(self, wallets: set[str], lo: int, hi: int, persist: bool = True, newest: bool = False) -> dict:
        """Candidates + decode for [lo, hi]. RpcError propagates (caller decides about the cursor).
        Block-scan mode caps the range at SIGNAL_BLOCKSCAN_MAX blocks: the oldest part for the cursor-driven
        poll (continuity), the newest part when newest=True (ad-hoc "last N blocks" scans)."""
        cap = self.s.signal_blockscan_max
        if self.scan_mode == "blocks" and hi - lo + 1 > cap:
            lo, hi = (hi - cap + 1, hi) if newest else (lo, lo + cap - 1)
        self._block_hint: dict[str, int] = {}
        if self.scan_mode == "logs":
            try:
                cands = self._candidates_logs(wallets, lo, hi)
            except RpcError as e:
                if not self.auto or not str(e).startswith("eth_getLogs"):
                    raise
                self.scan_mode = "blocks"
                if persist:
                    self.rec.append("live_scan_mode", "fact", {"from": "logs", "to": "blocks", "cause": str(e),
                                    "note": "node refused topic-only eth_getLogs; falling back to block scan"})
                if hi - lo + 1 > cap:
                    lo, hi = (hi - cap + 1, hi) if newest else (lo, lo + cap - 1)
                cands = self._candidates_blocks(wallets, lo, hi)
        else:
            cands = self._candidates_blocks(wallets, lo, hi)
        order = sorted(cands, key=lambda x: self._block_hint.get(x, 0))
        if len(order) > self.s.signal_max_receipts_per_poll:
            cut_block = self._block_hint[order[self.s.signal_max_receipts_per_poll]]
            if cut_block > lo:   # only process whole blocks; resume from cut_block next poll
                hi = cut_block - 1
                order = [x for x in order if self._block_hint[x] <= hi]
        res = {"signals": [], "rejected": [], "ignored": 0, "duplicates": 0}
        for h in order:
            if persist and self._known(h):
                res["duplicates"] += 1
                continue
            rc = self.rpc.get_receipt(h)
            if not rc:
                raise RpcError(f"receipt missing for {h}")
            tx = cands[h]
            if tx is None and (rc.get("from") or "").lower() in wallets:
                tx = self.rpc.get_tx(h)  # only needed for msg.value
            try:
                d = decode_tx(rc, tx, wallets, self.pools)
            except (ValueError, KeyError, IndexError, TypeError) as e:   # malformed data -> fail closed
                d = {"tx": h, "block": self._block_hint.get(h, 0), "wallet": None, "status": "rejected",
                     "reason": f"decode error: {type(e).__name__}"}
            sig = self._enrich(d) if d["status"] == "signal" else None
            if persist:
                self._persist(d, sig)
            if sig:
                res["signals"].append(sig)
            elif d["status"] == "rejected":
                res["rejected"].append(d)
            else:
                res["ignored"] += 1
        res.update(from_block=lo, to_block=hi, scan_mode=self.scan_mode, candidates=len(order))
        return res

    def _persist(self, d: dict, sig: dict | None) -> None:
        t = sig["t"] if sig else None
        new = self.store.x_count(
            "INSERT OR IGNORE INTO live_txs(tx,block,t,wallet,token,side,status,reason,signal_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (d["tx"], d["block"], t, d.get("wallet"), d.get("token"), d.get("side"), d["status"], d["reason"],
             json.dumps(sig) if sig else None))
        if not new:
            return
        if sig:
            self.rec.append("live_signal", "fact", {**sig, "note": "decoded from tx receipt logs; size_usd/price_usd "
                                                    "are inference (current ETH/USD)"})
        elif d["status"] == "rejected":
            self.rec.append("live_signal_rejected", "unknown", {**d, "note": "fail closed: not emitted as a signal"})

    # ------------------------------------------------------------ poll (persisted cursor)
    def poll(self) -> dict:
        with self._lock:
            try:
                out = self._poll()
            except RpcError as e:
                out = {"ok": False, "error": str(e), "scan_mode": self.scan_mode}
                self.rec.append("live_scan_error", "unknown", {**out, "note": "cursor NOT advanced; retry next poll"})
            self.last = {**out, "at": time.time()}
            return out

    def _poll(self) -> dict:
        self.rpc.ensure_mainnet()
        head = self.rpc.get_block_number() - self.s.signal_confirmations
        cursor = self.store.get_state("live_cursor")
        if cursor is None or cursor > head:
            cursor = max(0, head - self.s.signal_lookback_blocks)
        wallets = self.tracked_wallets()
        lo = cursor + 1
        if lo > head:
            return {"ok": True, "from_block": lo, "to_block": head, "new_signals": 0, "note": "no new blocks",
                    "wallets": len(wallets), "scan_mode": self.scan_mode}
        cap = self.s.signal_max_blocks_per_poll if self.scan_mode == "logs" else self.s.signal_blockscan_max
        hi = min(head, lo + cap - 1)
        if not wallets:
            res = {"from_block": lo, "to_block": hi, "signals": [], "rejected": [], "ignored": 0, "duplicates": 0,
                   "candidates": 0, "scan_mode": self.scan_mode, "note": "no tracked wallets"}
        else:
            res = self.scan_range(wallets, lo, hi)
        self.store.set_state("live_cursor", res["to_block"])
        self.store.set_state("live_head_t", self._block_ts(res["to_block"]))
        self.store.set_state("live_head_block", res["to_block"])
        return {"ok": True, "wallets": len(wallets), "new_signals": len(res["signals"]),
                "rejected": len(res["rejected"]), "ignored": res["ignored"], "duplicates": res["duplicates"],
                "candidates": res["candidates"], "from_block": res["from_block"], "to_block": res["to_block"],
                "chain_head_minus_conf": head, "behind_blocks": head - res["to_block"], "scan_mode": res["scan_mode"]}


class LiveSource:
    """Signal source for the paper engine backed by live_txs. Same interface as DemoSource."""
    name = "live"
    realtime = True

    def __init__(self, scanner: LiveSignalScanner, store: Store):
        self.scanner, self.store = scanner, store
        self.meta = {"_comment": "live mainnet wallet swaps (decoded receipts)"}

    def poll(self) -> dict:
        return self.scanner.poll()

    @property
    def end_t(self) -> int:
        return int(self.store.get_state("live_head_t", 0) or 0)

    def signals_until(self, t: int) -> list[dict]:
        rows = self.store.q("SELECT signal_json FROM live_txs WHERE status='signal' AND t<=? ORDER BY t, block, tx", (t,))
        return [json.loads(r["signal_json"]) for r in rows]

    def token(self, address: str) -> dict | None:
        rows = self.store.q("SELECT signal_json FROM live_txs WHERE status='signal' AND token=? LIMIT 1",
                            (address.lower(),))
        if not rows:
            return None
        return {"address": address.lower(), "symbol": json.loads(rows[0]["signal_json"]).get("symbol")}

    def price_at(self, token: str, t: float) -> float | None:
        return None

    def liquidity_at(self, token: str, t: float) -> float | None:
        return None


def _cli() -> None:  # pragma: no cover - manual smoke: python -m app.live_signals <wallet> [blocks]
    import sys
    from .config import settings
    from .record import Record as _R
    from .store import Store as _S
    import tempfile, pathlib
    if not settings.rpc_configured:
        sys.exit("ETH_RPC_URL not set")
    wallet = sys.argv[1].lower()
    blocks = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    d = pathlib.Path(tempfile.mkdtemp())
    rpc = RpcClient(settings.rpc_url, timeout=settings.rpc_timeout_s, max_rps=settings.rpc_max_rps)
    uni = Uniswap(rpc, cache_s=settings.quote_cache_s, stable_max_dev_pct=settings.stable_max_dev_pct)
    sc = LiveSignalScanner(settings, rpc, _S(d / "x.db"), _R(d / "x.jsonl"), uni)
    head = rpc.get_block_number() - settings.signal_confirmations
    res = sc.scan_range({wallet}, head - blocks + 1, head, persist=False, newest=True)
    res["rpc_calls"] = rpc.calls
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    _cli()
