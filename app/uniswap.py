"""Uniswap reads on Ethereum mainnet (read-only eth_call). Never signs or sends; swap calldata for the gated live path is built in app/live.py.

V2 (primary, used by the paper liquidity gate):
  factory.getPair(token, WETH) -> pair.getReserves() -> spot price in ETH, USD via ETH/USD,
  pool liquidity USD = 2 x WETH-side reserve x ETH/USD; impact from the constant-product curve (0.3% fee).
ETH/USD: median of the two canonical V2 stable pairs (USDC/WETH, WETH/USDT); if they disagree by more
  than STABLE_MAX_DEV_PCT the read FAILS CLOSED (could be a depeg or a manipulated pool).
V3 (informational / optional fallback): factory.getPool(token, WETH, fee) over fee tiers, slot0 for
  spot price, liquidity() for in-range L, pool token balances for a TVL estimate, QuoterV2 for size impact.

Every failure raises QuoteUnavailable(reason) -> callers record layer=unknown and refuse the entry.
All reads in one quote are pinned to a single block for a consistent snapshot.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .rpc import RpcClient, RpcError, dec_addr, dec_string, enc_addr, enc_uint, to_signed, words

# ---------------------------------------------------------------- mainnet addresses (checked on Etherscan)
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
V2_FACTORY = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
V2_USDC_WETH = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"   # token0=USDC, token1=WETH
V2_WETH_USDT = "0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852"   # token0=WETH, token1=USDT
V3_FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
V3_QUOTER_V2 = "0x61ffe014ba17989e743c5f6cb21bf9697530b21e"
V3_FEE_TIERS = (500, 3000, 10000, 100)
ADDRESSES = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, str) and v.startswith("0x")}

# ---------------------------------------------------------------- selectors (keccak256 of signature, first 4 bytes)
SEL_GET_PAIR = "0xe6a43905"      # getPair(address,address)
SEL_GET_RESERVES = "0x0902f1ac"  # getReserves()
SEL_TOKEN0 = "0x0dfe1681"        # token0()
SEL_DECIMALS = "0x313ce567"      # decimals()
SEL_SYMBOL = "0x95d89b41"        # symbol()
SEL_BALANCE_OF = "0x70a08231"    # balanceOf(address)
SEL_GET_POOL = "0x1698ee82"      # getPool(address,address,uint24)
SEL_SLOT0 = "0x3850c7bd"         # slot0()
SEL_LIQUIDITY = "0x1a686502"     # liquidity()
SEL_QUOTE_IN_SINGLE = "0xc6a5026a"  # QuoterV2.quoteExactInputSingle((address,address,uint256,uint24,uint160))

ZERO = "0x" + "0" * 40
Q96 = 2 ** 96


class QuoteUnavailable(Exception):
    """Pool data missing or untrustworthy. FORM layer: unknown. Paper entries must fail closed."""

    def __init__(self, reason: str, status: int = 422):
        super().__init__(reason)
        self.reason, self.status, self.layer = reason, status, "unknown"


def v2_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    """UniswapV2Library.getAmountOut (0.3% fee)."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    a = amount_in * 997
    return a * reserve_out // (reserve_in * 1000 + a)


def v2_impact_pct(side: str, size_quote: float, r_token: int, r_quote: int, dec_token: int, dec_quote: int) -> float:
    """Total cost vs mid (curve slippage + 0.3% LP fee), in percent, for a trade worth size_quote
    (in quote-asset units, e.g. ETH) on a V2 pool. Always >= 0. Returns 100 if the pool can't fill."""
    mid = (r_quote / 10 ** dec_quote) / (r_token / 10 ** dec_token)  # quote per token
    if side == "buy":
        q_in = int(size_quote * 10 ** dec_quote)
        t_out = v2_amount_out(q_in, r_quote, r_token)
        if t_out <= 0:
            return 100.0
        eff = (q_in / 10 ** dec_quote) / (t_out / 10 ** dec_token)
        return max(0.0, (eff / mid - 1) * 100)
    t_in = int(size_quote / mid * 10 ** dec_token)
    q_out = v2_amount_out(t_in, r_token, r_quote)
    if t_in <= 0 or q_out <= 0:
        return 100.0
    eff = (q_out / 10 ** dec_quote) / (t_in / 10 ** dec_token)
    return min(100.0, max(0.0, (1 - eff / mid) * 100))


