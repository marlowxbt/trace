"""Minimal JSON-RPC client for the chain. Read-only by construction: there is
no signer here and no method that can send a transaction."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

import requests

# Methods this client is allowed to call. Hard constraint 1 lives here: nothing
# that could broadcast a transaction is reachable through this class.
READ_ONLY_METHODS = frozenset({
    "eth_chainId",
    "eth_blockNumber",
    "eth_getBlockByNumber",
    "eth_getBlockByHash",
    "eth_getLogs",
    "eth_call",
    "eth_getCode",
    "eth_getBalance",
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
})


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Log:
    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    tx_hash: str
    log_index: int

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Log":
        return cls(
            address=raw["address"].lower(),
            topics=tuple(t.lower() for t in raw["topics"]),
            data=raw.get("data", "0x"),
            block_number=int(raw["blockNumber"], 16),
            tx_hash=raw["transactionHash"],
            log_index=int(raw["logIndex"], 16),
        )


class Rpc:
    def __init__(self, url: str, timeout_s: int = 20, max_batch: int = 50,
                 retries: int = 5, min_interval_s: float = 0.12):
        self.url = url
        self.timeout_s = timeout_s
        self.max_batch = max_batch
        self.retries = retries
        # The public node rate-limits. Pace ourselves rather than discovering
        # the limit with a 429 on every burst.
        self.min_interval_s = min_interval_s
        self._session = requests.Session()
        self._ids = itertools.count(1)
        self._last_post = 0.0
        self.calls = 0
        self.throttled = 0

    # -- transport ----------------------------------------------------------

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_post
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_post = time.monotonic()

    def _post(self, payload) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            self._pace()
            try:
                r = self._session.post(
                    self.url, json=payload,
                    headers={"content-type": "application/json"},
                    timeout=self.timeout_s,
                )
                if r.status_code == 429:
                    self.throttled += 1
                    retry_after = r.headers.get("Retry-After")
                    wait = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() \
                        else 1.0 * (2 ** attempt)
                    # Back off for everyone, not just this call.
                    self.min_interval_s = min(self.min_interval_s * 1.5, 2.0)
                    time.sleep(min(wait, 30.0))
                    last = RuntimeError("429 rate limited")
                    continue
                r.raise_for_status()
                self.calls += 1
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last = exc
                time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"rpc unreachable after {self.retries} tries: {last}")

    def call(self, method: str, params: Sequence[Any] | None = None) -> Any:
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"{method} is not on the read-only method list")
        body = self._post({"jsonrpc": "2.0", "id": next(self._ids),
                           "method": method, "params": list(params or [])})
        if "error" in body:
            raise RpcError(body["error"].get("code", 0),
                           body["error"].get("message", ""))
        return body["result"]

    def batch(self, requests_: Sequence[tuple[str, Sequence[Any]]],
              raise_on_error: bool = False) -> list[Any]:
        """Send calls in one round trip. Returns results in request order;
        a failed element is None unless raise_on_error."""
        out: list[Any] = []
        for start in range(0, len(requests_), self.max_batch):
            chunk = requests_[start:start + self.max_batch]
            for method, _ in chunk:
                if method not in READ_ONLY_METHODS:
                    raise ValueError(f"{method} is not on the read-only method list")
            payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": list(p)}
                       for i, (m, p) in enumerate(chunk)]
            body = self._post(payload)
            if isinstance(body, dict):  # some nodes reject batches
                raise RuntimeError(f"node did not answer a batch: {body}")
            by_id = {e["id"]: e for e in body}
            for i in range(len(chunk)):
                entry = by_id.get(i, {})
                if "error" in entry:
                    if raise_on_error:
                        raise RpcError(entry["error"].get("code", 0),
                                       entry["error"].get("message", ""))
                    out.append(None)
                else:
                    out.append(entry.get("result"))
        return out

    # -- convenience --------------------------------------------------------

    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def block_timestamp(self, number: int) -> int:
        blk = self.call("eth_getBlockByNumber", [hex(number), False])
        return int(blk["timestamp"], 16)

    def block_timestamps(self, numbers: Sequence[int]) -> dict[int, int]:
        res = self.batch([("eth_getBlockByNumber", [hex(n), False]) for n in numbers])
        return {n: int(b["timestamp"], 16)
                for n, b in zip(numbers, res) if b is not None}

    def get_logs(self, *, address: str | Sequence[str] | None = None,
                 topics: Sequence[Any] | None = None,
                 from_block: int, to_block: int,
                 page_blocks: int = 50_000) -> Iterator[Log]:
        """Paged eth_getLogs. The node caps a response at 10 000 logs; when we
        hit that cap the range is halved and retried, so no log is silently
        dropped."""
        pending: list[tuple[int, int]] = []
        lo = from_block
        while lo <= to_block:
            hi = min(lo + page_blocks - 1, to_block)
            pending.append((lo, hi))
            lo = hi + 1
        pending.reverse()  # process in ascending order via pop()

        while pending:
            lo, hi = pending.pop()
            flt: dict[str, Any] = {"fromBlock": hex(lo), "toBlock": hex(hi)}
            if address is not None:
                flt["address"] = address
            if topics:
                flt["topics"] = list(topics)
            try:
                raw = self.call("eth_getLogs", [flt])
            except RpcError as exc:
                if "exceeds limit" not in exc.message and "too many" not in exc.message.lower():
                    raise
                if hi <= lo:
                    raise RuntimeError(
                        f"block {lo} alone exceeds the node's log cap") from exc
                mid = (lo + hi) // 2
                pending.append((mid + 1, hi))
                pending.append((lo, mid))
                continue
            for item in raw:
                yield Log.parse(item)

    def find_block_at_timestamp(self, target_ts: int, *, head: int | None = None,
                                tolerance_s: int = 2) -> int:
        """First block at or after target_ts. Interpolation-guided search:
        blocks here are ~0.1s apart, so a linear guess lands close and the
        binary search only has to clean up."""
        lo, hi = 1, head if head is not None else self.block_number()
        lo_ts = self.block_timestamp(lo)
        hi_ts = self.block_timestamp(hi)
        if target_ts <= lo_ts:
            return lo
        if target_ts >= hi_ts:
            return hi
        for _ in range(64):
            if hi - lo <= 1:
                break
            span_ts = max(hi_ts - lo_ts, 1)
            frac = (target_ts - lo_ts) / span_ts
            guess = lo + int((hi - lo) * frac)
            guess = min(max(guess, lo + 1), hi - 1)
            guess_ts = self.block_timestamp(guess)
            if abs(guess_ts - target_ts) <= tolerance_s:
                return guess
            if guess_ts < target_ts:
                lo, lo_ts = guess, guess_ts
            else:
                hi, hi_ts = guess, guess_ts
        return hi
