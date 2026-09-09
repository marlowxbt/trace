"""Stage 1 - build the list of tokens worth paying X to watch.

The chain launches 17-35k tokens a day, so "the most recent N launches" is not
a watchlist: it turns over completely in under a minute and no token ever
survives long enough to accumulate the 20 minutes of history the detector
needs before it can fire. Selection is by traction instead - who is actually
trading the thing - with hysteresis so a token already collecting history is
not evicted over noise.

    python -m trace.watchlist            # refresh and print
    python -m trace.watchlist --dry-run  # print what would change, write nothing
    python -m trace.watchlist --json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Iterable, Sequence

from . import chain, db
from .config import Config, load
from .rpc import Rpc


@dataclass
class Candidate:
    address: str
    launch_block: int
    launch_ts: int | None = None
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    related: tuple[str, ...] = ()
    holders: int = 0
    transfers: int = 0
    source: str = "auto"          # 'auto' | 'pin'
    incumbent: bool = False
    added_ts: int | None = None

    @property
    def score(self) -> tuple[int, int]:
        return (self.holders, self.transfers)

    def age_s(self, now_ts: int) -> int | None:
        return None if self.launch_ts is None else max(now_ts - self.launch_ts, 0)


@dataclass
class Decision:
    address: str
    action: str      # 'keep' | 'add' | 'remove' | 'skip'
    reason: str


@dataclass
class Selection:
    watched: list[Candidate] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)

    def by_action(self, action: str) -> list[Decision]:
        return [d for d in self.decisions if d.action == action]


def ticker_ok(symbol: str | None, cfg: Config) -> bool:
    """A ticker we cannot put in an X query is a ticker we will not pay for.
    The query is ("0xADDRESS" OR "$TICKER"), so spaces and punctuation are out."""
    if not symbol:
        return False
    w = cfg.watchlist
    return bool(re.fullmatch(rf"[A-Za-z0-9_]{{{w.ticker_min_len},{w.ticker_max_len}}}", symbol))


def select(candidates: Sequence[Candidate], cfg: Config, now_ts: int) -> Selection:
    """Pure. No network, no database - this is the part worth testing."""
    w = cfg.watchlist
    sel = Selection()
    taken_tickers: set[str] = set()

    def claim(c: Candidate, action: str, reason: str) -> None:
        sel.watched.append(c)
        sel.decisions.append(Decision(c.address, action, reason))
        if c.symbol:
            taken_tickers.add(c.symbol.upper())

    # 1. pins first. They are deliberate, so traction does not gate them - but
    #    they consume cap slots, because the X bill does not care why a token
    #    is on the list.
    for c in [c for c in candidates if c.source == "pin"]:
        if len(sel.watched) < w.max_tokens:
            claim(c, "keep" if c.incumbent else "add", "pinned")
        else:
            sel.decisions.append(Decision(c.address, "skip", "over max_tokens"))

    # 2. incumbents, strongest first, on the looser stay-thresholds.
    incumbents = sorted((c for c in candidates if c.incumbent and c.source != "pin"),
                        key=lambda c: c.score, reverse=True)
    kept: list[Candidate] = []
    for c in incumbents:
        age = c.age_s(now_ts)
        if age is not None and age > w.max_age_hours * 3600:
            sel.decisions.append(Decision(c.address, "remove", f"older than {w.max_age_hours}h"))
        elif c.holders < w.stay_min_holders or c.transfers < w.stay_min_transfers:
            sel.decisions.append(Decision(
                c.address, "remove",
                f"went quiet ({c.holders}h/{c.transfers}t < {w.stay_min_holders}h/{w.stay_min_transfers}t)"))
        elif len(sel.watched) >= w.max_tokens:
            sel.decisions.append(Decision(c.address, "remove", "over max_tokens"))
        else:
            claim(c, "keep", "still trading")
            kept.append(c)

    # 3. new candidates on the stricter enter-thresholds.
    fresh = sorted((c for c in candidates if not c.incumbent and c.source != "pin"),
                   key=lambda c: c.score, reverse=True)
    queue: list[Candidate] = []
    for c in fresh:
        age = c.age_s(now_ts)
        if age is not None and age < w.min_age_min * 60:
            sel.decisions.append(Decision(c.address, "skip", "too young to score"))
        elif not ticker_ok(c.symbol, cfg):
            sel.decisions.append(Decision(c.address, "skip", f"unusable ticker {c.symbol!r}"))
        elif c.holders < w.enter_min_holders or c.transfers < w.enter_min_transfers:
            sel.decisions.append(Decision(c.address, "skip", "below entry traction"))
        elif c.symbol.upper() in taken_tickers:
            # Two tokens sharing a ticker cannot be told apart in an X query.
            # The weaker one would just pollute the stronger one's count.
            sel.decisions.append(Decision(c.address, "skip", f"ticker ${c.symbol} already watched"))
            taken_tickers.add(c.symbol.upper())
        else:
            queue.append(c)
            taken_tickers.add(c.symbol.upper())

    # 4. fill free slots, then let a much stronger candidate displace the
    #    weakest incumbent that is itself below entry level.
    for c in queue:
        if len(sel.watched) < w.max_tokens:
            claim(c, "add", f"{c.holders} holders in the traction window")
            continue
        weakest = min((k for k in kept), key=lambda k: k.score, default=None)
        if weakest is None:
            sel.decisions.append(Decision(c.address, "skip", "list full"))
            continue
        below_entry = (weakest.holders < w.enter_min_holders
                       or weakest.transfers < w.enter_min_transfers)
        if below_entry and c.holders >= weakest.holders * w.displace_factor:
            sel.watched.remove(weakest)
            kept.remove(weakest)
            sel.decisions = [d for d in sel.decisions
                             if not (d.address == weakest.address and d.action == "keep")]
            sel.decisions.append(Decision(weakest.address, "remove",
                                          f"displaced by ${c.symbol}"))
            claim(c, "add", f"displaced ${weakest.symbol} ({c.holders} vs {weakest.holders} holders)")
        else:
            sel.decisions.append(Decision(c.address, "skip", "list full"))

    return sel


# -- I/O side ---------------------------------------------------------------

def refresh(cfg: Config, conn: sqlite3.Connection, rpc: Rpc,
            dry_run: bool = False) -> tuple[Selection, dict]:
    w = cfg.watchlist
    head = rpc.block_number()
    now_ts = rpc.block_timestamp(head)

    from_block = rpc.find_block_at_timestamp(now_ts - w.discovery_window_min * 60, head=head)
    to_block = rpc.find_block_at_timestamp(now_ts - w.min_age_min * 60, head=head)
    launches = chain.scan_launches(rpc, cfg.chain.factory, from_block, to_block,
                                   cfg.chain.log_page_blocks)

    rows = db.active_watchlist(conn)
    incumbents = {
        r["address"]: Candidate(
            address=r["address"], launch_block=r["launch_block"], launch_ts=r["launch_ts"],
            symbol=r["symbol"], name=r["name"],
            related=tuple(filter(None, (r["related"] or "").split(","))),
            source=r["source"], incumbent=True, added_ts=r["added_ts"])
        for r in rows
    }
    pinned = {a.lower() for a in w.pinned_tokens}

    cands: dict[str, Candidate] = dict(incumbents)
    for l in launches:
        if l.token not in cands:
            cands[l.token] = Candidate(address=l.token, launch_block=l.block,
                                       related=l.related)
    for p in pinned:
        cands.setdefault(p, Candidate(address=p, launch_block=0))
        cands[p].source = "pin"

    # traction over the same window for everyone, incumbents included
    traction_from = rpc.find_block_at_timestamp(now_ts - w.traction_window_min * 60, head=head)
    ignore = {a: set(c.related) for a, c in cands.items()}
    tr = chain.traction(rpc, list(cands), traction_from, head, ignore=ignore,
                        page_blocks=cfg.chain.log_page_blocks)
    for a, t in tr.items():
        cands[a].holders, cands[a].transfers = t.holders, t.transfers

    # metadata only for the plausible ones - a name() call on every launch in
    # the window would be thousands of calls for nothing.
    need_meta = [a for a, c in cands.items()
                 if c.symbol is None and (c.source == "pin" or c.holders >= w.stay_min_holders)]
    for a, m in chain.token_meta(rpc, need_meta).items():
        cands[a].symbol, cands[a].name, cands[a].decimals = m.symbol, m.name, m.decimals

    # exact launch timestamps, only for what could plausibly be selected
    need_ts = [a for a, c in cands.items()
               if c.launch_ts is None and c.launch_block and c.holders >= w.stay_min_holders]
    if need_ts:
        stamps = rpc.block_timestamps([cands[a].launch_block for a in need_ts])
        for a in need_ts:
            cands[a].launch_ts = stamps.get(cands[a].launch_block)

    sel = select(list(cands.values()), cfg, now_ts)

    stats = {
        "head": head, "now_ts": now_ts,
        "scanned_launches": len(launches),
        "candidates": len(cands),
        "met_entry": sum(1 for c in cands.values()
                         if c.holders >= w.enter_min_holders
                         and c.transfers >= w.enter_min_transfers),
        "rpc_calls": rpc.calls,
    }

    if not dry_run:
        for c in sel.watched:
            db.upsert_token(conn, address=c.address, symbol=c.symbol, name=c.name,
                            decimals=c.decimals, launch_block=c.launch_block,
                            launch_ts=c.launch_ts, related=c.related, now_ts=now_ts)
        for d in sel.decisions:
            c = cands[d.address]
            if d.action == "add":
                db.add_to_watchlist(conn, d.address, c.source, now_ts, d.reason,
                                    c.holders, c.transfers)
            elif d.action == "keep":
                db.update_score(conn, d.address, c.holders, c.transfers, now_ts)
            elif d.action == "remove":
                db.remove_from_watchlist(conn, d.address, now_ts, d.reason)

    return sel, stats


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}"


def render(sel: Selection, stats: dict, cfg: Config) -> str:
    now_ts = stats["now_ts"]
    lines = [
        f"TRACE watchlist   {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(now_ts))}"
        f"   head {stats['head']:,}",
        "",
        f"  {'TICKER':<16}{'AGE':>7}{'HOLDERS':>9}{'TRANSF':>8}   {'ADDRESS':<44}{'SRC':<5}",
    ]
    for c in sorted(sel.watched, key=lambda c: c.score, reverse=True):
        lines.append(f"  {(c.symbol or '?')[:15]:<16}{fmt_age(c.age_s(now_ts)):>7}"
                     f"{c.holders:>9}{c.transfers:>8}   {c.address:<44}{c.source:<5}")
    if not sel.watched:
        lines.append("  (empty - nothing met the entry thresholds)")

    added, removed = sel.by_action("add"), sel.by_action("remove")
    lines += ["",
              f"  {len(sel.watched)}/{cfg.watchlist.max_tokens} watched"
              f" · {len(added)} added · {len(removed)} removed",
              f"  scanned {stats['scanned_launches']} launches over the last "
              f"{cfg.watchlist.discovery_window_min}m; {stats['met_entry']} met entry traction"
              f" ({stats['rpc_calls']} rpc calls)"]
    for d in added:
        lines.append(f"    + {short(d.address)}  {d.reason}")
    for d in removed:
        lines.append(f"    - {short(d.address)}  {d.reason}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trace.watchlist",
                                 description="Stage 1: refresh and print the watchlist")
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true", help="write nothing to the database")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    args = ap.parse_args(argv)

    cfg = load(args.config)
    conn = db.connect(cfg.db_file)
    rpc = Rpc(cfg.chain.rpc_url, cfg.chain.timeout_s)

    got = rpc.chain_id()
    if got != cfg.chain.chain_id:
        print(f"refusing to run: rpc reports chain {got}, config expects "
              f"{cfg.chain.chain_id}", file=sys.stderr)
        return 2

    sel, stats = refresh(cfg, conn, rpc, dry_run=args.dry_run)
    if args.json:
        print(json.dumps({
            "stats": stats,
            "watched": [asdict(c) for c in sorted(sel.watched, key=lambda c: c.score, reverse=True)],
            "decisions": [asdict(d) for d in sel.decisions if d.action != "skip"],
        }, indent=2, default=str))
    else:
        print(render(sel, stats, cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
