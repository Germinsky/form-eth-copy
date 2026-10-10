"""FastAPI app: API + single-page dashboard. PAPER ONLY."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, settings
from .gates import score_wallet
from .live_signals import LiveSignalScanner, LiveSource
from .market import FixtureMarket, LiveMarket
from .paper import PaperEngine
from .record import LAYERS, SESSION, Record
from .rpc import RpcClient, RpcError
from .signals import DemoSource, rpc_probe
from .store import Store
from .swaps import recent_swaps
from .uniswap import ADDRESSES, QuoteUnavailable, Uniswap

STATIC = Path(__file__).parent / "static"
SEED = ROOT / "data" / "seed_wallets.json"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

store = Store(settings.db_path)
record = Record(settings.record_path)
# Read-only chain access exists whenever ETH_RPC_URL is set (for /api/quote, /api/swaps, /api/signals/scan).
# The paper engine only *uses* it for liquidity/impact/prices when DATA_MODE is rpc or hybrid, and for
# signals only when additionally SIGNAL_SOURCE=live (settings.live_signals).
rpc = (RpcClient(settings.rpc_url, timeout=settings.rpc_timeout_s, max_rps=settings.rpc_max_rps)
       if settings.rpc_configured else None)
uni = Uniswap(rpc, cache_s=settings.quote_cache_s, stable_max_dev_pct=settings.stable_max_dev_pct) if rpc else None
scanner = LiveSignalScanner(settings, rpc, store, record, uni) if rpc else None
source = LiveSource(scanner, store) if (settings.live_signals and scanner) else DemoSource(settings.signal_fixture)
market = LiveMarket(source, uni, settings.data_mode) if (settings.live_reads and uni) else FixtureMarket(source)
engine = PaperEngine(settings, store, record, source, market)


def seed_wallets() -> int:
    if store.list_wallets():
        return 0
    data = json.loads(SEED.read_text())
    for w in data["wallets"]:
        store.upsert_wallet(w["address"], w["label"], w["stats"], w.get("independence_note"), "parked", demo=True)
        wallet = store.get_wallet(w["address"])
        _apply_gate(wallet, want_watch=True)
    return len(data["wallets"])


def _apply_gate(wallet: dict, want_watch: bool) -> dict:
    """Evaluate gates; only a PASS may be watched. Anything else is parked (fail closed)."""
    g = score_wallet(wallet, settings)
    status = "watched" if (want_watch and g["verdict"] == "PASS") else "parked"
    store.set_wallet_status(wallet["address"], status)
    record.append("gate_eval", "inference", {"address": wallet["address"], "verdict": g["verdict"],
                                              "reasons": g["reasons"], "status": status, "claims": g["claims"]})
    return g


LOOP_S = settings.signal_poll_seconds if engine.realtime else settings.auto_tick_seconds


async def _auto_loop():
    while True:
        await asyncio.sleep(LOOP_S)
        if not engine.done:
            try:   # RPC work must not block the event loop
                await asyncio.to_thread(engine.tick)
            except Exception as e:  # keep the loop alive; record and carry on (fail closed: no trade)
                record.append("loop_error", "unknown", {"error": f"{type(e).__name__}: {e}"[:300]})


@asynccontextmanager
async def lifespan(_: FastAPI):
    record.append("session_start", "fact", {"live_enabled": False, "data_mode": settings.data_mode,
                                            "effective_data_mode": settings.effective_data_mode,
                                            "chain_id": settings.expected_chain_id, "source": source.name,
                                            "signal_source": settings.signal_source,
                                            "market": market.name, "rpc_configured": settings.rpc_configured,
                                            "note": "paper only; no signer present in this build"})
    if settings.data_mode != "demo" and not settings.rpc_configured:
        record.append("data_mode_fallback", "unknown", {
            "requested": settings.data_mode, "effective": "demo",
            "note": "ETH_RPC_URL missing -> fixture liquidity/prices used; live pool data unknown"})
    if settings.signal_source == "live" and not settings.live_signals:
        record.append("signal_source_fallback", "unknown", {
            "requested": "live", "effective": "fixture", "data_mode": settings.data_mode,
            "rpc_configured": settings.rpc_configured,
            "note": "live signals need ETH_RPC_URL and DATA_MODE=rpc|hybrid; fixture tape in use"})
    n = seed_wallets()
    if n:
        record.append("seed", "fact", {"wallets": n, "demo": True, "note": "synthetic demo wallets"})
    task = asyncio.create_task(_auto_loop()) if LOOP_S > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="FORM ETH Copy Desk (PAPER ONLY)", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- models
class WalletIn(BaseModel):
    address: str
    label: str | None = None
    independence_note: str | None = None
    stats: dict = Field(default_factory=dict, description="round_trips, win_rate, median_hold_min, buys_per_day, "
                                                           "leftover_pct, top_coin_profit_share, source")
    watch: bool = False


class WalletPatch(BaseModel):
    label: str | None = None
    independence_note: str | None = None
    stats: dict | None = None
    status: str | None = Field(None, description="parked | watched (watched requires gate PASS)")


def _wallet_view(w: dict) -> dict:
    return {**w, "gate": score_wallet(w, settings)}


def _need(address: str) -> dict:
    w = store.get_wallet(address)
    if not w:
        raise HTTPException(404, "wallet not found")
    return w


# ---------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


_probe_cache: dict = {"t": 0.0, "v": None}


def _probe() -> dict:
    """rpc_probe, cached 30 s so a polling dashboard doesn't burn free-tier RPC quota."""
    import time
    if time.time() - _probe_cache["t"] > 30 or _probe_cache["v"] is None:
        _probe_cache.update(t=time.time(), v=rpc_probe(settings, rpc))
    return {**_probe_cache["v"], "calls": rpc.calls}


