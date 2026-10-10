"""Uniswap/RPC tests.  python -m pytest -q tests/   (or: python tests/test_uniswap.py)

Offline tests use tests/fakechain.py. Live tests run ONLY when ETH_RPC_URL is set in the environment
(or .env); otherwise they are skipped. Nothing here signs or sends a transaction.
"""
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=f"{tmp}/u.db", RECORD_PATH=f"{tmp}/u.jsonl", LIVE_ENABLED="false")

from fakechain import GOOD, NOPAIR, THIN, FakeChain  # noqa: E402

from app.config import settings  # noqa: E402
from app.market import LiveMarket  # noqa: E402
from app.paper import PaperEngine  # noqa: E402
from app.record import Record  # noqa: E402
from app.rpc import RpcClient, RpcError  # noqa: E402
from app.signals import DemoSource  # noqa: E402
from app.store import Store  # noqa: E402
from app.uniswap import USDC, WETH, QuoteUnavailable, Uniswap, sqrt_price_to_price, v2_amount_out, v2_impact_pct  # noqa: E402

LIVE_URL = settings.rpc_url if settings.rpc_configured else ""


def uni(**kw) -> Uniswap:
    return Uniswap(RpcClient(transport=FakeChain(**kw)), cache_s=0)


def _raises(fn, exc=QuoteUnavailable):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


# ---------------------------------------------------------------- offline
def test_math():
    assert v2_amount_out(10 ** 18, 100 * 10 ** 18, 100 * 10 ** 18) == 987158034397061298
    small = v2_impact_pct("buy", 0.001, 10 ** 24, 10 ** 24, 18, 18)
    assert 0.29 < small < 0.31                       # tiny trade ~= the 0.3% fee
    assert v2_impact_pct("buy", 10, 100 * 10 ** 18, 100 * 10 ** 18, 18, 18) > 9  # 10% of pool -> big impact
    assert abs(sqrt_price_to_price(2 ** 96, 18, 18) - 1) < 1e-12


def test_read_only_client():
    c = RpcClient(transport=FakeChain())
    for m in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign"):
        _raises(lambda: c.call(m, []), RpcError)
    assert c.get_block_number() == 20_000_000


def test_v2_quotes_offline():
    u = uni()
    assert abs(u.eth_usd()["eth_usd"] - 2000) < 1e-6
    s = u.v2_snapshot(GOOD)
    assert s.symbol == "GOOD" and abs(s.price_eth - 1e-4) < 1e-12 and abs(s.price_usd - 0.2) < 1e-9
    assert abs(s.liquidity_usd - 400_000) < 1e-6
    assert 0.3 < s.impact_pct("buy", 500) < 0.6
    w = u.v2_snapshot(WETH)
    assert abs(w.price_usd - 2000) < 1e-6 and w.liquidity_usd == 4_000_000
    q = u.quote(GOOD, 500)
    assert q["v2"]["impact"]["buy_pct"] > 0 and q["v3"]["available"] is False


def test_fail_closed_offline():
    assert _raises(lambda: uni().v2_snapshot(NOPAIR)).status == 404
    assert "disagree" in _raises(lambda: uni(usdt_eth_usd=2100).v2_snapshot(GOOD)).reason  # 5% > 2%
    assert "chainId" in _raises(lambda: uni(chain_id=5).v2_snapshot(GOOD)).reason
    assert _raises(lambda: uni(dead=True).v2_snapshot(GOOD)).status == 502


def _engine(tape_signals, chain: FakeChain, mode="rpc"):
    d = tempfile.mkdtemp()
    tape = Path(d) / "tape.json"
    toks = [{"address": a, "symbol": s} for a, s in ((GOOD, "GOOD"), (THIN, "THIN"), (NOPAIR, "NOPAIR"))]
    tape.write_text(json.dumps({"meta": {}, "tokens": toks, "signals": tape_signals}))
    s = replace(settings, data_mode=mode, db_path=Path(d) / "e.db", record_path=Path(d) / "r.jsonl",
                rpc_url="http://fake", cluster_required=True, cluster_min_wallets=2)
    store, rec, src = Store(s.db_path), Record(s.record_path), DemoSource(tape)
    for i, a in enumerate(("0xa11ce00000000000000000000000000000000001", "0xb0b0000000000000000000000000000000000002")):
        stats = {"round_trips": 40, "win_rate": 0.7, "median_hold_min": 120, "buys_per_day": 3,
                 "leftover_pct": 0.05, "top_coin_profit_share": 0.2}
        store.upsert_wallet(a, f"w{i}", stats, "independent", "watched")
    mkt = LiveMarket(src, Uniswap(RpcClient(transport=chain), cache_s=0), mode)
    return PaperEngine(s, store, rec, src, mkt), rec


