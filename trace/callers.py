"""Which accounts keep showing up, and how early.

The project always said `accounts.txt` would carry a hand-kept list of callers,
that a name on it would put a red edge on a row, and that it would never change
a verdict. This is the same idea with the hand taken out of it: the list is
computed from what the collector already measured, so nobody has to be trusted
to keep it honest and nobody can buy their way onto it.

Three things are measured per account, and all three are counts:

    covered   how many of the watched tokens it posted about, counted posts only
    first_at  minutes after launch of its earliest counted post
    led       tokens where it was among the first three counted voices

`covered >= 2` is what earns the red edge. It is a deliberately dull rule: an
account that turns up on two different launches on the same chain in one day is
either a caller or a bot, and either way it is worth seeing marked. It says
nothing about whether the call was good, and this module has no opinion about
that - there is no follower count here, no engagement, no score.

The hard rule from the detector still holds and is enforced by test: a marked
account changes nothing. `detector.evaluate` never sees this file.

Run: python -m trace.callers            # ranked table
     python -m trace.callers --json     # same thing for the exporter
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict

from . import config as _config
from . import db as _db

LISTED_MIN_TOKENS = 2       # showed up on this many watched tokens
LED_RANK = 3                # "among the first three voices" on a token
EARLY_MIN = 5               # a first post inside this many minutes is a first voice


def why(row: dict) -> str:
    """One line saying what put this account on the list.

    A mark a reader cannot interrogate is decoration. Every clause here is a
    count taken from the same table the verdict came from, so the sentence and
    the row cannot drift apart.
    """
    bits = ["on %d tokens" % row["covered"]]
    if row["led"]:
        bits.append("first voice on %d" % row["led"])
    if row["first_at"] is not None:
        bits.append("earliest +%dm" % row["first_at"])
    return " \u00b7 ".join(bits)


def build(conn) -> list[dict]:
    tok = {r["address"].lower(): (r["symbol"], r["launch_ts"])
           for r in conn.execute("SELECT address, symbol, launch_ts FROM tokens")}

    # who spoke first on each token, so "led" can be counted
    lead = defaultdict(set)
    for addr, (sym, _) in tok.items():
        rows = conn.execute(
            """SELECT DISTINCT author FROM posts
                WHERE token_address = ? AND matched IS NOT NULL AND author IS NOT NULL
                ORDER BY created_at LIMIT ?""", (addr, LED_RANK)).fetchall()
        for r in rows:
            lead[r["author"]].add(sym)

    acc = defaultdict(lambda: {"toks": set(), "ages": [], "posts": 0, "name": None,
                               "avatar": None})
    for r in conn.execute(
            """SELECT p.author, p.token_address, p.created_at,
                      a.name, a.avatar
                 FROM posts p
            LEFT JOIN authors a ON a.handle = p.author
                WHERE p.matched IS NOT NULL AND p.author IS NOT NULL"""):
        sym, launch = tok.get(r["token_address"].lower(), (None, None))
        if sym is None:
            continue
        v = acc[r["author"]]
        v["toks"].add(sym)
        v["posts"] += 1
        v["name"] = v["name"] or r["name"]
        v["avatar"] = v["avatar"] or r["avatar"]
        if launch:
            v["ages"].append(max(0, (r["created_at"] - launch) // 60))

    out = []
    for handle, v in acc.items():
        ages = sorted(v["ages"])
        out.append({
            "handle": handle,
            "name": v["name"] or handle,
            "avatar": v["avatar"],
            "covered": len(v["toks"]),
            "tokens": sorted(v["toks"]),
            "posts": v["posts"],
            "first_at": ages[0] if ages else None,
            "median_at": int(statistics.median(ages)) if ages else None,
            "led": len(lead.get(handle, ())),
            "listed": len(v["toks"]) >= LISTED_MIN_TOKENS,
            "early": bool(ages) and ages[0] <= EARLY_MIN,
        })
        out[-1]["why"] = why(out[-1])
    # repeat callers first, then whoever led most, then whoever arrives earliest
    out.sort(key=lambda a: (-a["covered"], -a["led"],
                            a["first_at"] if a["first_at"] is not None else 10 ** 6))
    return out


RULES = {
    "min_tokens": LISTED_MIN_TOKENS,
    "led_rank": LED_RANK,
    "early_min": EARLY_MIN,
    "sentence": ("An account is on the list once it has posted a counted post "
                 "about %d different watched tokens. Nothing else puts it there: "
                 "no follower count, no engagement, no submission, no payment. "
                 "Being on it marks a row and changes no verdict."
                 % LISTED_MIN_TOKENS),
}


def index(rows: list[dict]) -> dict:
    """handle -> the listed row, for a page that paints by author.

    Only listed accounts are in here. The caller list is a layer over the feed,
    never part of a post: the post record the page renders is the same object
    whether or not this map has a key for it.
    """
    return {r["handle"]: r for r in rows if r["listed"]}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="who keeps showing up, and how early")
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args(argv)

    conn = _db.connect(_config.load(args.config).db_file)
    rows = build(conn)
    if args.json:
        print(json.dumps(rows))
        return 0

    if not rows:
        print("No counted posts in %s yet, so nobody is on the list.\n"
              "Run the collector for a while first:\n"
              "    python3 -m trace.collector" % _config.load(args.config).db_file)
        return 0

    listed = [r for r in rows if r["listed"]]
    print(f"{len(rows)} accounts counted, {len(listed)} on more than one token\n")
    print(f"{'':2}{'account':22}{'tokens':>7}{'led':>5}{'first':>7}{'median':>8}"
          f"{'posts':>7}  what they showed up on")
    for r in rows[:args.limit]:
        print(f"{'*' if r['listed'] else ' ':2}"
              f"@{r['handle']:21}{r['covered']:>7}{r['led']:>5}"
              f"{('+%dm' % r['first_at']) if r['first_at'] is not None else '-':>7}"
              f"{('+%dm' % r['median_at']) if r['median_at'] is not None else '-':>8}"
              f"{r['posts']:>7}  {', '.join(r['tokens'])}")
    print("\n* = on the list. It marks a row and nothing else: the detector never "
          "reads this file.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
