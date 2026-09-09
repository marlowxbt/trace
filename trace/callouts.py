"""The second lane: how much money is behind the talking.

pump.fun carries Robinhood Chain as a first-class network, and every coin page
has a **Callouts** tab. The obvious idea is to treat it as a second post source.
Looking at it settled that: **the callouts are X posts**. Same handles, same X
icon, same text - the collector already reads them through its own feed.
Counting them again would double every rate and turn the age curve we measured
into noise.

What is actually new there is attached to the caller, not to the post:

    position    $6,258.33 held in this coin, right now
    spent       $10.3k put in
    entry       average entry at $4.1M market cap
    pnl         -$3,979.78, -38.5%

Nobody else joins "who is talking" to "do they have money on it". That is this
project's own thesis - attention beside the book - pointed at other people
instead of at you.

So this module is **not** a feed provider. It maps author -> position for a
token, and the collector reports, per window, how much money stands behind the
accounts that posted. It never enters the multiplier, for the same reason
accounts.txt never does: a number you can buy must not move a verdict.

Two things to be honest about, on the site and here:

1. **pump.fun pays for callouts.** They run a programme that pays per call, so
   the *count* of callouts is bought volume and is worth nothing as attention.
   The position is the part that cannot be faked without real money, and it is
   the only part this module keeps.
2. **There is no public API for it.** The feed arrives over a websocket into
   their own front end; single callouts hydrate from
   `frontend-api-v3.pump.fun/callout/{uuid}`. That is their application's
   private interface, not a product: it can change or close any day, and it
   cannot carry the promise the rest of TRACE makes - run it yourself, check
   every number. Anything sourced here is labelled `pumpfun` in storage so a
   reader can always tell which numbers came from where.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SOURCE = "pumpfun"


@dataclass(frozen=True)
class Position:
    """One caller's stake in one token, as their front end reported it."""
    author: str                  # X handle, lowercased
    token_address: str
    position_usd: float          # what the stake is worth now
    spent_usd: float | None      # what went in
    pnl_usd: float | None
    entry_mc_usd: float | None   # average entry, in market cap
    seen_ts: int
    source: str = SOURCE


def normalise_handle(h: str | None) -> str | None:
    if not h:
        return None
    return h.strip().lstrip("@").lower() or None


def money(v) -> float | None:
    """Their front end prints '$10.3k' and '$1.7M'. Store numbers, not strings."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "").replace("+", "")
    neg = s.startswith("-")
    s = s.lstrip("-")
    mult = 1.0
    if s[-1:].lower() == "k":
        mult, s = 1e3, s[:-1]
    elif s[-1:].lower() == "m":
        mult, s = 1e6, s[:-1]
    elif s[-1:].lower() == "b":
        mult, s = 1e9, s[:-1]
    try:
        n = float(s) * mult
    except ValueError:
        return None
    return -n if neg else n


def store(conn: sqlite3.Connection, p: Position) -> None:
    """One row per author per token. The latest reading wins - a position is a
    fact about now, not a log."""
    conn.execute(
        """INSERT INTO positions (token_address, author, position_usd, spent_usd,
                                  pnl_usd, entry_mc_usd, source, seen_ts)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(token_address, author) DO UPDATE SET
               position_usd = excluded.position_usd,
               spent_usd    = COALESCE(excluded.spent_usd, positions.spent_usd),
               pnl_usd      = excluded.pnl_usd,
               entry_mc_usd = COALESCE(excluded.entry_mc_usd, positions.entry_mc_usd),
               source       = excluded.source,
               seen_ts      = excluded.seen_ts""",
        (p.token_address, p.author, p.position_usd, p.spent_usd, p.pnl_usd,
         p.entry_mc_usd, p.source, p.seen_ts))


def ingest(conn: sqlite3.Connection, token_address: str, records, now: int) -> int:
    """Take whatever the caller scraped and keep only what is defensible.

    `records` are dicts in their front end's shape. A record with no handle, or
    no position, is dropped: an author we cannot name cannot be matched to a
    post, and a caller with no stake is exactly the paid-callout noise this
    module exists to ignore.
    """
    n = 0
    for r in records or []:
        who = normalise_handle(r.get("author") or r.get("username")
                               or r.get("handle"))
        pos = money(r.get("position") or r.get("position_usd"))
        if not who or not pos:
            continue
        store(conn, Position(
            author=who, token_address=token_address, position_usd=pos,
            spent_usd=money(r.get("spent") or r.get("spent_usd")),
            pnl_usd=money(r.get("profit") or r.get("pnl") or r.get("pnl_usd")),
            entry_mc_usd=money(r.get("entry_mc") or r.get("avg_entry")
                               or r.get("entry_mc_usd")),
            seen_ts=now))
        n += 1
    return n


def money_in_window(conn: sqlite3.Connection, address: str, start_ts: int,
                    end_ts: int) -> tuple[int, float]:
    """Of the accounts that posted in this window, how many hold the coin and
    for how much.

    Counted posts only - `matched IS NOT NULL` - so a post that was not about
    this token cannot drag its author's position in with it.
    """
    row = conn.execute(
        """SELECT COUNT(DISTINCT p.author), COALESCE(SUM(pos.position_usd), 0)
             FROM posts p
             JOIN positions pos
               ON pos.author = p.author AND pos.token_address = p.token_address
            WHERE p.token_address = ? AND p.created_at >= ? AND p.created_at < ?
              AND p.matched IS NOT NULL""",
        (address, start_ts, end_ts)).fetchone()
    return (row[0] or 0, float(row[1] or 0.0)) if row else (0, 0.0)
