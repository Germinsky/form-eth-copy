"""Minimal 'last N swaps' reader for a wallet via eth_getLogs. Read-only, PAPER ONLY.

Matches Uniswap V2 Swap(sender, amount0In, amount1In, amount0Out, amount1Out, to) and
V3 Swap(sender, recipient, amount0, amount1, sqrtPriceX96, liquidity, tick) where the indexed
recipient (topic2) == wallet. Limitation: swaps whose output goes to a router first (e.g. selling
for native ETH via the Universal Router, or multi-hop intermediate legs) have recipient=router and
are NOT seen. The engine-facing reader that does match Transfer logs, groups by tx hash and catches
router-hop sells is app/live_signals.py (SIGNAL_SOURCE=live); this module is the raw smoke route.
"""
from __future__ import annotations

from .rpc import RpcClient, RpcError, dec_addr, enc_addr, to_signed, words
from .uniswap import SEL_TOKEN0, WETH

V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SEL_TOKEN1 = "0xd21220a7"

_pool_tokens: dict[str, tuple[str, str]] = {}


def _tokens(rpc: RpcClient, pool: str) -> tuple[str | None, str | None]:
    if pool not in _pool_tokens:
        try:
            t0 = dec_addr(words(rpc.eth_call(pool, SEL_TOKEN0))[0])
            t1 = dec_addr(words(rpc.eth_call(pool, SEL_TOKEN1))[0])
        except (RpcError, IndexError):
            return None, None
        _pool_tokens[pool] = (t0, t1)
    return _pool_tokens[pool]


def _decode(rpc: RpcClient, log: dict) -> dict:
    pool = log["address"].lower()
    w = words(log["data"])
    t0, t1 = _tokens(rpc, pool)
    if log["topics"][0] == V2_SWAP:
        venue = "uniswap-v2"
        a0 = w[0] - w[2]   # net into pool (positive = wallet/router paid token0)
        a1 = w[1] - w[3]
    else:
        venue = "uniswap-v3"
        a0, a1 = to_signed(w[0]), to_signed(w[1])  # pool deltas: positive = into pool
    tin, tout = (t0, t1) if a0 > 0 else (t1, t0)
    amt_in, amt_out = (a0, -a1) if a0 > 0 else (a1, -a0)
    side = "buy" if tin == WETH and tout else ("sell" if tout == WETH else "swap")
    return {"block": int(log["blockNumber"], 16), "tx": log["transactionHash"], "log_index": int(log["logIndex"], 16),
            "venue": venue, "pool": pool, "token_in": tin, "token_out": tout, "amount_in_raw": str(amt_in),
            "amount_out_raw": str(amt_out), "side_vs_weth": side,
            "token": tout if side == "buy" else (tin if side == "sell" else None), "layer": "fact"}


def recent_swaps(rpc: RpcClient, wallet: str, blocks: int = 1000, limit: int = 20, chunk: int = 500) -> dict:
    rpc.ensure_mainnet()
    latest = rpc.get_block_number()
    lo = max(0, latest - blocks + 1)
    topics = [[V2_SWAP, V3_SWAP], None, "0x" + enc_addr(wallet)]
    logs: list[dict] = []
    hi = latest
    while hi >= lo and len(logs) < limit:   # newest chunk first, stop early once we have enough
        start = max(lo, hi - chunk + 1)
        logs.extend(rpc.get_logs(start, hi, topics))
        hi = start - 1
    logs.sort(key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)), reverse=True)
    return {"wallet": wallet.lower(), "from_block": lo, "to_block": latest, "scanned_to": hi + 1,
            "swaps": [_decode(rpc, x) for x in logs[:limit]],
            "match": "Uniswap V2/V3 Swap logs with recipient == wallet",
            "note": "raw recipient==wallet view; router-recipient swaps (e.g. sells to native ETH) are not captured here - "
                    "see /api/signals/scan/{wallet} for the decoder the paper engine uses"}
