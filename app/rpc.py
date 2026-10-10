"""Read-only Ethereum JSON-RPC client (httpx). Never signs, never broadcasts.

Deliberately NOT web3.py: web3 pulls in eth-account (key handling / signing). This module can only
issue read methods from READ_METHODS; anything else (eth_sendRawTransaction, eth_sign, ...) is refused
before it leaves the process.
"""
from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable

import httpx

MAINNET_CHAIN_ID = 1
READ_METHODS = frozenset({
    "eth_chainId", "eth_blockNumber", "eth_call", "eth_getLogs", "eth_getBlockByNumber",
    "eth_getCode", "eth_getBalance", "eth_getTransactionByHash", "eth_getTransactionReceipt",
    "eth_estimateGas", "eth_gasPrice",   # dry-run simulation only (no state change, no signature)
})


class RpcError(RuntimeError):
    """Transport / JSON-RPC error. Callers treat this as data UNKNOWN (fail closed)."""


class WrongChain(RpcError):
    pass


# Transport: (payload dict) -> response dict. Injectable so tests can run fully offline.
Transport = Callable[[dict], dict]


def http_transport(url: str, timeout: float) -> Transport:
    client = httpx.Client(timeout=timeout, headers={"Content-Type": "application/json"})

    def send(payload: dict) -> dict:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()
    return send


class RpcClient:
    """max_rps > 0 spaces requests at least 1/max_rps seconds apart (process-wide for this client, thread-safe).
    HTTP 429 is retried up to `retries_429` times with exponential backoff, then surfaces as RpcError."""

    def __init__(self, url: str = "", timeout: float = 8.0, transport: Transport | None = None,
                 max_rps: float = 0.0, retries_429: int = 2, backoff_s: float = 1.0):
        if not transport and not url:
            raise RpcError("no RPC URL configured")
        self._send = transport or http_transport(url, timeout)
        self._ids = itertools.count(1)
        self.calls = 0  # simple counter, surfaced in /api/health for rate-limit awareness
        self.throttled = 0  # number of HTTP 429s seen
        self._chain_ok = False
        self._min_gap = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        self._retries_429, self._backoff = retries_429, backoff_s
        self._rl_lock = threading.Lock()
        self._next_at = 0.0

    def _pace(self) -> None:
        if not self._min_gap:
            return
        with self._rl_lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._min_gap
        if wait > 0:
            time.sleep(wait)

    # ------------------------------------------------------------ core
    def call(self, method: str, params: list | None = None) -> Any:
        if method not in READ_METHODS:
            raise RpcError(f"method {method!r} refused: read-only client (paper only)")
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        attempt = 0
        while True:
            self._pace()
            self.calls += 1
            try:
                resp = self._send(payload)
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code == 429:
                    self.throttled += 1
                    if attempt < self._retries_429:
                        time.sleep(self._backoff * 2 ** attempt)
                        attempt += 1
                        continue
                raise RpcError(f"{method}: HTTP {code}") from None
            except Exception as e:  # network, timeout, bad JSON
                raise RpcError(f"{method}: {type(e).__name__}") from None
        if not isinstance(resp, dict):
            raise RpcError(f"{method}: malformed response")
        if resp.get("error"):
            err = resp["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise RpcError(f"{method}: {str(msg)[:200]}")
        if "result" not in resp:
            raise RpcError(f"{method}: no result")
        return resp["result"]

    # ------------------------------------------------------------ helpers
    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def verify_mainnet(self) -> int:
        cid = self.chain_id()
        if cid != MAINNET_CHAIN_ID:
            raise WrongChain(f"chainId {cid} != 1 (Ethereum mainnet only)")
        self._chain_ok = True
        return cid

    def ensure_mainnet(self) -> None:
        if not self._chain_ok:
            self.verify_mainnet()

    def get_block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def eth_call(self, to: str, data: str, block: str | int = "latest") -> str:
        self.ensure_mainnet()
        tag = hex(block) if isinstance(block, int) else block
        out = self.call("eth_call", [{"to": to, "data": data}, tag])
        if not isinstance(out, str) or not out.startswith("0x"):
            raise RpcError("eth_call: non-hex result")
        return out

    def simulate(self, tx: dict, block: str | int = "latest") -> str:
        """eth_call a full unsigned tx {from,to,value,data}. Dry run: nothing is signed or sent."""
        self.ensure_mainnet()
        tag = hex(block) if isinstance(block, int) else block
        out = self.call("eth_call", [tx, tag])
        if not isinstance(out, str) or not out.startswith("0x"):
            raise RpcError("eth_call: non-hex result")
        return out

    def estimate_gas(self, tx: dict) -> int:
        self.ensure_mainnet()
        return int(self.call("eth_estimateGas", [tx]), 16)

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice"), 16)

    def get_balance(self, addr: str, block: str | int = "latest") -> int:
        self.ensure_mainnet()
        tag = hex(block) if isinstance(block, int) else block
        return int(self.call("eth_getBalance", [addr, tag]), 16)

    def get_logs(self, from_block: int, to_block: int, topics: list, address: str | list | None = None) -> list:
        self.ensure_mainnet()
        flt: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}
        if address:
            flt["address"] = address
        out = self.call("eth_getLogs", [flt])
        if not isinstance(out, list):
            raise RpcError("eth_getLogs: non-list result")
        return out

    def get_receipt(self, tx_hash: str) -> dict | None:
        self.ensure_mainnet()
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def get_tx(self, tx_hash: str) -> dict | None:
        self.ensure_mainnet()
        return self.call("eth_getTransactionByHash", [tx_hash])

    def get_block(self, number: int, full_txs: bool = False) -> dict | None:
        self.ensure_mainnet()
        return self.call("eth_getBlockByNumber", [hex(number), full_txs])


# ---------------------------------------------------------------- tiny ABI helpers (static types only)
def enc_addr(a: str) -> str:
    a = a.lower().removeprefix("0x")
    if len(a) != 40:
        raise ValueError("bad address")
    return a.rjust(64, "0")


def enc_uint(n: int) -> str:
    if n < 0:
        raise ValueError("uint < 0")
    return hex(n)[2:].rjust(64, "0")


def words(hexdata: str) -> list[int]:
    h = hexdata.removeprefix("0x")
    if len(h) % 64:
        raise RpcError("ABI: result length not a multiple of 32 bytes")
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def dec_addr(word: int) -> str:
    return "0x" + hex(word)[2:].rjust(40, "0")[-40:]


def to_signed(word: int, bits: int = 256) -> int:
    return word - (1 << bits) if word >= 1 << (bits - 1) else word


def dec_string(hexdata: str) -> str | None:
    """Decode an ABI string, or a bytes32 symbol (old tokens like MKR). None if undecodable."""
    h = hexdata.removeprefix("0x")
    try:
        if len(h) == 64:  # bytes32
            return bytes.fromhex(h).rstrip(b"\0").decode("utf-8", "replace") or None
        off = int(h[:64], 16) * 2
        ln = int(h[off:off + 64], 16)
        return bytes.fromhex(h[off + 64:off + 64 + ln * 2]).decode("utf-8", "replace")
    except Exception:
        return None
