"""Strict wallet gates. Fail closed: any missing input => FAIL with an 'unknown' claim.

Returns {verdict: PASS|FAIL, reasons: [...], claims: [{claim, layer}], checks: [...]}
Layers:
  fact       - a measured input value as provided by the data source (fixture or chain)
  inference  - a verdict derived from facts + thresholds
  hypothesis - a forward-looking belief (e.g. "edge persists"); never treated as known
  unknown    - required input missing; gate fails closed
"""
from __future__ import annotations

from typing import Any

from .config import Settings


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def score_wallet(wallet: dict, s: Settings) -> dict:
    stats = wallet.get("stats") or {}
    src = stats.get("source", "unknown-source")
    checks: list[dict] = []
    claims: list[dict] = []

    def gate(key: str, name: str, op: str, threshold: float, fmt: str = "{}"):
        v = stats.get(key)
        if _missing(v):
            checks.append({"gate": name, "value": None, "threshold": f"{op} {threshold}", "ok": False,
                           "reason": f"{name}: data missing -> fail closed"})
            claims.append({"claim": f"{name} is not known", "layer": "unknown"})
            return
        v = float(v)
        ok = v >= threshold if op == ">=" else v <= threshold
        checks.append({"gate": name, "value": v, "threshold": f"{op} {threshold}", "ok": ok,
                       "reason": None if ok else f"{name} {fmt.format(v)} fails {op} {fmt.format(threshold)}"})
        claims.append({"claim": f"{name} = {fmt.format(v)} (source: {src})", "layer": "fact"})

    gate("round_trips", "completed round-trips", ">=", s.gate_min_round_trips)
    gate("win_rate", "win rate", ">=", s.gate_min_win_rate, "{:.2f}")
    gate("median_hold_min", "median hold (min)", ">=", s.gate_min_median_hold_min, "{:.0f}")
    gate("buys_per_day", "spray (buys/day)", "<=", s.gate_max_buys_per_day, "{:.1f}")
    gate("leftover_pct", "leftover/unsold share", "<=", s.gate_max_leftover_pct, "{:.2f}")
    gate("top_coin_profit_share", "top-coin profit concentration", "<=", s.gate_max_top_coin_profit_share, "{:.2f}")

    note = wallet.get("independence_note")
    if s.gate_require_independence_note:
        if _missing(note):
            checks.append({"gate": "independence note", "value": None, "threshold": "required", "ok": False,
                           "reason": "independence note missing -> cannot rule out sybil/farm/insider cluster"})
            claims.append({"claim": "wallet independence is not established", "layer": "unknown"})
        else:
            checks.append({"gate": "independence note", "value": note, "threshold": "required", "ok": True,
                           "reason": None})
            claims.append({"claim": f"independent operator: {note}", "layer": "inference"})

    reasons = [c["reason"] for c in checks if not c["ok"]]
    verdict = "PASS" if not reasons else "FAIL"
    claims.append({"claim": f"gate verdict {verdict} under current thresholds", "layer": "inference"})
    if verdict == "PASS":
        claims.append({"claim": "past edge persists into the next trades", "layer": "hypothesis"})
    return {"address": wallet["address"], "verdict": verdict, "reasons": reasons,
            "checks": checks, "claims": claims}
