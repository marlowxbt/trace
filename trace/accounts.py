"""The list.

A flat file of handles whose posts get marked in the feed. It is deliberately
the dumbest possible format - one handle per line, `#` starts a note - because
the point is that a stranger can open a pull request against it without
learning anything about the codebase.

The hard rule, enforced by keeping this module out of the detector's decision
path entirely: **the list never changes a verdict.** `detector.evaluate` takes
counts and nothing else. What the list produces is a count and a handle that
ride alongside the state, and a caller is free to ignore both.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "accounts.txt"


def load(path: str | Path | None = None) -> frozenset[str]:
    """Read the list. Missing file is not an error - it means nobody is on it."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return frozenset()
    out = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        handle = raw.split("#", 1)[0].strip().lstrip("@").lower()
        if handle:
            out.add(handle)
    return frozenset(out)


def marked_in(conn: sqlite3.Connection, address: str, start_ts: int, end_ts: int,
              listed: frozenset[str]) -> tuple[int, str | None]:
    """How many listed accounts posted in this window, and which one first.

    Returns (0, None) when the list is empty, without touching the database.
    """
    if not listed:
        return 0, None
    rows = conn.execute(
        """SELECT author, MIN(created_at) AS t FROM posts
            WHERE token_address = ? AND created_at >= ? AND created_at < ?
              AND matched IS NOT NULL
            GROUP BY author ORDER BY t ASC""",
        (address, start_ts, end_ts)).fetchall()
    hits = [r[0] for r in rows if r[0] and r[0].lower() in listed]
    return len(hits), (hits[0] if hits else None)
