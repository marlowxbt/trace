"""A public snapshot of what the collector has measured.

The site has been showing invented rows with an `example` chip on them since
the day it was built, because there was nothing real to show. There is now.

What this exports is deliberately narrow, and the narrowness is the product's
own rules showing up as code:

- **Public posts, opt-in, with a link back.** `--with-posts` adds a sample of
  the posts the collector actually counted: the handle, the text, the time and
  the reason it counted, plus the canonical x.com URL so any claim on the site
  can be checked against the original. These are public posts by accounts that
  published them publicly; nothing private is in this database to add. Without
  the flag the snapshot stays counts-only, which is what a hosted API should
  serve.
- **No wallet anything.** There is none in this database to leak: the wallet
  half runs in the visitor's browser and never touches a server. This file
  could not violate that constraint if it tried, and that is the point of
  having built it that way.
- **Byte-identical for every visitor.** The snapshot is one public series, the
  same for everyone, carrying no timestamp about anybody's trading. That is
  hard constraint 5, and it is what makes it safe to bake straight into a page.

Run: python -m trace.export > public.json
"""

from __future__ import annotations

import json
import re
import sys
import time

from . import attribution as _attribution
from . import cohort as _cohort
from . import config as _config
from . import db as _db
from . import detector as _detector


# --- the posts sample -------------------------------------------------------

_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_B58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}(?:pump|bonk)?\b")

# Tickers on this chain that are also the ticker of a listed security or a
# token on another chain. The collector does not need this list -- attribution
# already refuses those posts -- but the site should be able to say WHY a post
# it paid for did not count, in words, and that needs a name.
COLLISIONS = {
    "TER": "Teradyne, NASDAQ",
    "STAG": "STAG Industrial, NYSE",
    "AIP": "Arteris, NYSE",
    "TRADE": "an unrelated token",
    "FORK": "a pump.fun token on Solana",
    "ROB": "an unrelated token",
}


def why_not(text, address, symbol, created_at, launch_ts):
    """Say, in words, why a paid post was not counted.

    Mirrors trace.attribution.match; it never overrides it. Anything this
    cannot explain comes back as the honest default rather than a guess.
    """
    if created_at and launch_ts and created_at < launch_ts:
        return "posted before the token existed"
    others = [a for a in _ADDR.findall(text or "")
              if a.lower() != (address or "").lower()]
    if others:
        return "carries a different contract address"
    if _B58.search(text or "") and "pump" in (text or "").lower():
        return "a pump.fun address, so another chain"
    if _attribution.cashtag_in(text or "", symbol):
        who = COLLISIONS.get((symbol or "").upper())
        return (f"$" + symbol.upper() + " here is " + who) if who else \
            "the ticker with nothing tying it to this chain"
    return "never names the token"