def _live_status() -> dict:
    return {"cursor_block": store.get_state("live_cursor"), "head_t": store.get_state("live_head_t"),
            "tracked_wallets": len(scanner.tracked_wallets()), "scan_mode": scanner.scan_mode,
            "poll_seconds": settings.signal_poll_seconds, "last_poll": scanner.last or None,
            "counts": {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) n FROM live_txs GROUP BY status")},
            "rpc_throttled_429": rpc.throttled}


@app.get("/api/health")
def health():
    return {"ok": True, "live_enabled": False, "mode": "PAPER", "chain": "ethereum-mainnet", "chain_id": 1,
            "data_mode": settings.data_mode, "effective_data_mode": settings.effective_data_mode,
            "market": market.name, "signal_source": source.name, "signal_source_requested": settings.signal_source,
            "session": SESSION, "sim_t": engine.sim_t, "sim_end_t": source.end_t, "tape_done": engine.done,
            "live_signals": _live_status() if engine.realtime else None,
            "rpc": _probe() if rpc else
                   {"configured": False, "ok": None, "note": "ETH_RPC_URL not set; RPC not probed"}}


@app.get("/api/config")
def config():
    return settings.public()


@app.get("/api/wallets")
def list_wallets():
    return [_wallet_view(w) for w in store.list_wallets()]


@app.post("/api/wallets", status_code=201)
def add_wallet(body: WalletIn):
    if not ADDR_RE.match(body.address):
        raise HTTPException(422, "address must be 0x + 40 hex chars")
    if store.get_wallet(body.address):
        raise HTTPException(409, "wallet already exists")
    stats = {"source": "manual-entry", **body.stats}
    store.upsert_wallet(body.address, body.label, stats, body.independence_note, "parked")
    record.append("wallet_add", "fact", {"address": body.address.lower(), "label": body.label, "stats": stats})
    _apply_gate(store.get_wallet(body.address), want_watch=body.watch)
    return _wallet_view(store.get_wallet(body.address))


@app.get("/api/wallets/{address}")
def get_wallet(address: str):
    return _wallet_view(_need(address))


@app.patch("/api/wallets/{address}")
def patch_wallet(address: str, body: WalletPatch):
    w = _need(address)
    if body.status not in (None, "parked", "watched"):
        raise HTTPException(422, "status must be parked or watched")
    stats = w["stats"] if body.stats is None else {**w["stats"], **body.stats}
    store.upsert_wallet(w["address"], body.label if body.label is not None else w["label"], stats,
                        body.independence_note if body.independence_note is not None else w["independence_note"],
                        w["status"], w["demo"])
    record.append("wallet_update", "fact", {"address": w["address"], "changes": body.model_dump(exclude_none=True)})
    want = (body.status or w["status"]) == "watched"
    g = _apply_gate(store.get_wallet(w["address"]), want_watch=want)
    if body.status == "watched" and g["verdict"] != "PASS":
        raise HTTPException(409, {"error": "gate FAIL: wallet stays parked", "reasons": g["reasons"]})
    return _wallet_view(store.get_wallet(w["address"]))


@app.delete("/api/wallets/{address}")
def delete_wallet(address: str):
    w = _need(address)
    store.delete_wallet(w["address"])
    record.append("wallet_delete", "fact", {"address": w["address"], "label": w["label"]})
    return {"deleted": w["address"]}


@app.get("/api/wallets/{address}/gate")
def wallet_gate(address: str):
    return score_wallet(_need(address), settings)


@app.get("/api/portfolio")
def portfolio():
    return engine.portfolio()


@app.get("/api/trades")
def trades(limit: int = Query(100, le=1000)):
    return store.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/records")
def records(limit: int = Query(100, le=2000), kind: str | None = None, layer: str | None = None):
    if layer and layer not in LAYERS:
        raise HTTPException(422, f"layer must be one of {LAYERS}")
    return record.tail(limit, kind, layer)


@app.post("/api/paper/tick")
def paper_tick():
    return engine.tick()


