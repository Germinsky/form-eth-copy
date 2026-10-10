"""Settings loaded from .env. LIVE_ENABLED must stay false — enforced here."""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TRUTHY = {"1", "true", "yes", "on"}


class LiveTradingForbidden(RuntimeError):
    """Raised when anything tries to turn live trading on. This MVP is paper only."""


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(float(os.getenv(name, default)))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in TRUTHY


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    live_enabled: bool
    data_mode: str
    rpc_url: str
    expected_chain_id: int
    rpc_timeout_s: float
    quote_cache_s: float
    stable_max_dev_pct: float
    logs_chunk_blocks: int
    signal_fixture: Path
    signal_source: str
    signal_scan: str
    signal_lookback_blocks: int
    signal_confirmations: int
    signal_max_blocks_per_poll: int
    signal_blockscan_max: int
    signal_max_receipts_per_poll: int
    signal_max_age_s: int
    signal_poll_seconds: int
    rpc_max_rps: float
    paper_start_usd: float
    paper_size_usd: float
    paper_gas_usd: float
    lag_ms: int
    impact_pct: float
    liquidity_floor_usd: float
    gate_min_round_trips: int
    gate_min_win_rate: float
    gate_min_median_hold_min: float
    gate_max_buys_per_day: float
    gate_max_leftover_pct: float
    gate_max_top_coin_profit_share: float
    gate_require_independence_note: bool
    cluster_min_wallets: int
    cluster_window_s: int
    cluster_required: bool
    solo_size_fraction: float
    tick_seconds: int
    auto_tick_seconds: int
    db_path: Path
    record_path: Path

    @property
    def rpc_configured(self) -> bool:
        return bool(self.rpc_url) and "YOUR_KEY" not in self.rpc_url and "YOUR_PROJECT" not in self.rpc_url

    @property
    def live_reads(self) -> bool:
        """True when the paper engine should use live Uniswap reads (rpc/hybrid AND an RPC URL is set).
        DATA_MODE=rpc|hybrid without an RPC URL degrades to demo (fixture) numbers."""
        return self.data_mode in ("rpc", "hybrid") and self.rpc_configured

    @property
    def effective_data_mode(self) -> str:
        return self.data_mode if self.live_reads else "demo"

    @property
    def live_signals(self) -> bool:
        """Live wallet-swap signals need SIGNAL_SOURCE=live AND live reads (DATA_MODE rpc|hybrid + RPC URL):
        live signals reference real tokens, which only a live market can price. Otherwise: fixture tape."""
        return self.signal_source == "live" and self.live_reads

    @property
    def effective_signal_source(self) -> str:
        return "live" if self.live_signals else "fixture"

    def public(self) -> dict:
        """Config safe to show in UI/API (RPC URL redacted)."""
        d = asdict(self)
        d["rpc_url"] = _redact(self.rpc_url)
        d["rpc_configured"] = self.rpc_configured
        d["effective_data_mode"] = self.effective_data_mode
        d["effective_signal_source"] = self.effective_signal_source
        d["signal_fixture"] = str(self.signal_fixture)
        d["db_path"] = str(self.db_path)
        d["record_path"] = str(self.record_path)
        return d


