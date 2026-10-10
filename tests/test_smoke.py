"""Smoke test: python -m pytest -q  (or: python tests/test_smoke.py)
Uses a temp DB/record so it never touches data/."""
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=f"{tmp}/t.db", RECORD_PATH=f"{tmp}/r.jsonl", LIVE_ENABLED="false", DATA_MODE="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import LiveTradingForbidden, assert_live_dark, settings  # noqa: E402
from app.main import app  # noqa: E402


def test_demo_cycle():
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["live_enabled"] is False and h["chain_id"] == 1
        ws = {w["address"]: w for w in c.get("/api/wallets").json()}
        assert ws["0xa11ce00000000000000000000000000000000001"]["gate"]["verdict"] == "PASS"
        beef = ws["0xbeef000000000000000000000000000000000006"]
        assert beef["gate"]["verdict"] == "FAIL" and beef["status"] == "parked"  # missing data -> fail closed
        assert any(cl["layer"] == "unknown" for cl in beef["gate"]["claims"])
        r = c.post("/api/paper/run").json()
        assert r["done"]
        pf = c.get("/api/portfolio").json()
        assert len(pf["closed_positions"]) == 1 and len(pf["open_positions"]) == 1
        assert pf["closed_positions"][0]["symbol"] == "DEMOA"
        recs = c.get("/api/records?limit=2000").json()
        kinds = {x["kind"] for x in recs}
        assert {"signal", "skip", "cluster", "cluster_pending", "paper_fill", "paper_exit"} <= kinds
        assert all(set(x) == {"id", "session", "t_event", "kind", "layer", "content"} for x in recs)
        # watch on a FAIL wallet is refused
        assert c.patch("/api/wallets/0xcafe000000000000000000000000000000000004", json={"status": "watched"}).status_code == 409


def test_live_flag_refused():
    from dataclasses import replace
    try:
        assert_live_dark(replace(settings, live_enabled=True))
        raise AssertionError("should have refused")
    except LiveTradingForbidden:
        pass


if __name__ == "__main__":
    test_demo_cycle(); test_live_flag_refused(); print("ok")
