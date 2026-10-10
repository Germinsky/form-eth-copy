"""LIVE path tests: caps + gates + arming + sign/broadcast bookkeeping — fully offline, NOTHING is broadcast.

The fake chain below answers read-only calls only; the RpcClient itself refuses eth_sendRawTransaction & co.
Signing/broadcasting is the browser wallet's job and is simulated here by planting the tx/receipt that a wallet
would have produced. Run: python -m pytest -q tests/test_live.py
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
_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", f"{_tmp}/l.db")
os.environ.setdefault("RECORD_PATH", f"{_tmp}/l.jsonl")
os.environ.setdefault("LIVE_ENABLED", "false")

from fakechain import GOOD, PAIRS, THIN, FakeChain  # noqa: E402
from keccak import selector  # noqa: E402

from app import live as L  # noqa: E402
from app.config import HARD_MAX_TRADE_USD, LiveTradingForbidden, assert_live_dark, settings  # noqa: E402
from app.record import Record  # noqa: E402
from app.rpc import RpcClient, enc_addr, enc_uint  # noqa: E402
from app.store import Store  # noqa: E402
from app.uniswap import WETH, Uniswap, v2_amount_out  # noqa: E402

A = "0xa11ce00000000000000000000000000000000001"
B = "0xb0b0000000000000000000000000000000000002"
ME = "0x1111111111111111111111111111111111111111"
CODE = "correct-horse-battery-staple"
GOOD_STATS = {"round_trips": 40, "win_rate": 0.7, "median_hold_min": 120, "buys_per_day": 3,
              "leftover_pct": 0.05, "top_coin_profit_share": 0.2, "source": "test"}
SEND_METHODS = {"eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign", "eth_signTransaction"}


class LiveChain(FakeChain):
    """FakeChain + router quotes, token balance/allowance, gas, and planted txs/receipts."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.methods, self.txs, self.receipts = [], {}, {}
        self.balances, self.allowances, self.revert = {}, {}, False

    def __call__(self, payload):
        m, p = payload["method"], payload["params"]
        self.methods.append(m)
        out = {"eth_estimateGas": hex(150_000), "eth_gasPrice": hex(20 * 10 ** 9)}.get(m)
        if m == "eth_getTransactionByHash":
            out = self.txs.get(p[0])
        elif m == "eth_getTransactionReceipt":
            out = self.receipts.get(p[0])
        elif m in ("eth_call", "eth_estimateGas") and self.revert and p[0].get("from"):
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": 3, "message": "execution reverted"}}
        if out is not None or m in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
            return {"jsonrpc": "2.0", "id": payload["id"], "result": out}
        return super().__call__(payload)

    def _reserves(self, token):
        r0, r1, t0 = self.res[PAIRS[token]]
        return (r0, r1) if t0 == token else (r1, r0)   # (token, weth)

    def _out(self, amount, path):
        rt, rw = self._reserves(path[1] if path[0] == WETH else path[0])
        return v2_amount_out(amount, rw, rt) if path[0] == WETH else v2_amount_out(amount, rt, rw)

    def _call(self, to, data):
        sel = data[:10]
        if to == L.V2_ROUTER and sel == L.SEL_GET_AMOUNTS_OUT:
            w = [int(data[10 + i:74 + i], 16) for i in range(0, len(data) - 10, 64)]
            path = ["0x" + hex(x)[2:].rjust(40, "0") for x in w[3:3 + w[2]]]
            return "0x" + enc_uint(0x20) + enc_uint(2) + enc_uint(w[0]) + enc_uint(self._out(w[0], path))
        if to == L.V2_ROUTER and sel in (L.SEL_SWAP_EXACT_ETH_FOR_TOKENS, L.SEL_SWAP_EXACT_TOKENS_FOR_ETH):
            d = L.decode_swap(data)
            amt = d.get("amount_in") or self._sim_value
            out = self._out(amt, d["path"])
            return "0x" + enc_uint(0x20) + enc_uint(2) + enc_uint(amt) + enc_uint(out)
        if sel == L.SEL_BALANCE_OF and to in (GOOD, THIN):
            return "0x" + enc_uint(self.balances.get(to, 0))
        if sel == L.SEL_ALLOWANCE and to in (GOOD, THIN):
            return "0x" + enc_uint(self.allowances.get(to, 0))
        if sel == L.SEL_APPROVE:
            return "0x" + enc_uint(1)
        return super()._call(to, data)

    _sim_value = 0