def test_engine_uses_live_pool_reads():
    A, B = "0xa11ce00000000000000000000000000000000001", "0xb0b0000000000000000000000000000000000002"
    sigs = [{"id": "g1", "t": 10, "wallet": A, "token": GOOD, "side": "buy"},
            {"id": "g2", "t": 20, "wallet": B, "token": GOOD, "side": "buy"},
            {"id": "t1", "t": 30, "wallet": A, "token": THIN, "side": "buy"},
            {"id": "n1", "t": 40, "wallet": A, "token": NOPAIR, "side": "buy"},
            {"id": "g3", "t": 50, "wallet": A, "token": GOOD, "side": "sell"}]
    eng, rec = _engine(sigs, FakeChain())
    eng.run_all()
    recs = rec.tail(500)
    fills = [r["content"] for r in recs if r["kind"] == "paper_fill"]
    assert len(fills) == 2 and fills[-1]["side"] == "buy"
    assert abs(fills[-1]["mid_at_fill"] - 0.2) < 1e-9 and fills[-1]["price_source"].startswith("uniswap-v2")
    skips = {r["content"]["signal"]: r for r in recs if r["kind"] == "skip"}
    assert skips["t1"]["content"]["reason"] == "below liquidity floor" and skips["t1"]["content"]["liquidity_usd"] == 40000
    assert skips["n1"]["layer"] == "unknown" and skips["n1"]["content"]["reason"] == "liquidity unknown -> fail closed"
    assert any(r["kind"] == "pool_read" and r["layer"] == "fact" for r in recs)


def test_engine_fails_closed_when_rpc_dead():
    A, B = "0xa11ce00000000000000000000000000000000001", "0xb0b0000000000000000000000000000000000002"
    sigs = [{"id": "g1", "t": 10, "wallet": A, "token": GOOD, "side": "buy"},
            {"id": "g2", "t": 20, "wallet": B, "token": GOOD, "side": "buy"}]
    eng, rec = _engine(sigs, FakeChain(dead=True))
    eng.run_all()
    skips = [r for r in rec.tail(100) if r["kind"] == "skip"]
    assert len(skips) == 2 and all(r["layer"] == "unknown" for r in skips)
    assert not eng.portfolio()["open_positions"]


def test_hybrid_keeps_fixture_tokens_offline():
    src = DemoSource()  # the original demo tape: all tokens have price paths
    m = LiveMarket(src, Uniswap(RpcClient(transport=FakeChain(dead=True))), "hybrid")
    demoa = "0xd3e0000000000000000000000000000000000a01"
    assert not m.is_live(demoa) and m.liquidity_at(demoa, 0) == 420000  # no RPC touched
    assert m.is_live(GOOD)


# ---------------------------------------------------------------- live (gated on ETH_RPC_URL)
def test_live_quote():
    if not LIVE_URL:
        print("SKIP test_live_quote: ETH_RPC_URL not set")
        try:
            import pytest
            pytest.skip("ETH_RPC_URL not set")
        except ImportError:
            return
    u = Uniswap(RpcClient(LIVE_URL, timeout=15))
    assert u.rpc.verify_mainnet() == 1
    for tok in (WETH, USDC):
        q = u.quote(tok, 500)
        assert q["v2"]["price_usd"] > 0 and q["v2"]["liquidity_usd"] > 1_000_000, q["v2"]
    assert 0.9 < u.v2_snapshot(USDC).price_usd < 1.1
    _raises(lambda: u.v2_snapshot(NOPAIR))


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
