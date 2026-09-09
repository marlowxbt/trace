"""Stage 3 - the detector.

One rule set for every token, no per-token tuning, no hand-picking.

The whole module is deliberately free of network, config and clock access:
every function takes what it needs as an argument. That is what makes the
replay test deterministic.

Definitions, fixed once and used everywhere:

    window      300 seconds, aligned to the unix epoch
    rate        posts in the last COMPLETE window
    baseline    mean of the 6 complete windows behind it (30 minutes)
    multiplier  rate / max(baseline, 1.0)
    floor       5 posts in a window - below it a token never fires
    warm-up     those 6 baseline windows must be fully observed
    cooldown    1800 seconds per token after a SPIKE

A window counts as fully observed when the whole of it falls after the moment
the token entered the watchlist. An observed window with zero posts is still
observed; an unobserved one is not zero, it is missing.

That single sentence is the entire history rule. There is no partial
averaging, no zero padding, no grace period and no special case for young
tokens: until the six windows are there the token is WARMING and cannot fire.
The first verdict therefore lands 35 minutes after admission (6 baseline
windows plus the one being rated), which the 4 hour sticky window covers with
room to spare.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from . import accounts as _accounts
from . import callouts as _callouts

WARMING = "WARMING"
QUIET = "QUIET"
WARM = "WARM"
SPIKE = "SPIKE"


@dataclass(frozen=True)
class Params:
    window_s: int = 300
    baseline_windows: int = 6
    floor: int = 5
    spike_x: float = 3.0
    warm_x: float = 1.5
    cooldown_s: int = 1800
    baseline_min: float = 1.0


DEFAULTS = Params()


@dataclass(frozen=True)
class Verdict:
    state: str
    window: int            # start of the rated window
    rate: int
    baseline: float
    multiplier: float
    windows_observed: int
    cooldown_until: int
    baseline_kind: str | None = None   # 'own' | 'cohort' | None
    authors: int = 0
    known: int = 0
    first_known: str | None = None
    backed: int = 0            # accounts in this window that hold the coin
    money_usd: float = 0.0     # what those accounts hold, in dollars
    first_author: str | None = None
    first_post_ts: int | None = None

    @property
    def fired(self) -> bool:
        return self.state == SPIKE


def window_start(ts: int, window_s: int = 300) -> int:
    """Start of the window containing ts, aligned to the epoch."""
    return (ts // window_s) * window_s


def _ceil_window(ts: int, window_s: int) -> int:
    """First window boundary at or after ts."""
    return -(-ts // window_s) * window_s


def counts_by_window(post_times, window_s: int = 300) -> dict[int, int]:
    """Bucket post timestamps into aligned windows."""
    out: dict[int, int] = {}
    for ts in post_times:
        w = window_start(int(ts), window_s)
        out[w] = out.get(w, 0) + 1
    return out


def evaluate(
    *,
    counts: Mapping[int, int],
    now: int,
    watch_start: int,
    cooldown_until: int = 0,
    first_post: tuple[str | None, int] | None = None,
    authors: int = 0,
    known: int = 0,
    first_known: str | None = None,
    backed: int = 0,
    money_usd: float = 0.0,
    cohort_baseline: float | None = None,
    params: Params = DEFAULTS,
) -> Verdict:
    """Rate one token at one moment.

    counts          window start -> posts in that window
    now             evaluation time, unix seconds
    watch_start     when the token entered the watchlist
    cooldown_until  from the previous verdict, 0 if never fired
    first_post      (author, ts) of the earliest post in the rated window,
                    supplied by the caller because this module does not read
                    storage. Only ever surfaced on a SPIKE.
    authors         distinct accounts posting in the rated window. It never
                    gates a verdict - it is reported next to the rate so a
                    reader can tell forty voices from four accounts posting
                    ten times each. Counting it is free: we already store the
                    author of every post.
    known           how many of those accounts are on the public list, and
                    first_known which one posted first. Pure passthrough: this
                    function never reads either value. The list marks a row,
                    it does not decide one. See trace/accounts.py.
    backed          how many of those accounts hold the coin, and money_usd
                    what they hold between them. Passthrough for exactly the
                    same reason, and a sharper one: pump.fun pays people to
                    make callouts, so anything derived from that feed is a
                    number somebody can buy. It rides beside the verdict so a
                    reader can weigh it. It never moves one.
    """
    w = params.window_s
    rated = window_start(now, w) - w          # last complete window
    first_full = _ceil_window(watch_start, w)  # first window we saw whole
    oldest_needed = rated - params.baseline_windows * w

    observed = 0 if rated < first_full else (rated - first_full) // w + 1

    def warming():
        return Verdict(
            state=WARMING, window=rated, rate=counts.get(rated, 0),
            baseline=0.0, multiplier=0.0, windows_observed=observed,
            cooldown_until=cooldown_until, authors=authors,
            known=known, first_known=first_known,
            backed=backed, money_usd=money_usd)

    # The rated window itself must have been observed. Nothing replaces that.
    if rated < first_full:
        return warming()

    if oldest_needed >= first_full:
        total = sum(counts.get(oldest_needed + i * w, 0)
                    for i in range(params.baseline_windows))
        baseline, kind = total / params.baseline_windows, "own"
    elif cohort_baseline is not None:
        # The token is too young to have a past. Measured on live launches:
        # the first post lands two to four minutes after deployment and the
        # whole event is over inside fifteen, while its own baseline needs
        # thirty-five. So the reference becomes what a token *this old*
        # normally gets - one global curve, the same for every token, no
        # per-token tuning. See trace/cohort.py.
        baseline, kind = cohort_baseline, "cohort"
    else:
        return warming()

    rate = counts.get(rated, 0)
    multiplier = rate / max(baseline, params.baseline_min)

    loud_enough = multiplier >= params.spike_x and rate >= params.floor
    in_cooldown = now < cooldown_until

    if loud_enough and not in_cooldown:
        state = SPIKE
        cooldown_until = now + params.cooldown_s
    elif multiplier >= params.warm_x:
        # Accelerating, but either too thin to call or still cooling down.
        # The floor gates the loud verdict only: it never turns acceleration
        # back into silence, or 3.5x on seven posts would read QUIET while
        # 2.9x on thirty read WARM.
        state = WARM
    else:
        state = QUIET

    author, first_ts = (first_post or (None, None))
    return Verdict(
        state=state, window=rated, rate=rate, baseline=baseline,
        multiplier=multiplier, windows_observed=observed,
        cooldown_until=cooldown_until, authors=authors,
        known=known, first_known=first_known, baseline_kind=kind,
        backed=backed, money_usd=money_usd,
        first_author=author if state == SPIKE else None,
        first_post_ts=first_ts if state == SPIKE else None,
    )


# --- storage adapter --------------------------------------------------------
# Everything above is pure. Everything below is the thin layer that feeds it
# from SQLite, so both the live collector and the replay test drive the exact
# same code path.


def record_minute(conn: sqlite3.Connection, address: str, minute_ts: int,
                  n: int = 1) -> None:
    conn.execute(
        """INSERT INTO minute_counts (token_address, minute_ts, n) VALUES (?,?,?)
           ON CONFLICT(token_address, minute_ts)
           DO UPDATE SET n = minute_counts.n + excluded.n""",
        (address, (minute_ts // 60) * 60, n))


def rebuild_minutes(conn: sqlite3.Connection, address: str) -> None:
    """Recompute minute_counts for one token straight from posts."""
    conn.execute("DELETE FROM minute_counts WHERE token_address = ?", (address,))
    # matched IS NOT NULL is the whole of trace/attribution.py showing up in
    # the arithmetic: a post we paid for but could not tie to this token is
    # evidence, not attention.
    conn.execute(
        """INSERT INTO minute_counts (token_address, minute_ts, n)
           SELECT token_address, (created_at / 60) * 60, COUNT(*)
             FROM posts WHERE token_address = ? AND matched IS NOT NULL
            GROUP BY token_address, (created_at / 60) * 60""",
        (address,))


def counts_for(conn: sqlite3.Connection, address: str, since_ts: int,
               until_ts: int, window_s: int = 300) -> dict[int, int]:
    rows = conn.execute(
        """SELECT minute_ts, n FROM minute_counts
            WHERE token_address = ? AND minute_ts >= ? AND minute_ts < ?""",
        (address, since_ts, until_ts))
    out: dict[int, int] = {}
    for minute_ts, n in rows:
        wnd = window_start(minute_ts, window_s)
        out[wnd] = out.get(wnd, 0) + n
    return out


def first_post_in(conn: sqlite3.Connection, address: str, start_ts: int,
                  end_ts: int) -> tuple[str | None, int] | None:
    row = conn.execute(
        """SELECT author, created_at FROM posts
            WHERE token_address = ? AND created_at >= ? AND created_at < ?
              AND matched IS NOT NULL
            ORDER BY created_at ASC, post_id ASC LIMIT 1""",
        (address, start_ts, end_ts)).fetchone()
    return (row[0], row[1]) if row else None


def authors_in(conn: sqlite3.Connection, address: str, start_ts: int,
               end_ts: int) -> int:
    """Distinct accounts posting in one window.

    Free to compute - the author is already on every stored post - and it is
    the cheapest answer to the only real objection to counting posts: that a
    handful of accounts can manufacture a rate.
    """
    row = conn.execute(
        """SELECT COUNT(DISTINCT author) FROM posts
            WHERE token_address = ? AND created_at >= ? AND created_at < ?
              AND matched IS NOT NULL""",
        (address, start_ts, end_ts)).fetchone()
    return row[0] if row else 0


def tick(conn: sqlite3.Connection, address: str, now: int, watch_start: int,
         params: Params = DEFAULTS, persist: bool = True,
         listed: frozenset[str] | None = None,
         cohort_baseline: float | None = None) -> Verdict:
    """Evaluate one token against stored counts and record the result."""
    w = params.window_s
    rated = window_start(now, w) - w
    oldest = rated - params.baseline_windows * w
    counts = counts_for(conn, address, oldest, rated + w, w)

    row = conn.execute(
        "SELECT cooldown_until FROM token_state WHERE token_address = ?",
        (address,)).fetchone()
    cooldown_until = (row[0] or 0) if row else 0

    first = first_post_in(conn, address, rated, rated + w)
    n_known, who_known = _accounts.marked_in(
        conn, address, rated, rated + w, listed or frozenset())
    n_backed, money = _callouts.money_in_window(conn, address, rated, rated + w)
    v = evaluate(counts=counts, now=now, watch_start=watch_start,
                 cooldown_until=cooldown_until, first_post=first,
                 authors=authors_in(conn, address, rated, rated + w),
                 known=n_known, first_known=who_known,
                 backed=n_backed, money_usd=money,
                 cohort_baseline=cohort_baseline, params=params)
    if persist:
        conn.execute(
            """INSERT INTO token_state (token_address, state, rate, baseline,
                                        multiplier, first_author, first_post_ts,
                                        cooldown_until, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(token_address) DO UPDATE SET
                   state=excluded.state, rate=excluded.rate,
                   baseline=excluded.baseline, multiplier=excluded.multiplier,
                   first_author=COALESCE(excluded.first_author, token_state.first_author),
                   first_post_ts=COALESCE(excluded.first_post_ts, token_state.first_post_ts),
                   cooldown_until=excluded.cooldown_until,
                   updated_ts=excluded.updated_ts""",
            (address, v.state, v.rate, v.baseline, v.multiplier,
             v.first_author, v.first_post_ts, v.cooldown_until, now))
    return v


def replay(conn: sqlite3.Connection, address: str, start_ts: int, end_ts: int,
           watch_start: int, params: Params = DEFAULTS,
           persist: bool = False,
           listed: frozenset[str] | None = None,
           curve=None, launch_ts: int | None = None) -> list[Verdict]:
    """Walk a recorded stretch one window at a time.

    Deterministic and offline. This is the function the replay test drives,
    and it is the same code the live loop runs, one window at a time.
    """
    w = params.window_s
    out: list[Verdict] = []
    cooldown_until = 0
    t = window_start(start_ts, w) + w
    while t <= end_ts:
        rated = window_start(t, w) - w
        oldest = rated - params.baseline_windows * w
        counts = counts_for(conn, address, oldest, rated + w, w)
        first = first_post_in(conn, address, rated, rated + w)
        n_known, who_known = _accounts.marked_in(
            conn, address, rated, rated + w, listed or frozenset())
        n_backed, money = _callouts.money_in_window(conn, address, rated, rated + w)
        cb = None
        if curve is not None and launch_ts is not None:
            cb = curve.baseline_for(rated - launch_ts)
        v = evaluate(counts=counts, now=t, watch_start=watch_start,
                     cooldown_until=cooldown_until, first_post=first,
                     authors=authors_in(conn, address, rated, rated + w),
                     known=n_known, first_known=who_known,
                     backed=n_backed, money_usd=money,
                     cohort_baseline=cb, params=params)
        cooldown_until = v.cooldown_until
        out.append(v)
        t += w
    if persist and out:
        last = out[-1]
        conn.execute(
            """INSERT INTO token_state (token_address, state, rate, baseline,
                                        multiplier, first_author, first_post_ts,
                                        cooldown_until, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(token_address) DO UPDATE SET
                   state=excluded.state, rate=excluded.rate,
                   baseline=excluded.baseline, multiplier=excluded.multiplier,
                   cooldown_until=excluded.cooldown_until,
                   updated_ts=excluded.updated_ts""",
            (address, last.state, last.rate, last.baseline, last.multiplier,
             last.first_author, last.first_post_ts, last.cooldown_until, end_ts))
    return out


def main(argv: list[str] | None = None) -> int:
    """python -m trace.detector - print the current verdict per watched token."""
    import argparse
    import time
    from . import config as _config   # imported here so tests never need TOML
    from . import db as _db

    ap = argparse.ArgumentParser(description="TRACE detector")
    ap.add_argument("--config", default=None)
    ap.add_argument("--now", type=int, default=None, help="override the clock")
    args = ap.parse_args(argv)

    listed = _accounts.load()
    cfg = _config.load(args.config)
    conn = _db.connect(cfg.db_file)
    now = args.now if args.now is not None else int(time.time())

    rows = _db.active_watchlist(conn)
    if not rows:
        print("watchlist is empty - run python -m trace.watchlist first")
        return 1

    print(f"{'ticker':<12} {'state':<8} {'rate':>5} {'acct':>5} {'base':>7} "
          f"{'mult':>7}  first post")
    print("-" * 78)
    for r in rows:
        v = tick(conn, r["address"], now, r["added_ts"], listed=listed)
        first = ""
        if v.first_known:
            first = f"@{v.first_known} ON THE LIST · "
        if v.first_author:
            mins = (now - (v.first_post_ts or now)) // 60
            first += f"@{v.first_author}, {mins} min ago"
        sym = (r["symbol"] or r["address"][:10])[:12]
        print(f"{sym:<12} {v.state:<8} {v.rate:>5} {v.authors:>5} "
              f"{v.baseline:>7.2f} {v.multiplier:>6.1f}x  {first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
