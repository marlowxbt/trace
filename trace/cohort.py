"""What "normal" looks like at a given age.

The original rule was: compare a token against its own baseline. Live data
killed it. On this chain a token's first post lands two to four minutes after
its contract is deployed, and the whole event is over inside fifteen - while
the detector needs thirty-five minutes of that token's own history before it
may speak. Measured, on real launches:

    HODLster   first post 159s after launch, burst 13/11/14 in three windows
    ROBOSHARE  140s        AIP 145s        Fork 253s        TRADE 807s

There is nothing to backfill. Those windows are not missing data, the token did
not exist. **A three-minute-old memecoin has no own normal**, and a detector
that demands one is asking for a biography from something that dies before it
can have one.

So the reference changes, and only the reference. Instead of

    this token, against what this token usually gets

it is

    this token, against what a token *this old* usually gets

Everything else survives intact: it is still a ratio, still counts against a
measured denominator, still no prediction, still one rule for every token and
no hand-tuning. The cohort is global - every token is measured against the same
curve - so nothing here can be aimed at a particular coin.

The curve is built from the posts already collected, using exactly the history
rule the detector uses: a window counts only if the whole of it was observed,
and only from the token's launch onward. A bucket with too few windows behind
it reports nothing rather than a number nobody should trust.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Age buckets in seconds, widening as tokens get older, because the interesting
# variation is all in the first hour.
BUCKETS = (
    (0, 300),          # the first window of life
    (300, 600),
    (600, 1200),
    (1200, 2400),
    (2400, 4800),
    (4800, 9600),
    (9600, 1 << 31),
)

# A bucket needs this many observed windows behind it before it is willing to
# say what normal is. Below it, the honest answer is "not measured yet".
MIN_WINDOWS = 12


def bucket_for(age_s: int) -> tuple[int, int] | None:
    if age_s < 0:
        return None
    for b in BUCKETS:
        if b[0] <= age_s < b[1]:
            return b
    return None


def label(b: tuple[int, int]) -> str:
    lo, hi = b[0] // 60, b[1] // 60
    return f"{lo}-{hi}m" if hi < 100000 else f"{lo}m+"


@dataclass(frozen=True)
class Curve:
    """posts-per-window by age bucket, with how much evidence is behind each."""
    posts: dict[tuple[int, int], int]
    windows: dict[tuple[int, int], int]

    def mean(self, b) -> float | None:
        n = self.windows.get(b, 0)
        if n < MIN_WINDOWS:
            return None
        return self.posts.get(b, 0) / n

    def baseline_for(self, age_s: int) -> float | None:
        b = bucket_for(age_s)
        return self.mean(b) if b else None

    @property
    def total_windows(self) -> int:
        return sum(self.windows.values())


def build(conn: sqlite3.Connection, window_s: int = 300) -> Curve:
    """Measure the curve from stored posts.

    Only observed windows count, and only from launch: `cursors.observed_from`
    is where our knowledge of a token starts and `tokens.launch_ts` is where
    the token starts, and a window before either of those is not a quiet window,
    it is not a window.
    """
    posts: dict[tuple[int, int], int] = {}
    windows: dict[tuple[int, int], int] = {}

    rows = conn.execute(
        """SELECT t.address, t.launch_ts, c.observed_from, c.updated_ts
             FROM tokens t JOIN cursors c ON c.token_address = t.address
            WHERE t.launch_ts IS NOT NULL AND c.observed_from IS NOT NULL""")
    for address, launch_ts, obs_from, last_poll in rows.fetchall():
        start = max(launch_ts, obs_from)
        end = last_poll or obs_from
        first = -(-start // window_s) * window_s          # first whole window
        counts = dict(conn.execute(
            """SELECT (created_at / ?) * ?, COUNT(*) FROM posts
                WHERE token_address = ? AND matched IS NOT NULL
                GROUP BY (created_at / ?) * ?""",
            (window_s, window_s, address, window_s, window_s)).fetchall())
        w = first
        while w + window_s <= end:
            b = bucket_for(w - launch_ts)
            if b:
                windows[b] = windows.get(b, 0) + 1
                posts[b] = posts.get(b, 0) + counts.get(w, 0)
            w += window_s
    return Curve(posts=posts, windows=windows)


def rows(curve: Curve) -> list[dict]:
    """The curve as a table, oldest bucket last, every bucket present.

    A bucket with too little behind it is in here with a null mean rather than
    missing, because "we looked and there is not enough yet" and "we never
    looked" are different answers and the table has to be able to say both.
    """
    return [{"label": label(b),
             "lo_min": b[0] // 60,
             "hi_min": None if b[1] > 100000 else b[1] // 60,
             "windows": curve.windows.get(b, 0),
             "posts": curve.posts.get(b, 0),
             "normal": curve.mean(b)} for b in BUCKETS]


def main(argv=None) -> int:
    """`python -m trace.cohort` - what a token this age normally gets.

    README, the Makefile and docs/ATTENTION.md have all told people to run this
    since the first commit, and until now it printed nothing at all: the module
    was a library with no way in. This is that way in.
    """
    import argparse
    import json

    from . import config as _config
    from . import db as _db

    ap = argparse.ArgumentParser(
        description="the attention-by-age curve, rebuilt from the database")
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = _config.load(args.config)
    conn = _db.connect(cfg.db_file)
    curve = build(conn, cfg.detector.rate_window_min * 60)
    table = rows(curve)

    if args.json:
        print(json.dumps(table))
        return 0

    if not curve.total_windows:
        print("No observed windows in %s yet, so there is no curve to draw.\n"
              "Run the collector for a while first:\n"
              "    python3 -m trace.collector" % cfg.db_file)
        return 0

    print("%d observed windows across %d buckets. A bucket says nothing until "
          "%d windows\nare behind it - 'not measured' is an answer, and a "
          "guess is not.\n" % (curve.total_windows,
                               sum(1 for r in table if r["windows"]),
                               MIN_WINDOWS))
    print(f"{'age':>10}{'normal':>14}{'windows':>10}{'posts':>8}")
    for r in table:
        normal = ("%.2f" % r["normal"]) if r["normal"] is not None \
            else ("not measured" if r["windows"] else "-")
        print(f"{r['label']:>10}{normal:>14}{r['windows']:>10}{r['posts']:>8}")

    seen = [r for r in table if r["normal"] is not None]
    if len(seen) >= 2:
        hi, lo = max(seen, key=lambda r: r["normal"]), min(seen, key=lambda r: r["normal"])
        if lo["normal"] > 0:
            print("\nLoudest bucket %s at %.2f, quietest %s at %.2f - "
                  "a %.0f-fold fall." % (hi["label"], hi["normal"],
                                         lo["label"], lo["normal"],
                                         hi["normal"] / lo["normal"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
