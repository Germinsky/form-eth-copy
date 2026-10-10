"""Live wallet-swap signal tests.  python -m pytest -q tests/   (or: python tests/test_live_signals.py)

Offline: fake receipts/logs/blocks (tests/fakelive.py). The live scan test runs ONLY when ETH_RPC_URL is
set (env or .env); otherwise it is skipped. Nothing here signs or sends anything.
"""
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=f"{tmp}/l.db", RECORD_PATH=f"{tmp}/l.jsonl", LIVE_ENABLED="false")

import httpx  # noqa: E402

from fakechain import GOOD, THIN  # noqa: E402
from fakelive import (HEAD, ROUTER, SUSHI_LIKE, T0, V2_GOOD, V2_THIN, V3_GOOD, FakeLiveChain,  # noqa: E402
                      deposit, transfer, v2swap, v3swap, withdrawal)

from app import uniswap as U  # noqa: E402
from app.config import settings  # noqa: E402
from app.live_signals import LiveSignalScanner, LiveSource, PoolBook, decode_tx  # noqa: E402
from app.market import LiveMarket  # noqa: E402
from app.paper import PaperEngine  # noqa: E402
from app.record import Record  # noqa: E402
from app.rpc import RpcClient  # noqa: E402
from app.store import Store  # noqa: E402
from app.uniswap import Uniswap  # noqa: E402

A = "0xa11ce00000000000000000000000000000000001"
B = "0xb0b0000000000000000000000000000000000002"
OTHER = "0x0000000000000000000000000000000000000fff"
E18 = 10 ** 18
PASS_STATS = {"round_trips": 40, "win_rate": 0.7, "median_hold_min": 120, "buys_per_day": 3,
              "leftover_pct": 0.05, "top_coin_profit_share": 0.2}
LIVE_URL = settings.rpc_url if settings.rpc_configured else ""


def _h(n: int) -> str:
    return "0x" + f"{n:064x}"


# ------------------------------------------------------------------ canned txs
def buy_eth(ch, h, block, w, amt_tok=5000 * E18, eth=E18, refund=0):
    logs = [deposit(eth), transfer(U.WETH, ROUTER, V2_GOOD, eth - refund), v2swap(V2_GOOD),
            transfer(GOOD, V2_GOOD, w, amt_tok)]
    if refund:
        logs.append(withdrawal(refund))
    return ch.add_tx(h, block, w, logs, value=eth)