@app.post("/api/paper/run")
def paper_run_all():
    """Run ticks until the demo tape is exhausted (one full paper cycle)."""
    return engine.run_all()


@app.post("/api/paper/reset")
def paper_reset():
    """Clears paper positions/trades/sim clock. the_record is append-only and is NOT cleared."""
    store.reset_paper()
    record.append("paper_reset", "fact", {"note": "paper state cleared; the_record retained"})
    return {"reset": True, "sim_t": engine.sim_t}


# ---------------------------------------------------------------- chain reads (read-only smoke routes)
def _need_uni() -> Uniswap:
    if not uni:
        raise HTTPException(503, {"layer": "unknown", "reason": "ETH_RPC_URL not configured; see README"})
    return uni


@app.get("/api/quote/{token}")
def quote(token: str, size_usd: float = Query(None, gt=0, le=10_000_000), v3: bool = True):
    """Live Uniswap price/liquidity for a token vs WETH (V2 authoritative, V3 best-effort). Read-only."""
    if not ADDR_RE.match(token):
        raise HTTPException(422, "token must be 0x + 40 hex chars")
    u = _need_uni()
    size = size_usd or settings.paper_size_usd
    try:
        q = u.quote(token, size) if v3 else {"token": token.lower(), "v2": u.v2_snapshot(token).as_dict(size)}
    except QuoteUnavailable as e:
        raise HTTPException(e.status, {"layer": "unknown", "reason": e.reason, "token": token.lower(),
                                       "paper_entry": "would be refused (fail closed)"}) from None
    v2 = q["v2"]
    q["paper_gate"] = {"liquidity_floor_usd": settings.liquidity_floor_usd,
                       "passes_floor": v2["liquidity_usd"] >= settings.liquidity_floor_usd, "layer": "inference"}
    q["addresses"] = ADDRESSES
    q["note"] = "read-only eth_call at a pinned block; nothing signed or sent"
    return q


@app.get("/api/swaps/{wallet}")
def swaps(wallet: str, blocks: int = Query(1000, ge=1, le=20_000), limit: int = Query(20, ge=1, le=200)):
    """Last N Uniswap V2/V3 swaps where the wallet is the recipient (eth_getLogs). Read-only."""
    if not ADDR_RE.match(wallet):
        raise HTTPException(422, "wallet must be 0x + 40 hex chars")
    _need_uni()
    try:
        return recent_swaps(rpc, wallet, blocks=blocks, limit=limit, chunk=settings.logs_chunk_blocks)
    except RpcError as e:
        raise HTTPException(502, {"layer": "unknown", "reason": str(e)}) from None


# ---------------------------------------------------------------- live wallet-swap signals
@app.get("/api/signals/live")
def live_signals(limit: int = Query(100, ge=1, le=2000), status: str | None = None):
    """Decoded live txs (dedup table). status = signal | rejected | ignored."""
    if status and status not in ("signal", "rejected", "ignored"):
        raise HTTPException(422, "status must be signal | rejected | ignored")
    sql, args = "SELECT * FROM live_txs", ()
    if status:
        sql, args = sql + " WHERE status=?", (status,)
    rows = store.q(sql + " ORDER BY block DESC, tx LIMIT ?", args + (limit,))
    for r in rows:
        r["signal"] = json.loads(r.pop("signal_json")) if r.get("signal_json") else None
    return rows


@app.post("/api/signals/poll")
def live_poll():
    """Scan new blocks for watched-wallet swaps WITHOUT running the paper engine (persists + dedups)."""
    if not engine.realtime:
        raise HTTPException(409, {"layer": "unknown", "reason": "live signals not active (need SIGNAL_SOURCE=live, "
                                  "DATA_MODE=rpc|hybrid and ETH_RPC_URL)", "effective": settings.effective_signal_source})
    return scanner.poll()


@app.get("/api/signals/scan/{wallet}")
def live_scan_wallet(wallet: str, blocks: int = Query(300, ge=1, le=5000)):
    """Ad-hoc read-only scan of ANY wallet over the last N blocks with the live decoder. Not persisted, not fed
    to the engine, nothing written to the_record. Block-scan mode (public nodes) only covers the newest
    SIGNAL_BLOCKSCAN_MAX blocks of the window."""
    if not ADDR_RE.match(wallet):
        raise HTTPException(422, "wallet must be 0x + 40 hex chars")
    _need_uni()
    sc = LiveSignalScanner(settings, rpc, store, record, uni)
    sc.pools = scanner.pools
    try:
        head = rpc.get_block_number() - settings.signal_confirmations
        res = sc.scan_range({wallet.lower()}, head - blocks + 1, head, persist=False, newest=True)
    except RpcError as e:
        raise HTTPException(502, {"layer": "unknown", "reason": str(e)}) from None
    res["wallet"] = wallet.lower()
    res["note"] = "read-only ad-hoc scan; not persisted, not fed to the paper engine"
    return res
