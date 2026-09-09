"""Chain reads for Pons: launches, token metadata, traction.

Everything here is eth_getLogs / eth_call. Nothing writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .rpc import Log, Rpc

# Pons v2 factory, token-creation event. Verified against the live chain:
# topic1 is the new ERC-20 (name()/symbol() answer on it), topic2 and topic3
# are two contracts deployed or referenced alongside it. Their exact roles are
# not needed for the watchlist, so they are carried through unnamed rather
# than guessed at.
TOPIC_TOKEN_CREATED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"

# keccak("Transfer(address,address,uint256)")
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"

SEL_NAME = "0x06fdde03"
SEL_SYMBOL = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"


@dataclass(frozen=True)
class Launch:
    token: str
    related: tuple[str, ...]
    block: int
    tx_hash: str


@dataclass(frozen=True)
class TokenMeta:
    address: str
    name: str | None
    symbol: str | None
    decimals: int | None


@dataclass(frozen=True)
class Traction:
    token: str
    transfers: int
    holders: int  # distinct receiving addresses, pool and burn excluded


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_string(raw: str | None) -> str | None:
    """ABI string return, tolerating the bytes32 tokens still in the wild."""
    if not raw or raw == "0x":
        return None
    try:
        b = bytes.fromhex(raw[2:])
    except ValueError:
        return None
    if len(b) >= 64:
        offset = int.from_bytes(b[0:32], "big")
        if offset == 32:
            length = int.from_bytes(b[32:64], "big")
            if 0 <= length <= len(b) - 64:
                s = b[64:64 + length].decode("utf-8", "replace")
                return s.replace("\x00", "").strip() or None
    if len(b) == 32:
        s = b.rstrip(b"\x00").decode("utf-8", "replace")
        return s.replace("\x00", "").strip() or None
    return None


def scan_launches(rpc: Rpc, factory: str, from_block: int, to_block: int,
                  page_blocks: int = 50_000) -> list[Launch]:
    """Every token created by the factory in [from_block, to_block]."""
    out: list[Launch] = []
    for log in rpc.get_logs(address=factory, topics=[TOPIC_TOKEN_CREATED],
                            from_block=from_block, to_block=to_block,
                            page_blocks=page_blocks):
        if len(log.topics) < 2:
            continue
        out.append(Launch(
            token=_addr(log.topics[1]),
            related=tuple(_addr(t) for t in log.topics[2:]),
            block=log.block_number,
            tx_hash=log.tx_hash,
        ))
    return out


def token_meta(rpc: Rpc, addresses: Sequence[str]) -> dict[str, TokenMeta]:
    """name/symbol/decimals for many tokens in as few round trips as possible."""
    calls: list[tuple[str, list]] = []
    for a in addresses:
        for sel in (SEL_NAME, SEL_SYMBOL, SEL_DECIMALS):
            calls.append(("eth_call", [{"to": a, "data": sel}, "latest"]))
    res = rpc.batch(calls)
    out: dict[str, TokenMeta] = {}
    for i, a in enumerate(addresses):
        name_r, sym_r, dec_r = res[3 * i], res[3 * i + 1], res[3 * i + 2]
        decimals = None
        if dec_r and dec_r != "0x":
            try:
                d = int(dec_r, 16)
                decimals = d if 0 <= d <= 36 else None
            except ValueError:
                decimals = None
        out[a] = TokenMeta(address=a, name=decode_string(name_r),
                           symbol=decode_string(sym_r), decimals=decimals)
    return out


def traction(rpc: Rpc, tokens: Sequence[str], from_block: int, to_block: int,
             ignore: dict[str, set[str]] | None = None,
             chunk: int = 150, page_blocks: int = 50_000) -> dict[str, Traction]:
    """Transfers and distinct receivers per token over a block window.

    A proxy for "is anyone actually trading this", built only from ERC-20
    Transfer logs so it needs no Pons ABI. Receivers that are the token's own
    related contracts, the zero address or the burn address do not count."""
    ignore = ignore or {}
    counts: dict[str, int] = {t: 0 for t in tokens}
    receivers: dict[str, set[str]] = {t: set() for t in tokens}

    for start in range(0, len(tokens), chunk):
        batch = list(tokens[start:start + chunk])
        for log in rpc.get_logs(address=batch, topics=[TOPIC_TRANSFER],
                                from_block=from_block, to_block=to_block,
                                page_blocks=page_blocks):
            tok = log.address
            if tok not in counts or len(log.topics) < 3:
                continue
            counts[tok] += 1
            to = _addr(log.topics[2])
            if to in (ZERO, DEAD) or to == tok or to in ignore.get(tok, ()):
                continue
            receivers[tok].add(to)

    return {t: Traction(token=t, transfers=counts[t], holders=len(receivers[t]))
            for t in tokens}
