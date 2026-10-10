"""Market data for the paper engine: prices, pool liquidity and fill impact.

FixtureMarket  - demo tape numbers (offline).
LiveMarket     - Uniswap V2 reads on mainnet for 'live' tokens, fixture numbers for the rest.
  DATA_MODE=rpc    -> every token is live (fixture's synthetic tokens therefore fail closed: no pair).
  DATA_MODE=hybrid -> tokens present in the fixture tape with a price_path use fixture numbers;
                      any other token address (e.g. a real mainnet token in a signal) is read live.
Live reads raise QuoteUnavailable -> engine records layer=unknown and refuses the entry.
Live prices are 'now' (latest block), not the historical price at the signal's sim time.
"""
from __future__ import annotations

from .signals import DemoSource
from .uniswap import QuoteUnavailable, Uniswap


class FixtureMarket:
    name = "fixture"

    def __init__(self, src: DemoSource):
        self.src = src

    def is_live(self, token: str) -> bool:
        return False

    def price_at(self, token: str, t: float) -> float | None:
        return self.src.price_at(token, t)

    def liquidity_at(self, token: str, t: float) -> float | None:
        return self.src.liquidity_at(token, t)

    def pool_info(self, token: str) -> dict | None:
        return None

    def impact_pct(self, token: str, side: str, size_usd: float) -> float | None:
        return None  # engine falls back to IMPACT_PCT + size/liquidity model


class LiveMarket(FixtureMarket):
    name = "uniswap-v2"

    def __init__(self, src: DemoSource, uni: Uniswap, mode: str):
        super().__init__(src)
        self.uni, self.mode = uni, mode
        self._last_px: dict[str, float] = {}

    def is_live(self, token: str) -> bool:
        if self.mode == "rpc":
            return True
        tok = self.src.token(token)
        return not (tok and tok.get("price_path"))

    def price_at(self, token: str, t: float) -> float | None:
        if not self.is_live(token):
            return super().price_at(token, t)
        px = self.uni.v2_snapshot(token).price_usd  # raises QuoteUnavailable
        self._last_px[token.lower()] = px
        return px

    def last_price(self, token: str) -> float | None:
        return self._last_px.get(token.lower())

    def liquidity_at(self, token: str, t: float) -> float | None:
        if not self.is_live(token):
            return super().liquidity_at(token, t)
        return self.uni.v2_snapshot(token).liquidity_usd

    def pool_info(self, token: str) -> dict | None:
        if not self.is_live(token):
            return None
        s = self.uni.v2_snapshot(token)
        return {"venue": "uniswap-v2", "pair": s.pair, "block": s.block, "symbol": s.symbol,
                "reserves": {"token": str(s.r_token), "quote": str(s.r_quote)},
                "price_usd": s.price_usd, "liquidity_usd": round(s.liquidity_usd, 2), "eth_usd": s.eth_usd}

    def impact_pct(self, token: str, side: str, size_usd: float) -> float | None:
        if not self.is_live(token):
            return None
        return self.uni.v2_snapshot(token).impact_pct(side, size_usd)


__all__ = ["FixtureMarket", "LiveMarket", "QuoteUnavailable"]