def _patch_value(chain):
    """eth_call params carry `value`; stash it so the router fake can quote ETH-in swaps."""
    orig = chain.__call__

    def call(payload):
        if payload["method"] == "eth_call" and payload["params"][0].get("value"):
            chain._sim_value = int(payload["params"][0]["value"], 16)
        return orig(payload)
    return call


def make(tmpdir=None, chain=None, **over):
    d = Path(tmpdir or tempfile.mkdtemp())
    s = replace(settings, live_enabled=True, live_armed=True, live_broadcast=False, arm_code=CODE, arm_code_sha256="",
                rpc_url="http://fake", db_path=d / "l.db", record_path=d / "r.jsonl", cluster_required=True,
                cluster_min_wallets=2, live_trade_usd=100, live_max_trade_usd=500, live_liquidity_floor_usd=250_000,
                liquidity_floor_usd=150_000, live_max_impact_pct=1.5, live_max_slippage_bps=100,
                live_daily_loss_limit_usd=150, live_allowed_senders=(), signal_max_age_s=900)
    s = replace(s, **over)
    assert_live_dark(s)
    chain = chain or LiveChain()
    rpc = RpcClient(transport=_patch_value(chain))
    store, rec = Store(s.db_path), Record(s.record_path)
    for a in (A, B):
        store.upsert_wallet(a, a[:6], GOOD_STATS, "independent operator", "watched")
    desk = L.LiveDesk(s, store, rec, rpc, Uniswap(rpc, cache_s=0))
    return desk, chain, store, rec


def paper_pos(store, token=GOOD, wallets=(A, B), entry_t=None):
    return store.x("INSERT INTO positions(token,symbol,status,source_wallets,size_usd,qty,entry_t,entry_price,gas_usd) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (token, "GOOD" if token == GOOD else "THIN", "open",
                                                 json.dumps(list(wallets)), 500, 2500,
                                                 int(entry_t or time.time() - 30), 0.2, 8))


def refused(fn, *a, status=None):
    try:
        fn(*a)
    except L.LiveRefused as e:
        assert status is None or e.status == status, (e.status, e.reason)
        return e
    raise AssertionError("expected LiveRefused")


def kinds(rec):
    return [r["kind"] for r in rec.tail(1000)]


# ---------------------------------------------------------------- selectors / encoding
def test_selectors_match_keccak():
    assert selector("swapExactETHForTokens(uint256,address[],address,uint256)") == L.SEL_SWAP_EXACT_ETH_FOR_TOKENS
    assert selector("swapExactTokensForETH(uint256,uint256,address[],address,uint256)") == L.SEL_SWAP_EXACT_TOKENS_FOR_ETH
    assert selector("getAmountsOut(uint256,address[])") == L.SEL_GET_AMOUNTS_OUT
    assert selector("approve(address,uint256)") == L.SEL_APPROVE
    assert selector("allowance(address,address)") == L.SEL_ALLOWANCE
    assert selector("balanceOf(address)") == L.SEL_BALANCE_OF
    d = L.decode_swap(L.enc_swap_eth_for_tokens(123, [WETH, GOOD], ME, 999))
    assert d == {"fn": "swapExactETHForTokens", "amount_out_min": 123, "to": ME, "deadline": 999, "path": [WETH, GOOD]}


