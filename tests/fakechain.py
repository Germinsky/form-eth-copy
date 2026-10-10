"""Offline fake mainnet for tests: answers the handful of eth_calls app.uniswap makes."""
from app.rpc import enc_addr, enc_uint
from app import uniswap as U

GOOD = "0x00000000000000000000000000000000000000aa"   # 1,000,000 GOOD vs 100 WETH -> $0.20, $400k liq
THIN = "0x00000000000000000000000000000000000000ab"   # 50,000 THIN vs 10 WETH   -> $0.40, $40k liq
NOPAIR = "0x00000000000000000000000000000000000000ac"
PAIRS = {GOOD: "0x00000000000000000000000000000000000000ba", THIN: "0x00000000000000000000000000000000000000bb"}


class FakeChain:
    def __init__(self, chain_id=1, eth_usd=2000.0, usdt_eth_usd=None, dead=False):
        self.chain_id, self.dead = chain_id, dead
        w = 1000 * 10 ** 18
        self.res = {  # pair -> (r0, r1, token0)
            U.V2_USDC_WETH: (int(1000 * eth_usd * 10 ** 6), w, U.USDC),
            U.V2_WETH_USDT: (w, int(1000 * (usdt_eth_usd or eth_usd) * 10 ** 6), U.WETH),
            PAIRS[GOOD]: (1_000_000 * 10 ** 18, 100 * 10 ** 18, GOOD),
            PAIRS[THIN]: (50_000 * 10 ** 18, 10 * 10 ** 18, THIN),
        }

    def __call__(self, payload: dict) -> dict:
        if self.dead:
            raise ConnectionError("rpc down")
        m, p = payload["method"], payload["params"]
        r = {"eth_chainId": hex(self.chain_id), "eth_blockNumber": hex(20_000_000)}.get(m)
        if r is None and m == "eth_call":
            r = self._call(p[0]["to"].lower(), p[0]["data"])
        if r is None and m == "eth_getLogs":
            r = []
        return {"jsonrpc": "2.0", "id": payload["id"], "result": r}

    def _call(self, to: str, data: str) -> str:
        sel, args = data[:10], data[10:]
        if to == U.V2_FACTORY and sel == U.SEL_GET_PAIR:
            a, b = ("0x" + args[24:64]), ("0x" + args[88:128])
            other = a if b == U.WETH else b
            return "0x" + enc_addr(PAIRS.get(other, "0x" + "0" * 40))
        if to == U.V3_FACTORY:
            return "0x" + "0" * 64
        if to in self.res:
            r0, r1, t0 = self.res[to]
            if sel == U.SEL_GET_RESERVES:
                return "0x" + enc_uint(r0) + enc_uint(r1) + enc_uint(0)
            if sel == U.SEL_TOKEN0:
                return "0x" + enc_addr(t0)
        if sel == U.SEL_DECIMALS and to in (GOOD, THIN):
            return "0x" + enc_uint(18)
        if sel == U.SEL_SYMBOL and to in (GOOD, THIN):
            name = b"GOOD" if to == GOOD else b"THIN"
            return "0x" + name.hex().ljust(64, "0")
        return "0x"