def posts_sample(conn, symbols, counted_limit=40, rejected_limit=12):
    tok = {r["symbol"]: r for r in conn.execute(
        "SELECT address, symbol, launch_ts FROM tokens")}
    counted, rejected = [], []
    for sym in symbols:
        t = tok.get(sym)
        if t is None:
            continue
        for r in conn.execute(
                """SELECT post_id, author, created_at, text, matched
                     FROM posts WHERE token_address = ? AND matched IS NOT NULL
                    ORDER BY created_at LIMIT ?""", (t["address"], counted_limit)):
            counted.append({
                "symbol": sym,
                "author": r["author"],
                "post_id": r["post_id"],
                "ts": r["created_at"],
                "age_min": max(0, (r["created_at"] - t["launch_ts"]) // 60),
                "matched": r["matched"],
                "text": r["text"],
                "url": f"https://x.com/{r['author']}/status/{r['post_id']}",
            })
    # Rank the vetoes by how much they explain. "Posted before the token
    # existed" is true but dull -- every backfill is full of them. A post
    # carrying somebody else's contract address under our ticker is the whole
    # reason attribution exists, so it goes first.
    def rank(reason):
        if "is " in reason and "," in reason:      # a named ticker collision
            return 0
        if "different contract" in reason:
            return 1
        if "another chain" in reason:
            return 2
        if "nothing tying it" in reason:
            return 3
        return 4                                   # before launch

    pool = []
    for r in conn.execute(
            """SELECT p.post_id, p.author, p.created_at, p.text, p.token_address
                 FROM posts p WHERE p.matched IS NULL"""):
        t = next((c for c in tok.values()
                  if c["address"].lower() == r["token_address"].lower()), None)
        if t is None:
            continue
        reason = why_not(r["text"], t["address"], t["symbol"],
                         r["created_at"], t["launch_ts"])
        if reason == "never names the token":
            continue
        pool.append((rank(reason), t["symbol"], {
            "symbol": t["symbol"], "author": r["author"],
            "post_id": r["post_id"], "ts": r["created_at"],
            "reason": reason, "text": r["text"],
            "url": f"https://x.com/{r['author']}/status/{r['post_id']}"}))

    pool.sort(key=lambda x: (x[0], x[2]["ts"]))
    seen = set()
    for _, sym, row in pool:                        # one per token, best first
        if sym in seen:
            continue
        seen.add(sym)
        rejected.append(row)
        if len(rejected) >= rejected_limit:
            break
    return {"counted": counted, "rejected": rejected}


def snapshot(conn, cfg) -> dict:
    w = cfg.detector.rate_window_min * 60
    curve = _cohort.build(conn, w)
    rows = list(conn.execute(
        """SELECT t.address, t.symbol, t.launch_ts, c.observed_from, c.updated_ts
             FROM tokens t JOIN cursors c ON c.token_address = t.address
            WHERE c.observed_from IS NOT NULL
            ORDER BY t.launch_ts"""))

    params = _detector.Params(
        window_s=w, baseline_windows=cfg.detector.baseline_windows,
        floor=cfg.detector.floor, spike_x=cfg.detector.spike_multiplier,
        warm_x=cfg.detector.warm_multiplier,
        cooldown_s=cfg.detector.cooldown_min * 60,
        baseline_min=cfg.detector.min_baseline)

    tokens, events = [], []
    for r in rows:
        addr = r["address"]
        paid, counted, accounts = conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN matched IS NOT NULL THEN 1 ELSE 0 END),
                      COUNT(DISTINCT CASE WHEN matched IS NOT NULL
                                          THEN author END)
                 FROM posts WHERE token_address = ?""", (addr,)).fetchone()
        horizon = max(r["launch_ts"] or 0, r["observed_from"])
        verdicts = _detector.replay(conn, addr, horizon, r["updated_ts"],
                                    watch_start=horizon, params=params,
                                    curve=curve, launch_ts=r["launch_ts"])
        peak = max(verdicts, key=lambda v: (v.rate, v.multiplier), default=None)
        last = verdicts[-1] if verdicts else None
        fired = [v for v in verdicts if v.fired]
        tokens.append({
            "symbol": r["symbol"],
            "address": addr,
            "launch_ts": r["launch_ts"],
            "paid": paid or 0,
            "counted": counted or 0,
            "accounts": accounts or 0,
            "peak_rate": peak.rate if peak else 0,
            "peak_multiplier": round(peak.multiplier, 1) if peak else 0.0,
            "state": (peak.state if peak else _detector.WARMING),
            "spikes": len(fired),
            # where it stands at the last window we rated
            "now": {
                "window": last.window if last else 0,
                "rate": last.rate if last else 0,
                "accounts": last.authors if last else 0,
                "baseline": round(last.baseline, 2) if last else 0.0,
                "baseline_kind": last.baseline_kind if last else None,
                "multiplier": round(last.multiplier, 1) if last else 0.0,
                "state": (last.state if last else _detector.WARMING),
            },
            "last_post_ts": conn.execute(
                "SELECT MAX(created_at) FROM posts WHERE token_address = ?"
                "   AND matched IS NOT NULL", (addr,)).fetchone()[0] or 0,
        })
        if fired:
            v0 = fired[0]
            events.append({
                "symbol": r["symbol"],
                "age_min": (v0.window - (r["launch_ts"] or v0.window)) // 60,
                "rate": v0.rate,
                "baseline": round(v0.baseline, 2),
                "baseline_kind": v0.baseline_kind,
                "multiplier": round(v0.multiplier, 1),
                "accounts": v0.authors,
                # the whole run, so a reader can see the shape and not just the
                # peak - counts only, never a word anybody wrote
                "windows": [
                    {"age_min": (v.window - (r["launch_ts"] or v.window)) // 60,
                     "rate": v.rate, "baseline": round(v.baseline, 2),
                     "kind": v.baseline_kind, "accounts": v.authors,
                     "multiplier": round(v.multiplier, 1), "state": v.state}
                    for v in verdicts if v.rate or v.fired],
            })

    spend, reqs = conn.execute(
        "SELECT COALESCE(SUM(est_cost_usd),0), COUNT(*) FROM reads").fetchone()
    return {
        "generated_at": int(time.time()),
        "provider": cfg.x.provider,
        "rules": {
            "window_s": w,
            "baseline_windows": cfg.detector.baseline_windows,
            "floor": cfg.detector.floor,
            "spike_x": cfg.detector.spike_multiplier,
            "warm_x": cfg.detector.warm_multiplier,
            "cooldown_s": cfg.detector.cooldown_min * 60,
        },
        "cohort": [
            {"label": _cohort.label(b),
             "lo_min": b[0] // 60,
             "windows": curve.windows.get(b, 0),
             "posts": curve.posts.get(b, 0),
             "normal": (None if curve.mean(b) is None else round(curve.mean(b), 2))}
            for b in _cohort.BUCKETS],
        "cohort_windows": curve.total_windows,
        "cohort_min_windows": _cohort.MIN_WINDOWS,
        "tokens": tokens,
        "events": events,
        "totals": {
            "paid": sum(t["paid"] for t in tokens),
            "counted": sum(t["counted"] for t in tokens),
            "requests": reqs,
            "spend_usd": round(spend, 4),
        },
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="public JSON snapshot")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--with-posts", action="store_true",
                    help="include real counted posts (handle, text, link)")
    ap.add_argument("--avatars", default=None,
                    help="avatars.json from trace.avatars, merged in")
    args = ap.parse_args(argv)
    cfg = _config.load(args.config)
    conn = _db.connect(cfg.db_file)
    data = snapshot(conn, cfg)
    if args.with_posts:
        syms = [e["symbol"] for e in data["events"]] or \
            [t["symbol"] for t in data["tokens"]][:2]
        data["posts"] = posts_sample(conn, syms)
        if args.avatars:
            with open(args.avatars) as fh:
                av = json.load(fh)
            data["avatars"] = av
            data["posts"]["avatars_missing"] = sorted(
                {p["author"] for p in data["posts"]["counted"]
                 + data["posts"]["rejected"]} - set(av))
    text = json.dumps(data, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out}: {len(text)} bytes, "
              f"{len(data['tokens'])} tokens, {len(data['events'])} events",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