# ---------------------------------------------------------------- config gates
def test_config_refuses_blind_or_unsafe_enable():
    base = replace(settings, rpc_url="http://fake")
    bad = [dict(live_enabled=True),                                         # no arm code: blind enable
           dict(live_enabled=True, arm_code="short"),                        # weak code
           dict(live_enabled=True, arm_code="changeme"),                     # placeholder
           dict(live_enabled=True, arm_code=CODE, live_max_trade_usd=501),   # above the locked cap
           dict(live_enabled=True, arm_code=CODE, live_trade_usd=600),
           dict(live_enabled=True, arm_code=CODE, live_max_slippage_bps=5000),
           dict(live_enabled=True, arm_code=CODE, live_liquidity_floor_usd=1000),  # looser than paper floor
           dict(live_enabled=True, arm_code=CODE, rpc_url=""),
           dict(live_armed=True), dict(live_broadcast=True)]                 # flags without LIVE_ENABLED
    for kw in bad:
        try:
            assert_live_dark(replace(base, **kw))
            raise AssertionError(f"should refuse {kw}")
        except LiveTradingForbidden:
            pass
    assert_live_dark(replace(base, live_enabled=True, arm_code=CODE))   # ok
    assert HARD_MAX_TRADE_USD == 500
    os.environ["PRIVATE_KEY"] = "0xdead"
    try:
        assert_live_dark(base)
        raise AssertionError("PRIVATE_KEY must be refused")
    except LiveTradingForbidden:
        pass
    finally:
        del os.environ["PRIVATE_KEY"]
    pub = replace(base, live_enabled=True, arm_code=CODE).public()
    assert "arm_code" not in pub and "arm_code_sha256" not in pub and CODE not in json.dumps(pub)


def test_defaults_are_off():
    assert settings.live_enabled is False and settings.live_armed is False and settings.live_broadcast is False
    assert settings.live_cap_usd <= 500


def test_live_off_refuses_everything():
    desk, *_ = make(live_enabled=False, live_armed=False)
    assert desk.status()["mode"] == "OFF"
    refused(desk.prepare, "buy:1", ME, status=403)
    refused(desk.arm, CODE, "ARM LIVE 500", status=403)


# ---------------------------------------------------------------- prepare (dry run)
def test_prepare_buy_dry_run_builds_capped_unsigned_tx_and_never_broadcasts():
    desk, chain, store, rec = make()
    pid = paper_pos(store)
    o = desk.prepare(f"buy:{pid}", ME)
    tx, q = o["tx"], o["quote"]
    assert tx["to"] == L.V2_ROUTER and tx["from"] == ME and tx["chainId"] == "0x1"
    usd = int(tx["value"], 16) / 1e18 * q["eth_usd"]
    assert 99 < usd <= 100 and usd <= desk.s.live_cap_usd
    d = L.decode_swap(tx["data"])
    assert d["path"] == [WETH, GOOD] and d["to"] == ME
    assert d["amount_out_min"] == int(q["expected_out_raw"]) * 9900 // 10000
    assert d["deadline"] - time.time() <= desk.s.live_deadline_s + 2
    assert o["sim"]["ok"] and o["sim"]["gas"] == 150_000
    assert o["status"] == "prepared" and not o["ready_to_sign"]
    assert "LIVE_BROADCAST=false (dry run)" in o["blocked_by"] and "not armed (ARM_CODE + phrase)" in o["blocked_by"]
    # dry run: even armed + user click -> refused, nothing released
    desk.arm(CODE, desk.arm_phrase())
    e = refused(desk.confirm, o["id"], ME, True, status=409)
    assert "LIVE_BROADCAST=false" in e.reason
    k = kinds(rec)
    assert {"live_prepare", "live_simulate", "live_armed", "live_refused"} <= set(k) and "live_sign_request" not in k
    assert not SEND_METHODS & set(chain.methods)
    layers = {r["kind"]: r["layer"] for r in rec.tail(100)}
    assert layers["live_prepare"] == "inference" and layers["live_simulate"] == "fact"