def _redact(url: str) -> str:
    """Keep scheme+host only; keys live in the path or query (Alchemy/Infura) and must never be shown."""
    if not url:
        return ""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname}/***" if u.hostname else "***"


def load_settings() -> Settings:
    s = Settings(
        live_enabled=_b("LIVE_ENABLED", False),
        data_mode=os.getenv("DATA_MODE", "demo").strip().lower(),
        # ETH_RPC_URL preferred; RPC_URL kept for backwards compatibility with older .env files.
        rpc_url=(os.getenv("ETH_RPC_URL") or os.getenv("RPC_URL") or "").strip(),
        expected_chain_id=_i("EXPECTED_CHAIN_ID", 1),
        rpc_timeout_s=_f("RPC_TIMEOUT_S", 8),
        quote_cache_s=_f("QUOTE_CACHE_S", 15),
        stable_max_dev_pct=_f("STABLE_MAX_DEV_PCT", 2.0),
        logs_chunk_blocks=_i("LOGS_CHUNK_BLOCKS", 500),
        signal_fixture=_path("SIGNAL_FIXTURE", "data/fixture_signals.json"),
        signal_source=os.getenv("SIGNAL_SOURCE", "fixture").strip().lower(),
        signal_scan=os.getenv("SIGNAL_SCAN", "auto").strip().lower(),
        signal_lookback_blocks=_i("SIGNAL_LOOKBACK_BLOCKS", 300),
        signal_confirmations=_i("SIGNAL_CONFIRMATIONS", 2),
        signal_max_blocks_per_poll=_i("SIGNAL_MAX_BLOCKS_PER_POLL", 2000),
        signal_blockscan_max=_i("SIGNAL_BLOCKSCAN_MAX", 20),
        signal_max_receipts_per_poll=_i("SIGNAL_MAX_RECEIPTS_PER_POLL", 100),
        signal_max_age_s=_i("SIGNAL_MAX_AGE_S", 900),
        signal_poll_seconds=_i("SIGNAL_POLL_SECONDS", 30),
        rpc_max_rps=_f("RPC_MAX_RPS", 5),
        paper_start_usd=_f("PAPER_START_USD", 10000),
        paper_size_usd=_f("PAPER_SIZE_USD", 500),
        paper_gas_usd=_f("PAPER_GAS_USD", 8),
        lag_ms=_i("LAG_MS", 12000),
        impact_pct=_f("IMPACT_PCT", 0.6),
        liquidity_floor_usd=_f("LIQUIDITY_FLOOR_USD", 150000),
        gate_min_round_trips=_i("GATE_MIN_ROUND_TRIPS", 20),
        gate_min_win_rate=_f("GATE_MIN_WIN_RATE", 0.55),
        gate_min_median_hold_min=_f("GATE_MIN_MEDIAN_HOLD_MIN", 30),
        gate_max_buys_per_day=_f("GATE_MAX_BUYS_PER_DAY", 12),
        gate_max_leftover_pct=_f("GATE_MAX_LEFTOVER_PCT", 0.30),
        gate_max_top_coin_profit_share=_f("GATE_MAX_TOP_COIN_PROFIT_SHARE", 0.50),
        gate_require_independence_note=_b("GATE_REQUIRE_INDEPENDENCE_NOTE", True),
        cluster_min_wallets=_i("CLUSTER_MIN_WALLETS", 2),
        cluster_window_s=_i("CLUSTER_WINDOW_S", 1800),
        cluster_required=_b("CLUSTER_REQUIRED", True),
        solo_size_fraction=_f("SOLO_SIZE_FRACTION", 0.5),
        tick_seconds=_i("TICK_SECONDS", 900),
        auto_tick_seconds=_i("AUTO_TICK_SECONDS", 0),
        db_path=_path("DB_PATH", "data/form.db"),
        record_path=_path("RECORD_PATH", "data/the_record.jsonl"),
    )
    assert_live_dark(s)
    if s.data_mode not in ("demo", "rpc", "hybrid"):
        raise RuntimeError("DATA_MODE must be demo | rpc | hybrid")
    if s.signal_source not in ("fixture", "live"):
        raise RuntimeError("SIGNAL_SOURCE must be fixture | live")
    if s.signal_scan not in ("auto", "logs", "blocks"):
        raise RuntimeError("SIGNAL_SCAN must be auto | logs | blocks")
    if s.expected_chain_id != 1:
        raise RuntimeError("Ethereum mainnet only: EXPECTED_CHAIN_ID must be 1")
    return s


def assert_live_dark(s: Settings) -> None:
    if s.live_enabled:
        raise LiveTradingForbidden(
            "LIVE_ENABLED is true. This build is PAPER ONLY: there is no signer, no key "
            "handling and no order routing. Set LIVE_ENABLED=false."
        )


settings = load_settings()
