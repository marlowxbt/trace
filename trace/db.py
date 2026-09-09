"""SQLite access. Thin on purpose: the schema is the documentation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    exists, and this project's database is meant to survive an upgrade rather
    than be thrown away - it holds posts somebody paid for."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    if "matched" not in have:
        conn.execute("ALTER TABLE posts ADD COLUMN matched TEXT")
    have = {r[1] for r in conn.execute("PRAGMA table_info(cursors)")}
    if "observed_from" not in have:
        conn.execute("ALTER TABLE cursors ADD COLUMN observed_from INTEGER")
    conn.execute("""CREATE TABLE IF NOT EXISTS authors (
        handle TEXT PRIMARY KEY, name TEXT, avatar TEXT, seen_ts INTEGER NOT NULL)""")


def upsert_token(conn: sqlite3.Connection, *, address: str, symbol: str | None,
                 name: str | None, decimals: int | None, launch_block: int,
                 launch_ts: int | None, related: Iterable[str], now_ts: int) -> None:
    conn.execute(
        """INSERT INTO tokens (address, symbol, name, decimals, launch_block,
                               launch_ts, related, first_seen_ts)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET
               symbol   = COALESCE(excluded.symbol, tokens.symbol),
               name     = COALESCE(excluded.name, tokens.name),
               decimals = COALESCE(excluded.decimals, tokens.decimals),
               launch_ts = COALESCE(excluded.launch_ts, tokens.launch_ts)""",
        (address, symbol, name, decimals, launch_block, launch_ts,
         ",".join(related), now_ts))


def active_watchlist(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT w.*, t.symbol, t.name, t.launch_ts, t.launch_block, t.related
             FROM watchlist w JOIN tokens t ON t.address = w.address
            WHERE w.removed_ts IS NULL
            ORDER BY w.added_ts"""))


def add_to_watchlist(conn: sqlite3.Connection, address: str, source: str,
                     now_ts: int, reason: str, holders: int | None = None,
                     transfers: int | None = None) -> None:
    conn.execute(
        """INSERT INTO watchlist (address, source, added_ts, removed_ts,
                                  holders, transfers, scored_ts)
           VALUES (?,?,?,NULL,?,?,?)
           ON CONFLICT(address) DO UPDATE SET
               source = excluded.source, removed_ts = NULL,
               added_ts = CASE WHEN watchlist.removed_ts IS NULL
                               THEN watchlist.added_ts ELSE excluded.added_ts END,
               holders = excluded.holders, transfers = excluded.transfers,
               scored_ts = excluded.scored_ts""",
        (address, source, now_ts, holders, transfers, now_ts))
    conn.execute(
        "INSERT INTO watchlist_history (ts, address, action, reason) VALUES (?,?,?,?)",
        (now_ts, address, "add", reason))


def update_score(conn: sqlite3.Connection, address: str, holders: int,
                 transfers: int, now_ts: int) -> None:
    conn.execute(
        "UPDATE watchlist SET holders=?, transfers=?, scored_ts=? WHERE address=?",
        (holders, transfers, now_ts, address))


def remove_from_watchlist(conn: sqlite3.Connection, address: str, now_ts: int,
                          reason: str) -> None:
    conn.execute("UPDATE watchlist SET removed_ts=? WHERE address=? AND removed_ts IS NULL",
                 (now_ts, address))
    conn.execute(
        "INSERT INTO watchlist_history (ts, address, action, reason) VALUES (?,?,?,?)",
        (now_ts, address, "remove", reason))