def test_rpc_client_refuses_send_methods():
    c = RpcClient(transport=LiveChain())
    for m in SEND_METHODS:
        try:
            c.call(m, [])
            raise AssertionError(m)
        except Exception as e:
            assert "refused" in str(e)


def test_cap_holds_at_max_size_and_impact_cap_refuses():
    desk, _, store, _ = make(live_trade_usd=500)
    o = desk.prepare(f"buy:{paper_pos(store)}", ME)
    assert int(o["tx"]["value"], 16) / 1e18 * o["quote"]["eth_usd"] <= 500
    desk2, _, store2, rec2 = make(live_trade_usd=500, live_max_impact_pct=0.4)
    e = refused(desk2.prepare, f"buy:{paper_pos(store2)}", ME)
    assert "impact" in e.reason and any(r["kind"] == "live_refused" for r in rec2.tail(10))


def test_liquidity_floor_and_unknown_pool_fail_closed():
    desk, _, store, _ = make()
    e = refused(desk.prepare, f"buy:{paper_pos(store, THIN)}", ME)
    assert "liquidity floor" in e.reason
    dead, _, store_d, _ = make(chain=LiveChain(dead=True))
    e = refused(dead.prepare, f"buy:{paper_pos(store_d)}", ME)
    assert e.layer == "unknown"


def test_only_watched_passing_wallets_and_cluster_rule():
    desk, _, store, _ = make()
    store.set_wallet_status(B, "parked")
    e = refused(desk.prepare, f"buy:{paper_pos(store)}", ME)
    assert e.reason == "needs >= 2 watched+PASS source wallets, have 1"
    store.upsert_wallet(B, "b", {**GOOD_STATS, "win_rate": 0.1}, "x", "watched")   # watched but now FAILS
    refused(desk.prepare, f"buy:{paper_pos(store)}", ME)
    solo, _, store_s, _ = make(cluster_required=False)
    store_s.set_wallet_status(B, "parked")
    assert solo.prepare(f"buy:{paper_pos(store_s)}", ME)["status"] == "prepared"
    store_s.set_wallet_status(A, "parked")
    refused(solo.prepare, f"buy:{paper_pos(store_s)}", ME)   # zero passing wallets


def test_freshness_blocks_only_when_broadcasting():
    desk, _, store, _ = make()
    assert desk.prepare(f"buy:{paper_pos(store, entry_t=5400)}", ME)["status"] == "prepared"  # fixture: dry run ok
    bc, _, store_b, _ = make(live_broadcast=True)
    refused(bc.prepare, f"buy:{paper_pos(store_b, entry_t=5400)}", ME)
    refused(bc.prepare, f"buy:{paper_pos(store_b, entry_t=time.time() - 5000)}", ME)


def test_sender_allow_list_and_simulation_revert():
    desk, _, store, _ = make(live_allowed_senders=(ME,))
    refused(desk.prepare, f"buy:{paper_pos(store)}", "0x" + "2" * 40, status=403)
    chain = LiveChain()
    chain.revert = True
    d2, _, s2, _ = make(chain=chain, live_broadcast=True)
    o = d2.prepare(f"buy:{paper_pos(s2)}", ME)
    assert o["sim"]["ok"] is False and o["sim"]["layer"] == "unknown" and "simulation did not pass" in o["blocked_by"]
    d2.arm(CODE, d2.arm_phrase())
    refused(d2.confirm, o["id"], ME, True)


