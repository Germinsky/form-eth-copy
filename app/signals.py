"""Signal sources.

DemoSource  - offline fixture tape (SIGNAL_FIXTURE, default data/fixture_signals.json). Default.
rpc_probe   - read-only mainnet probe (eth_chainId must be 1, eth_blockNumber) via app.rpc.
LiveSource  - live watched-wallet Uniswap V2/V3 swaps decoded from mainnet receipts: app.live_signals
              (SIGNAL_SOURCE=live + DATA_MODE=rpc|hybrid + ETH_RPC_URL). RpcSource below is an alias.
No source here can sign or send anything.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from pathlib import Path

from .config import ROOT, Settings

FIXTURE = ROOT / "data" / "fixture_signals.json"


class DemoSource:
    name = "fixture"

    def __init__(self, path: Path | None = None):
        path = path or FIXTURE
        data = json.loads(path.read_text())
        self.meta = data.get("meta", {})
        self.tokens: dict[str, dict] = {t["address"].lower(): t for t in data["tokens"]}
        self.signals: list[dict] = sorted(data["signals"], key=lambda x: x["t"])
        for sig in self.signals:
            sig["token"] = sig["token"].lower()
            sig["wallet"] = sig["wallet"].lower()

    @property
    def end_t(self) -> int:
        last_price_t = max((p[0] for t in self.tokens.values() for p in t.get("price_path") or []), default=0)
        return max(self.signals[-1]["t"] if self.signals else 0, last_price_t)

    def signals_until(self, t: int) -> list[dict]:
        return [x for x in self.signals if x["t"] <= t]

    def token(self, address: str) -> dict | None:
        return self.tokens.get(address.lower())

    def price_at(self, token: str, t: float) -> float | None:
        """Linear interpolation over the token's price path (USD). Clamps at the ends."""
        tok = self.token(token)
        if not tok or not tok.get("price_path"):
            return None
        path = tok["price_path"]
        ts = [p[0] for p in path]
        i = bisect_right(ts, t)
        if i == 0:
            return path[0][1]
        if i >= len(path):
            return path[-1][1]
        (t0, p0), (t1, p1) = path[i - 1], path[i]
        return p0 + (p1 - p0) * (t - t0) / (t1 - t0)

    def liquidity_at(self, token: str, t: float) -> float | None:
        tok = self.token(token)
        return None if not tok else tok.get("liquidity_usd")


def rpc_probe(s: Settings, client=None) -> dict:
    """Read-only JSON-RPC probe. Confirms the endpoint is Ethereum mainnet (chainId 1)."""
    from .rpc import RpcClient, RpcError
    if not s.rpc_configured:
        return {"configured": False, "ok": False, "note": "ETH_RPC_URL not set (or placeholder); demo fixtures in use"}
    try:
        c = client or RpcClient(s.rpc_url, timeout=min(s.rpc_timeout_s, 5))
        chain_id = c.chain_id()
        block = c.get_block_number()
        ok = chain_id == s.expected_chain_id
        return {"configured": True, "ok": ok, "chain_id": chain_id, "block": block,
                "note": None if ok else "WRONG CHAIN — mainnet only; live reads will fail closed"}
    except RpcError as e:  # network/key errors — message never contains the URL
        return {"configured": True, "ok": False, "note": f"probe failed: {e}"}


def __getattr__(name):  # lazy alias kept for older imports: signals.RpcSource -> live_signals.LiveSource
    if name == "RpcSource":
        from .live_signals import LiveSource
        return LiveSource
    raise AttributeError(name)
