"""the_record: append-only JSONL log.

Every line: {id, session, t_event, kind, layer, content}
The file is only ever opened in append mode. Nothing in this codebase rewrites or truncates it.
"""
from __future__ import annotations

import json
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAYERS = ("fact", "inference", "hypothesis", "unknown")

SESSION = "s-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Record:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._count_lines()

    def _count_lines(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    def append(self, kind: str, layer: str, content: Any) -> dict:
        if layer not in LAYERS:
            raise ValueError(f"layer must be one of {LAYERS}, got {layer!r}")
        with _lock:
            self._seq += 1
            entry = {
                "id": f"rec-{self._seq:06d}",
                "session": SESSION,
                "t_event": _now(),
                "kind": kind,
                "layer": layer,
                "content": content,
            }
            with self.path.open("a", encoding="utf-8") as fh:  # append-only
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return entry

    def tail(self, limit: int = 100, kind: str | None = None, layer: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out: deque = deque(maxlen=limit)
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and e.get("kind") != kind:
                    continue
                if layer and e.get("layer") != layer:
                    continue
                out.append(e)
        return list(reversed(out))  # newest first