# ---------------------------------------------------------------- arming
def test_arming_dual_confirmation_lockout_and_ttl():
    t = [time.time()]
    desk, _, _, rec = make()
    desk.now = lambda: t[0]
    refused(desk.arm, "wrong-code-wrong-code", desk.arm_phrase(), status=403)
    refused(desk.arm, CODE, "ARM LIVE", status=403)          # right code, wrong phrase
    assert desk.arm(CODE, "ARM LIVE 500")["armed"]
    t[0] += desk.s.live_arm_ttl_s + 1
    assert not desk.status()["armed"]                         # expired
    assert desk.arm(CODE, desk.arm_phrase())["armed"] and not desk.disarm()["armed"]
    for _ in range(L.MAX_ARM_FAILURES):
        refused(desk.arm, "nope-nope-nope", desk.arm_phrase())
    refused(desk.arm, CODE, desk.arm_phrase(), status=423)    # locked even with the right code
    no_cfg, *_ = make(live_armed=False)
    refused(no_cfg.arm, CODE, no_cfg.arm_phrase(), status=403)
    sha, *_ = make(arm_code="", arm_code_sha256=L._hash_code(CODE))
    assert sha.arm(CODE, sha.arm_phrase())["armed"]
    assert any(r["kind"] == "live_arm_failed" for r in rec.tail(50))


# ---------------------------------------------------------------- kill switch
def test_daily_loss_kill_switch_and_manual_latch():
    desk, _, store, rec = make()
    today = L._day()
    store.x("INSERT INTO live_orders(id,created_at,day,kind,side,status,gas_usd,pnl_usd,ref) VALUES(?,?,?,?,?,?,?,?,?)",
            ("lo-x", int(time.time()), today, "swap", "sell", "confirmed", 10, -145, "sell:9"))
    k = desk.kill_state()
    assert k["tripped"] and "daily loss $155.00" in k["reason"]
    e = refused(desk.prepare, f"buy:{paper_pos(store)}", ME, status=423)
    assert "kill switch" in e.reason
    refused(desk.arm, CODE, desk.arm_phrase(), status=423)
    d2, _, s2, _ = make()
    d2.arm(CODE, d2.arm_phrase())
    st = d2.trip_kill("manual: test")
    assert st["kill_switch"]["latched"] and not st["armed"]
    refused(d2.reset_kill, "bad-code-bad-code", status=403)
    assert not d2.reset_kill(CODE)["kill_switch"]["tripped"]


# ---------------------------------------------------------------- sign -> broadcast -> confirm (wallet simulated)
def _planted(chain, tx, h, ok=True, logs=()):
    chain.txs[h] = {"hash": h, "from": tx["from"], "to": tx["to"], "value": tx["value"], "input": tx["data"], "nonce": "0x1"}
    chain.receipts[h] = {"status": "0x1" if ok else "0x0", "blockNumber": hex(20_000_001), "gasUsed": hex(120_000),
                         "effectiveGasPrice": hex(20 * 10 ** 9), "logs": list(logs)}