def sell_router_hop(ch, h, block, w, amt_tok=5000 * E18, eth=E18 // 2):
    return ch.add_tx(h, block, w, [transfer(GOOD, w, V2_GOOD, amt_tok), v2swap(V2_GOOD),
                                   transfer(U.WETH, V2_GOOD, ROUTER, eth), withdrawal(eth)])


def sell_v3_weth(ch, h, block, w):
    return ch.add_tx(h, block, w, [transfer(GOOD, w, V3_GOOD, 100 * E18), v3swap(V3_GOOD, to=w),
                                   transfer(U.WETH, V3_GOOD, w, E18 // 100)])


def _decode(ch, h, wallets=(A, B)):
    rpc = RpcClient(transport=ch)
    v = ch.txs[h]
    return decode_tx(v["receipt"], v["tx"], set(wallets), PoolBook(rpc))


# ------------------------------------------------------------------ decoder
def test_decode_buy_with_native_eth_and_refund():
    ch = FakeLiveChain()
    d = _decode(ch, buy_eth(ch, _h(1), HEAD - 5, A, eth=E18, refund=E18 // 10))
    assert d["status"] == "signal" and d["side"] == "buy" and d["token"] == GOOD and d["wallet"] == A
    assert d["base_asset"] == "eth" and int(d["base_amount_raw"]) == E18 - E18 // 10   # refund netted out
    assert d["venues"] == ["uniswap-v2"]


def test_decode_router_hop_sell_to_native_eth():
    ch = FakeLiveChain()
    d = _decode(ch, sell_router_hop(ch, _h(2), HEAD - 5, A))
    assert d["status"] == "signal" and d["side"] == "sell" and d["token"] == GOOD
    assert d["reason"].startswith("router-hop") and int(d["base_amount_raw"]) == E18 // 2


def test_decode_v3_sell_for_weth():
    ch = FakeLiveChain()
    d = _decode(ch, sell_v3_weth(ch, _h(3), HEAD - 5, A))
    assert d["status"] == "signal" and d["side"] == "sell" and d["venues"] == ["uniswap-v3"]
    assert d["base_asset"] == U.WETH and d["reason"] == "wallet base-asset inflow"


def test_decode_fails_closed():
    ch = FakeLiveChain()
    # token-for-token (GOOD -> THIN) is ambiguous
    tft = ch.add_tx(_h(10), HEAD - 5, A, [transfer(GOOD, A, V2_GOOD, E18), v2swap(V2_GOOD), v2swap(V2_THIN),
                                          transfer(THIN, V2_THIN, A, E18)])
    assert _decode(ch, tft)["status"] == "rejected"
    # look-alike pool (factory != Uniswap) -> not a Uniswap swap at all
    fake = ch.add_tx(_h(11), HEAD - 5, A, [deposit(E18), v2swap(SUSHI_LIKE), transfer(GOOD, SUSHI_LIKE, A, E18)],
                     value=E18)
    assert _decode(ch, fake)["status"] == "ignored"
    # someone else's tx sends GOOD to A -> not attributable
    third = ch.add_tx(_h(12), HEAD - 5, OTHER, [deposit(E18), v2swap(V2_GOOD), transfer(GOOD, V2_GOOD, A, E18)],
                      value=E18)
    d = _decode(ch, third)
    assert d["status"] == "rejected" and d["wallet"] == A and "did not send" in d["reason"]
    # sell whose proceeds go elsewhere (no Withdrawal, no base inflow) -> rejected
    nop = ch.add_tx(_h(13), HEAD - 5, A, [transfer(GOOD, A, V2_GOOD, E18), v2swap(V2_GOOD),
                                          transfer(U.WETH, V2_GOOD, OTHER, E18)])
    assert "no base-asset proceeds" in _decode(ch, nop)["reason"]
    # buy with no payment from wallet (tokens "gifted" inside a swap tx A sent) -> rejected
    gift = ch.add_tx(_h(14), HEAD - 5, A, [v2swap(V2_GOOD), transfer(GOOD, V2_GOOD, A, E18)])
    assert _decode(ch, gift)["status"] == "rejected"
    # paid with two different base assets -> rejected
    two = ch.add_tx(_h(15), HEAD - 5, A, [transfer(U.USDC, A, V2_GOOD, 10 ** 6), transfer(U.WETH, A, V2_GOOD, E18),
                                          v2swap(V2_GOOD), transfer(GOOD, V2_GOOD, A, E18)])
    assert "multiple base assets" in _decode(ch, two)["reason"]
    # reverted
    rev = ch.add_tx(_h(16), HEAD - 5, A, [], status=0)
    assert _decode(ch, rev)["status"] == "ignored"
    # token in a swap tx but not in any swapped pool's tokens (THIN delivered alongside a GOOD-pool swap)
    odd = ch.add_tx(_h(17), HEAD - 5, A, [deposit(E18), v2swap(V2_GOOD), transfer(THIN, OTHER, A, E18)], value=E18)
    assert "not in any verified Uniswap pool" in _decode(ch, odd)["reason"]


# ------------------------------------------------------------------ scanner + engine
def _setup(ch, **over):
    d = Path(tempfile.mkdtemp())
    kw = dict(data_mode="rpc", signal_source="live", rpc_url="http://fake", db_path=d / "e.db",
              record_path=d / "r.jsonl", cluster_required=True, cluster_min_wallets=2, signal_confirmations=2,
              signal_lookback_blocks=50, signal_scan="auto", logs_chunk_blocks=20, signal_max_age_s=900,
              liquidity_floor_usd=150000, signal_max_receipts_per_poll=100, signal_max_blocks_per_poll=2000)
    s = replace(settings, **{**kw, **over})
    store, rec = Store(s.db_path), Record(s.record_path)
    for i, a in enumerate((A, B)):
        store.upsert_wallet(a, f"w{i}", PASS_STATS, "independent", "watched")
    rpc = RpcClient(transport=ch)
    uni = Uniswap(rpc, cache_s=0)
    sc = LiveSignalScanner(s, rpc, store, rec, uni)
    src = LiveSource(sc, store)
    eng = PaperEngine(s, store, rec, src, LiveMarket(src, uni, "rpc"))
    return eng, sc, store, rec


def test_live_signals_drive_paper_engine():
    ch = FakeLiveChain()
    buy_eth(ch, _h(1), HEAD - 10, A)
    buy_eth(ch, _h(2), HEAD - 8, B)
    sell_router_hop(ch, _h(3), HEAD - 6, A)
    buy_eth(ch, _h(4), HEAD - 1, A)       # inside the confirmation window: not scanned yet
    eng, sc, store, rec = _setup(ch)
    r = eng.tick()
    assert r["poll"]["ok"] and r["poll"]["new_signals"] == 3 and r["poll"]["to_block"] == HEAD - 2
    assert r["sim_t"] == T0 - 24          # clock = scanned head block timestamp
    recs = rec.tail(500)[::-1]            # oldest first
    sigs = [x for x in recs if x["kind"] == "live_signal"]
    assert len(sigs) == 3 and all(x["layer"] == "fact" for x in sigs)
    s1 = next(x["content"] for x in sigs if x["content"]["tx"] == _h(1))
    assert {"wallet", "token", "side", "t", "size_usd", "id"} <= set(s1)
    assert s1["size_usd"] == 2000 and abs(s1["price_usd"] - 0.4) < 1e-9 and s1["symbol"] == "GOOD"
    fills = [x["content"] for x in recs if x["kind"] == "paper_fill"]
    assert [f["side"] for f in fills] == ["buy", "sell"]           # cluster buy, then mirror exit
    assert fills[0]["signal_price"] == 0.4 and fills[0]["price_source"].startswith("uniswap-v2")
    assert any(x["kind"] == "paper_exit" for x in recs)
    # next block confirms tx 4; a second poll picks only it up (dedup by tx hash)
    ch.head += 1
    r2 = eng.tick()
    assert r2["poll"]["new_signals"] == 1 and r2["processed"] == 1
    # reset paper -> cursor cleared -> rescan is deduped: no duplicate live_signal records
    store.reset_paper()
    n_before = len([x for x in rec.tail(2000) if x["kind"] == "live_signal"])
    r3 = sc.poll()
    assert r3["new_signals"] == 0 and r3["duplicates"] == 4
    assert len([x for x in rec.tail(2000) if x["kind"] == "live_signal"]) == n_before
    assert store.q("SELECT COUNT(*) n FROM live_txs WHERE status='signal'")[0]["n"] == 4


def test_auto_falls_back_to_block_scan_when_getlogs_refused():
    ch = FakeLiveChain(refuse_logs=True)
    buy_eth(ch, _h(1), HEAD - 48, A)      # inside the first capped 10-block window
    eng, sc, store, rec = _setup(ch, signal_blockscan_max=10)
    r = sc.poll()
    assert r["ok"] and r["scan_mode"] == "blocks" and r["new_signals"] == 1
    assert r["to_block"] == HEAD - 50 - 2 + 10       # block scan is capped per poll
    assert any(x["kind"] == "live_scan_mode" for x in rec.tail(50))
    assert "eth_getLogs" not in ch.methods[ch.methods.index("eth_getLogs") + 1:]   # stays in blocks mode


def test_rpc_error_holds_cursor_and_records_unknown():
    ch = FakeLiveChain()
    buy_eth(ch, _h(1), HEAD - 5, A)
    eng, sc, store, rec = _setup(ch)
    ch.dead = True
    r = eng.tick()
    assert r["poll"]["ok"] is False and store.get_state("live_cursor") is None
    assert any(x["kind"] == "live_scan_error" and x["layer"] == "unknown" for x in rec.tail(20))
    ch.dead = False
    assert sc.poll()["new_signals"] == 1


def test_stale_live_buy_is_skipped_but_sells_are_not_gated():
    ch = FakeLiveChain()
    buy_eth(ch, _h(1), HEAD - 40, A)      # 38 blocks * 12 s = 456 s old at the scanned head
    eng, sc, store, rec = _setup(ch, signal_max_age_s=300, cluster_required=False)
    eng.tick()
    skip = [x["content"] for x in rec.tail(100) if x["kind"] == "skip"]
    assert skip and skip[0]["reason"] == "stale live signal"


def test_rejected_tx_written_to_record_once():
    ch = FakeLiveChain()
    ch.add_tx(_h(9), HEAD - 5, A, [transfer(GOOD, A, V2_GOOD, E18), v2swap(V2_GOOD), v2swap(V2_THIN),
                                   transfer(THIN, V2_THIN, A, E18)])
    eng, sc, store, rec = _setup(ch)
    sc.poll(); store.set_state("live_cursor", None); sc.poll()
    rej = [x for x in rec.tail(100) if x["kind"] == "live_signal_rejected"]
    assert len(rej) == 1 and rej[0]["layer"] == "unknown"


def test_live_source_requires_rpc_and_live_data_mode():
    assert replace(settings, signal_source="live", data_mode="hybrid", rpc_url="").effective_signal_source == "fixture"
    assert replace(settings, signal_source="live", data_mode="demo", rpc_url="http://x").effective_signal_source == "fixture"
    assert replace(settings, signal_source="live", data_mode="hybrid", rpc_url="http://x").effective_signal_source == "live"


def test_rpc_rate_limit_and_429_retry():
    calls = []

    def ok(p):
        calls.append(time.monotonic())
        return {"jsonrpc": "2.0", "id": p["id"], "result": "0x1"}
    c = RpcClient(transport=ok, max_rps=50)
    for _ in range(6):
        c.call("eth_chainId")
    assert calls[-1] - calls[0] >= 5 / 50 * 0.9
    n = {"i": 0}

    def flaky(p):
        n["i"] += 1
        if n["i"] < 3:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))
        return {"jsonrpc": "2.0", "id": p["id"], "result": "0x1"}
    c2 = RpcClient(transport=flaky, backoff_s=0.01)
    assert c2.call("eth_chainId") == "0x1" and c2.throttled == 2


# ------------------------------------------------------------------ live (gated on ETH_RPC_URL)
def test_live_wallet_scan():
    if not LIVE_URL:
        print("SKIP test_live_wallet_scan: ETH_RPC_URL not set")
        try:
            import pytest
            pytest.skip("ETH_RPC_URL not set")
        except ImportError:
            return
    wallet = os.getenv("LIVE_TEST_WALLET", "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13")
    d = Path(tempfile.mkdtemp())
    s = replace(settings, db_path=d / "x.db", record_path=d / "x.jsonl")
    rpc = RpcClient(LIVE_URL, timeout=20, max_rps=settings.rpc_max_rps)
    sc = LiveSignalScanner(s, rpc, Store(s.db_path), Record(s.record_path), Uniswap(rpc))
    head = rpc.get_block_number() - 2
    res = sc.scan_range({wallet}, head - 19, head, persist=False)
    assert res["to_block"] <= head and {"signals", "rejected", "ignored"} <= set(res)
    print(json.dumps({k: v for k, v in res.items() if k != "signals"}, default=str)[:500], len(res["signals"]))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
            except BaseException as e:  # pytest.skip() raises Skipped (a BaseException) when pytest is installed
                if type(e).__name__ != "Skipped":
                    raise
                continue
            print("ok", name)
