"""Offline fake mainnet for live-signal tests: receipts, txs, blocks and topic-filtered eth_getLogs on top of
tests/fakechain.FakeChain (which answers the Uniswap eth_calls)."""
from app import live_signals as L
from app import uniswap as U
from app.rpc import enc_addr, enc_uint

from fakechain import GOOD, PAIRS, THIN, FakeChain

HEAD = 20_000_000
T0 = 1_700_000_000
ROUTER = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"   # Universal Router (any non-wallet address works)
V2_GOOD = PAIRS[GOOD]
V2_THIN = PAIRS[THIN]
V3_GOOD = "0x00000000000000000000000000000000000000ca"
SUSHI_LIKE = "0x00000000000000000000000000000000000000cf"   # emits a V2 Swap, factory is not Uniswap
POOLS = {V2_GOOD: (U.V2_FACTORY, GOOD, U.WETH), V2_THIN: (U.V2_FACTORY, THIN, U.WETH),
         V3_GOOD: (U.V3_FACTORY, GOOD, U.WETH),
         SUSHI_LIKE: ("0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac", GOOD, U.WETH)}


def _t(a: str) -> str:
    return "0x" + enc_addr(a)


def transfer(token, frm, to, amt):
    return {"address": token, "topics": [L.TRANSFER, _t(frm), _t(to)], "data": "0x" + enc_uint(amt)}


def v2swap(pool, to=ROUTER):
    return {"address": pool, "topics": [L.V2_SWAP, _t(ROUTER), _t(to)], "data": "0x" + enc_uint(0) * 4}


def v3swap(pool, to=ROUTER):
    return {"address": pool, "topics": [L.V3_SWAP, _t(ROUTER), _t(to)], "data": "0x" + enc_uint(0) * 7}


def deposit(amt, dst=ROUTER):
    return {"address": U.WETH, "topics": [L.WETH_DEPOSIT, _t(dst)], "data": "0x" + enc_uint(amt)}


def withdrawal(amt, src=ROUTER):
    return {"address": U.WETH, "topics": [L.WETH_WITHDRAWAL, _t(src)], "data": "0x" + enc_uint(amt)}


class FakeLiveChain(FakeChain):
    def __init__(self, refuse_logs=False, **kw):
        super().__init__(**kw)
        self.head = HEAD
        self.txs: dict[str, dict] = {}       # hash -> {"tx":..., "receipt":...}
        self.refuse_logs = refuse_logs
        self.methods: list[str] = []

    def add_tx(self, h, block, frm, logs, value=0, status=1, to=ROUTER):
        h = h.lower()
        lg = [{**x, "blockNumber": hex(block), "transactionHash": h, "logIndex": hex(i), "removed": False}
              for i, x in enumerate(logs)]
        self.txs[h] = {"tx": {"hash": h, "from": frm, "to": to, "value": hex(value), "blockNumber": hex(block)},
                       "receipt": {"transactionHash": h, "from": frm, "to": to, "blockNumber": hex(block),
                                   "status": hex(status), "logs": lg}}
        return h

    def __call__(self, payload):
        m, p = payload["method"], payload["params"]
        self.methods.append(m)
        if self.dead:
            raise ConnectionError("rpc down")
        if m == "eth_blockNumber":
            return self._ok(payload, hex(self.head))
        if m == "eth_getLogs":
            if self.refuse_logs:
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "error": {"code": -32701, "message": "Please specify an address in your request"}}
            return self._ok(payload, self._logs(p[0]))
        if m == "eth_getTransactionReceipt":
            return self._ok(payload, (self.txs.get(p[0].lower()) or {}).get("receipt"))
        if m == "eth_getTransactionByHash":
            return self._ok(payload, (self.txs.get(p[0].lower()) or {}).get("tx"))
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            b = {"number": p[0], "timestamp": hex(T0 + 12 * (n - HEAD))}
            if p[1]:
                b["transactions"] = [v["tx"] for v in self.txs.values() if int(v["tx"]["blockNumber"], 16) == n]
            return self._ok(payload, b)
        if m == "eth_call":
            to, data = p[0]["to"].lower(), p[0]["data"]
            if to in POOLS and data[:10] in (L.SEL_FACTORY, U.SEL_TOKEN0, L.SEL_TOKEN1):
                fac, t0, t1 = POOLS[to]
                return self._ok(payload, "0x" + enc_addr({L.SEL_FACTORY: fac, U.SEL_TOKEN0: t0}.get(data[:10], t1)))
        return super().__call__(payload)

    @staticmethod
    def _ok(payload, r):
        return {"jsonrpc": "2.0", "id": payload["id"], "result": r}

    def _logs(self, flt):
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        out = []
        for v in self.txs.values():
            for lg in v["receipt"]["logs"]:
                if not lo <= int(lg["blockNumber"], 16) <= hi:
                    continue
                if all(want is None or (lg["topics"][i] in (want if isinstance(want, list) else [want])
                                        if i < len(lg["topics"]) else False)
                       for i, want in enumerate(flt["topics"])):
                    out.append(lg)
        return out