def test_full_flow_armed_broadcast_then_exit_needs_exact_approve():
    desk, chain, store, rec = make(live_broadcast=True)
    pid = paper_pos(store)
    o = desk.prepare(f"buy:{pid}", ME)
    refused(desk.submitted, o["id"], "0x" + "a" * 64)          # hash before the sign request -> refused
    refused(desk.confirm, o["id"], ME, True)                    # not armed
    desk.arm(CODE, desk.arm_phrase())
    assert desk.order_view(o["id"])["ready_to_sign"]
    refused(desk.confirm, o["id"], ME, False)                   # no UI click
    refused(desk.confirm, o["id"], "0x" + "3" * 40, True)       # different wallet
    r = desk.confirm(o["id"], ME, True)
    assert r["status"] == "sign_requested" and r["tx"] == o["tx"]
    refused(desk.prepare, f"buy:{pid}", ME)                     # in flight
    h = "0x" + "b" * 64
    got = int(o["quote"]["expected_out_raw"])
    _planted(chain, o["tx"], h, logs=[{"address": GOOD, "topics": [L.TRANSFER, "0x" + enc_addr(PAIRS[GOOD]),
                                                                   "0x" + enc_addr(ME)], "data": "0x" + enc_uint(got)}])
    v = desk.submitted(o["id"], h)
    assert v["status"] == "submitted" and v["verified"]
    v = desk.poll(o["id"])
    assert v["status"] == "confirmed" and v["gas_usd"] > 0
    lp = desk.positions()[0]
    assert lp["status"] == "open" and int(lp["qty_raw"]) == got and lp["owner"] == ME
    k = kinds(rec)
    for want in ("live_prepare", "live_simulate", "live_sign_request", "live_broadcast", "live_confirm", "live_fill"):
        assert want in k, want
    lay = {(r["kind"]): r["layer"] for r in rec.tail(200)}
    assert lay["live_broadcast"] == "fact" and lay["live_confirm"] == "fact" and lay["live_fill"] == "inference"
    # exit: wallet holds tokens but allowance 0 -> exact-amount approve prepared first
    chain.balances[GOOD] = got
    sell = next(c for c in desk.candidates() if c["side"] == "sell")
    ap = desk.prepare(sell["ref"], ME)
    assert ap["kind"] == "approve" and ap["tx"]["to"] == GOOD
    dec = L.decode_swap(ap["tx"]["data"])
    assert dec["spender"] == L.V2_ROUTER and 0 < dec["amount"] <= got and dec["amount"] < 2 ** 255
    chain.allowances[GOOD] = got
    sw = desk.prepare(sell["ref"], ME)
    assert sw["kind"] == "swap" and L.decode_swap(sw["tx"]["data"])["path"] == [GOOD, WETH]
    assert sw["usd_notional"] <= 500
    assert not SEND_METHODS & set(chain.methods)


def test_broadcast_mismatch_latches_kill_switch():
    desk, chain, store, rec = make(live_broadcast=True)
    o = desk.prepare(f"buy:{paper_pos(store)}", ME)
    desk.arm(CODE, desk.arm_phrase())
    desk.confirm(o["id"], ME, True)
    h = "0x" + "c" * 64
    evil = {**o["tx"], "value": hex(int(o["tx"]["value"], 16) * 10)}
    _planted(chain, evil, h)
    v = desk.submitted(o["id"], h)
    assert v["status"] == "mismatch"
    st = desk.status()
    assert st["kill_switch"]["latched"] and not st["armed"]
    assert any(r["kind"] == "live_broadcast" and r["content"].get("mismatch") == ["value"] for r in rec.tail(20))


def test_unverified_hash_stays_unknown_until_seen():
    desk, chain, store, rec = make(live_broadcast=True)
    o = desk.prepare(f"buy:{paper_pos(store)}", ME)
    desk.arm(CODE, desk.arm_phrase())
    desk.confirm(o["id"], ME, True)
    h = "0x" + "d" * 64
    v = desk.submitted(o["id"], h)
    assert v["status"] == "submitted" and not v["verified"]
    assert [r for r in rec.tail(10) if r["kind"] == "live_broadcast"][0]["layer"] == "unknown"
    assert desk.poll(o["id"])["status"] == "submitted"
    _planted(chain, o["tx"], h, ok=False)
    assert desk.poll(o["id"])["status"] == "failed"
    assert not desk.positions()


# ---------------------------------------------------------------- API guard (default app = LIVE OFF)
def test_api_live_routes_guarded_and_off_by_default():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        st = c.get("/api/live/status").json()
        assert st["mode"] == "OFF" and st["live_enabled"] is False and st["can_broadcast"] is False
        assert c.post("/api/live/arm", json={"code": "x", "phrase": "y"}).status_code == 403      # no header
        r = c.post("/api/live/arm", json={"code": "x", "phrase": "y"},
                   headers={"X-Form-Desk": "1", "Origin": "https://evil.example"})
        assert r.status_code == 403 and "cross-origin" in r.text
        r = c.post("/api/live/prepare", json={"ref": "buy:1", "sender": ME}, headers={"X-Form-Desk": "1"})
        assert r.status_code == 403 and "LIVE OFF" in r.text
        assert "arm_code" not in c.get("/api/config").json()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