def sqrt_price_to_price(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """Human price of token0 denominated in token1."""
    return (sqrt_price_x96 / Q96) ** 2 * 10 ** (dec0 - dec1)


@dataclass
class V2Snapshot:
    token: str
    symbol: str | None
    decimals: int
    pair: str
    quote_asset: str        # WETH normally; USDC when the token *is* WETH
    quote_decimals: int
    r_token: int
    r_quote: int
    eth_usd: float
    block: int
    read_at: float

    @property
    def price_quote(self) -> float:
        return (self.r_quote / 10 ** self.quote_decimals) / (self.r_token / 10 ** self.decimals)

    @property
    def price_eth(self) -> float:
        return 1.0 if self.token == WETH else self.price_quote

    @property
    def price_usd(self) -> float:
        return self.price_quote if self.token == WETH else self.price_quote * self.eth_usd

    @property
    def liquidity_usd(self) -> float:
        quote_side = self.r_quote / 10 ** self.quote_decimals
        return 2 * quote_side * (1.0 if self.token == WETH else self.eth_usd)

    def impact_pct(self, side: str, size_usd: float) -> float:
        size_quote = size_usd if self.token == WETH else size_usd / self.eth_usd
        return v2_impact_pct(side, size_quote, self.r_token, self.r_quote, self.decimals, self.quote_decimals)

    def as_dict(self, size_usd: float | None = None) -> dict:
        d = {
            "venue": "uniswap-v2", "pair": self.pair, "quote_asset": self.quote_asset, "block": self.block,
            "reserves": {"token": str(self.r_token), "quote": str(self.r_quote)},
            "price_eth": self.price_eth, "price_usd": self.price_usd, "liquidity_usd": round(self.liquidity_usd, 2),
            "liquidity_method": "2 x quote-side reserve x ETH/USD",
            "layers": {"reserves": "fact", "price_usd": "inference", "liquidity_usd": "inference"},
        }
        if size_usd:
            d["impact"] = {"size_usd": size_usd, "buy_pct": round(self.impact_pct("buy", size_usd), 4),
                           "sell_pct": round(self.impact_pct("sell", size_usd), 4),
                           "model": "constant product incl. 0.3% LP fee", "layer": "inference"}
        return d


class Uniswap:
    def __init__(self, rpc: RpcClient, cache_s: float = 15.0, stable_max_dev_pct: float = 2.0):
        self.rpc, self.cache_s, self.max_dev = rpc, cache_s, stable_max_dev_pct
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}
        self._meta: dict[str, tuple[int, str | None]] = {}   # token -> (decimals, symbol), immutable
        self._pairs: dict[tuple[str, str], str] = {}         # found pairs are immutable

    # ------------------------------------------------------------ plumbing
    def _call(self, to: str, data: str, block: int | str = "latest") -> str:
        try:
            return self.rpc.eth_call(to, data, block)
        except RpcError as e:
            raise QuoteUnavailable(f"rpc: {e}", status=502) from None

    def _cached(self, key: str, fn):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_s:
                return hit[1]
        val = fn()
        with self._lock:
            self._cache[key] = (now, val)
        return val

    def block(self) -> int:
        try:
            self.rpc.ensure_mainnet()
            return self.rpc.get_block_number()
        except RpcError as e:
            raise QuoteUnavailable(f"rpc: {e}", status=502) from None

    def token_meta(self, token: str) -> tuple[int, str | None]:
        token = token.lower()
        if token not in self._meta:
            out = self._call(token, SEL_DECIMALS)
            w = words(out) if out != "0x" else []
            if not w or w[0] > 36:
                raise QuoteUnavailable("token decimals() missing or invalid (not an ERC-20?)")
            try:
                sym = dec_string(self._call(token, SEL_SYMBOL))
            except QuoteUnavailable:
                sym = None
            self._meta[token] = (w[0], sym)
        return self._meta[token]

    # ------------------------------------------------------------ V2
    def v2_pair(self, a: str, b: str) -> str | None:
        key = tuple(sorted((a.lower(), b.lower())))
        if key in self._pairs:
            return self._pairs[key]
        out = self._call(V2_FACTORY, SEL_GET_PAIR + enc_addr(a) + enc_addr(b))
        pair = dec_addr(words(out)[0]) if out != "0x" else ZERO
        if pair == ZERO:
            return None
        self._pairs[key] = pair
        return pair

    def _v2_reserves(self, pair: str, block: int) -> tuple[int, int, str]:
        w = words(self._call(pair, SEL_GET_RESERVES, block))
        t0 = dec_addr(words(self._call(pair, SEL_TOKEN0, block))[0])
        if len(w) < 2:
            raise QuoteUnavailable("getReserves malformed")
        return w[0], w[1], t0

    def _reserves_for(self, pair: str, token: str, block: int) -> tuple[int, int]:
        r0, r1, t0 = self._v2_reserves(pair, block)
        r_tok, r_other = (r0, r1) if t0 == token else (r1, r0)
        if r_tok == 0 or r_other == 0:
            raise QuoteUnavailable("V2 pair has zero reserves")
        return r_tok, r_other

    def eth_usd(self, block: int | None = None) -> dict:
        def read():
            b = block or self.block()
            r_usdc_w = self._reserves_for(V2_USDC_WETH, WETH, b)   # (weth, usdc)
            r_usdt_w = self._reserves_for(V2_WETH_USDT, WETH, b)   # (weth, usdt)
            p1 = (r_usdc_w[1] / 1e6) / (r_usdc_w[0] / 1e18)
            p2 = (r_usdt_w[1] / 1e6) / (r_usdt_w[0] / 1e18)
            dev = abs(p1 - p2) / min(p1, p2) * 100
            if dev > self.max_dev:
                raise QuoteUnavailable(f"ETH/USD sources disagree by {dev:.2f}% (> {self.max_dev}%)")
            px = (p1 + p2) / 2
            if not 50 <= px <= 100_000:
                raise QuoteUnavailable(f"ETH/USD {px:.2f} outside sanity band")
            return {"eth_usd": px, "sources": {"v2_usdc_weth": p1, "v2_weth_usdt": p2}, "dev_pct": round(dev, 4),
                    "block": b, "layer": "inference", "method": "mean of V2 USDC/WETH and WETH/USDT mid prices"}
        return self._cached(f"ethusd:{block}", read) if block is None else read()

    def v2_snapshot(self, token: str) -> V2Snapshot:
        token = token.lower()

        def read() -> V2Snapshot:
            b = self.block()
            px = self.eth_usd(b)["eth_usd"]
            if token == WETH:
                r_tok, r_q = self._reserves_for(V2_USDC_WETH, WETH, b)
                return V2Snapshot(token, "WETH", 18, V2_USDC_WETH, USDC, 6, r_tok, r_q, px, b, time.time())
            pair = self.v2_pair(token, WETH)
            if not pair:
                raise QuoteUnavailable("no Uniswap V2 WETH pair for token", status=404)
            dec, sym = self.token_meta(token)
            r_tok, r_w = self._reserves_for(pair, token, b)
            return V2Snapshot(token, sym, dec, pair, WETH, 18, r_tok, r_w, px, b, time.time())
        return self._cached(f"v2:{token}", read)

    # ------------------------------------------------------------ V3
    def v3_quote(self, token: str, size_usd: float | None = None) -> dict:
        """Best (highest TVL) V3 pool vs WETH (vs USDC for WETH itself)."""
        token = token.lower()

        def read() -> dict:
            b = self.block()
            px = self.eth_usd(b)["eth_usd"]
            other, other_dec, other_usd = (USDC, 6, 1.0) if token == WETH else (WETH, 18, px)
            dec, sym = (18, "WETH") if token == WETH else self.token_meta(token)
            pools = []
            for fee in V3_FEE_TIERS:
                out = self._call(V3_FACTORY, SEL_GET_POOL + enc_addr(token) + enc_addr(other) + enc_uint(fee), b)
                pool = dec_addr(words(out)[0])
                if pool == ZERO:
                    continue
                s0 = words(self._call(pool, SEL_SLOT0, b))
                liq = words(self._call(pool, SEL_LIQUIDITY, b))[0]
                bal_t = words(self._call(token, SEL_BALANCE_OF + enc_addr(pool), b))[0]
                bal_o = words(self._call(other, SEL_BALANCE_OF + enc_addr(pool), b))[0]
                if not s0 or s0[0] == 0:
                    continue
                t0_is_token = int(token, 16) < int(other, 16)
                d0, d1 = (dec, other_dec) if t0_is_token else (other_dec, dec)
                p01 = sqrt_price_to_price(s0[0], d0, d1)
                price_other = p01 if t0_is_token else (1 / p01 if p01 else 0)
                # 2 x quote-side balance: robust to dead pools with absurd slot0 prices (same method as V2).
                tvl = 2 * bal_o / 10 ** other_dec * other_usd
                pools.append({"pool": pool, "fee": fee, "sqrt_price_x96": str(s0[0]), "tick": to_signed(s0[1], 256),
                              "in_range_liquidity": str(liq), "price_in_quote": price_other,
                              "price_usd": price_other * other_usd, "tvl_usd": round(tvl, 2),
                              "token_balance": str(bal_t), "quote_balance": str(bal_o)})
            if not pools:
                raise QuoteUnavailable("no Uniswap V3 pool vs WETH", status=404)
            best = max(pools, key=lambda p: p["tvl_usd"])
            return {"venue": "uniswap-v3", "quote_asset": other, "symbol": sym, "block": b, "best": best,
                    "pools": pools, "eth_usd": px,
                    "tvl_method": "2 x quote-side (WETH/USDC) balance held by the pool x USD (includes out-of-range liquidity)",
                    "layers": {"slot0": "fact", "price_usd": "inference", "tvl_usd": "inference"}}
        q = dict(self._cached(f"v3:{token}", read))
        if size_usd:
            q["impact"] = self._v3_buy_impact(token, q, size_usd)
        return q

    def _v3_buy_impact(self, token: str, q: dict, size_usd: float) -> dict:
        """QuoterV2.quoteExactInputSingle(quote -> token) via eth_call. Not a view fn, but eth_call is a dry run."""
        best, other = q["best"], q["quote_asset"]
        other_dec = 6 if other == USDC else 18
        amount_in = int(size_usd / (1.0 if other == USDC else q["eth_usd"]) * 10 ** other_dec)
        data = (SEL_QUOTE_IN_SINGLE + enc_addr(other) + enc_addr(token) + enc_uint(amount_in)
                + enc_uint(best["fee"]) + enc_uint(0))
        try:
            out = words(self._call(V3_QUOTER_V2, data, q["block"]))[0]
            dec = 18 if token == WETH else self.token_meta(token)[0]
            eff = (amount_in / 10 ** other_dec) / (out / 10 ** dec)
            pct = max(0.0, (eff / best["price_in_quote"] - 1) * 100)
            return {"size_usd": size_usd, "buy_pct": round(pct, 4), "model": "QuoterV2 exact-input single (incl. fee)",
                    "pool": best["pool"], "layer": "inference"}
        except (QuoteUnavailable, IndexError, ZeroDivisionError) as e:
            return {"size_usd": size_usd, "buy_pct": None, "layer": "unknown", "note": f"quoter failed: {e}"}

    # ------------------------------------------------------------ combined
    def quote(self, token: str, size_usd: float | None = None) -> dict:
        """V2 is authoritative for the paper gate; V3 is best-effort and never blocks the V2 answer."""
        snap = self.v2_snapshot(token)
        out = {"token": snap.token, "symbol": snap.symbol, "decimals": snap.decimals, "chain_id": 1,
               "block": snap.block, "eth_usd": self.eth_usd(), "v2": snap.as_dict(size_usd)}
        try:
            out["v3"] = self.v3_quote(token, size_usd)
        except QuoteUnavailable as e:
            out["v3"] = {"available": False, "layer": "unknown", "reason": e.reason}
        return out
