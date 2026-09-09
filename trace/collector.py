"""Stage 2 - the collector.

Reads the watchlist, asks a paid feed who is talking about each token, stores
the posts, and hands the counts to the detector. This is the only module in the
project that spends money, so it is the only one written defensively about it.

It does not know which feed it is reading. `trace/feed.py` defines a provider;
`twitterapi.py` and `xapi.py` implement one each, thirty-three times apart in
price, and everything below is written against the interface.

The four rules it exists to enforce
-----------------------------------

**Every query carries a cursor.** Feeds bill per post returned. A poll that
does not say "only what is newer than this" re-buys every post already in the
database, so a five-minute loop over a busy token would pay for the same hour
of chatter twelve times over.

**Polling faster than the window is pure waste.** The detector rates the last
*complete* window, so a second poll inside the same window cannot change any
verdict — and on twitterapi.io it still costs the 15-credit floor. The default
schedule wakes once per window, just after it closes. Ten tokens at one minute
is $2.16 a day before anybody posts; the same ten once per window is $0.43.

**The budget stops the loop, it does not degrade it.** When the day's spend
would exceed `daily_budget_usd`, the collector raises and exits. It does not
poll less, drop tokens or shorten pages, because a detector fed a thinned
stream reports QUIET for a token that is screaming, and a wrong answer is worse
than no answer.

**A window is only rated once it has been collected.** If the last successful
poll of a token happened before the rated window closed, that window's count is
an undercount, so the collector prints nothing for it rather than a verdict
built on it. This is the collector's half of the history rule in `detector.py`.

Query shape, per token:

    ("0xdeadbeef…" OR $TICK) -is:retweet

The **contract address is the anchor** — anyone posting it is talking about
this token and no other. The **cashtag is the reach**, because almost nobody
posts an address. The cashtag is **dropped whenever two watched tokens share a
symbol**: this chain had three live tokens called `Fork` at the same moment,
and `$FORK` would have counted all three as one. A smaller number that is true
beats a bigger one that is not. Retweets are excluded always — a retweet is the
same voice again, and it bills like a new one.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import accounts as _accounts
from . import attribution as _attr
from . import cohort as _cohort
from . import config as _config
from . import db as _db
from . import detector as _detector
from . import feed as _feed
from . import twitterapi, xapi

ENV_KEYS = ("TRACE_X_BEARER", "TRACE_X_API_KEY")
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9_]+$")
MAX_QUERY_CHARS = 512


class BudgetStop(RuntimeError):
    """The day's budget would be exceeded by the next request."""


@dataclass
class PollResult:
    address: str
    query: str
    requests: int = 0
    posts: int = 0
    new_posts: int = 0
    counted: int = 0        # of the new ones, how many are about this token
    cost_usd: float = 0.0
    since_ts: int | None = None
    backfilled: bool = False
    truncated: bool = False
    error: str | None = None


@dataclass
class RoundResult:
    polls: list[PollResult] = field(default_factory=list)
    stopped: str | None = None

    @property
    def cost_usd(self) -> float:
        return sum(p.cost_usd for p in self.polls)

    @property
    def posts(self) -> int:
        return sum(p.posts for p in self.polls)


# --- provider ---------------------------------------------------------------

def build_provider(cfg, key: str):
    name = (cfg.x.provider or "").strip().lower()
    if name in ("twitterapi.io", "twitterapi", "tapi"):
        return twitterapi.Client(key, query_type=cfg.x.query_type)
    if name in ("x", "twitter", "official"):
        return xapi.Client(key)
    raise SystemExit(f'unknown provider "{cfg.x.provider}" - '
                     f'use "twitterapi.io" or "x"')


# --- query ------------------------------------------------------------------

def ambiguous_symbols(rows: Iterable) -> frozenset[str]:
    """Symbols claimed by more than one watched token, lowercased."""
    seen: dict[str, int] = {}
    for r in rows:
        sym = (r["symbol"] if not isinstance(r, tuple) else r[1]) or ""
        sym = sym.strip().lower()
        if sym:
            seen[sym] = seen.get(sym, 0) + 1
    return frozenset(s for s, n in seen.items() if n > 1)


def usable_cashtag(symbol: str | None, cfg,
                   ambiguous: frozenset[str] = frozenset()) -> str | None:
    if not symbol:
        return None
    s = symbol.strip().lstrip("$")
    if not SAFE_SYMBOL.match(s):
        return None
    if not (cfg.ticker_min_len <= len(s) <= cfg.ticker_max_len):
        return None
    if s.lower() in ambiguous:
        return None
    return s


def build_query(address: str, symbol: str | None, cfg,
                ambiguous: frozenset[str] = frozenset(),
                exclude_links: bool = False) -> str:
    """The exact string sent to the feed. Deterministic, so a test can assert."""
    clauses = [f'"{address}"']
    tag = usable_cashtag(symbol, cfg, ambiguous)
    if tag:
        clauses.append(f"${tag}")
    q = f"({' OR '.join(clauses)}) -is:retweet"
    if exclude_links:
        q += " -has:links"
    if len(q) > MAX_QUERY_CHARS:                    # address alone always fits
        q = f'("{address}") -is:retweet' + (" -has:links" if exclude_links else "")
    return q


# --- schedule ---------------------------------------------------------------

def cadence_s(cfg) -> int:
    """How often a round happens, on average. `next_wake` is the distance to
    the *next* one, which is shorter whenever we are mid-window - using it to
    extrapolate a daily bill overstates it, sometimes fivefold."""
    return cfg.x.poll_interval_s or cfg.detector.rate_window_min * 60


def next_wake(now: int, cfg) -> int:
    """Seconds to sleep before the next poll.

    With `poll_interval_s = 0` this lands just after the next window boundary,
    which is the earliest moment a new verdict can exist. Any earlier wake pays
    the per-call floor to learn something the detector will not look at.
    """
    if cfg.x.poll_interval_s:
        return cfg.x.poll_interval_s
    w = cfg.detector.rate_window_min * 60
    return (w - (now % w)) + cfg.x.poll_delay_s


# --- money ------------------------------------------------------------------

def day_start(now: int) -> int:
    return (now // 86400) * 86400          # UTC midnight


def spend_today(conn: sqlite3.Connection, now: int) -> float:
    row = conn.execute("SELECT COALESCE(SUM(est_cost_usd), 0) FROM reads WHERE ts >= ?",
                       (day_start(now),)).fetchone()
    return float(row[0] or 0.0)


def worst_case_request_usd(provider) -> float:
    """The most one request can cost: a full page, every post billed."""
    return provider.cost_usd(provider.page_size)


def check_budget(conn: sqlite3.Connection, provider, cfg, now: int) -> float:
    """Room left today. Raises rather than let a request overshoot."""
    spent = spend_today(conn, now)
    worst = worst_case_request_usd(provider)
    if spent + worst > cfg.daily_budget_usd:
        raise BudgetStop(
            f"daily budget reached: ${spent:.4f} spent of "
            f"${cfg.daily_budget_usd:.2f}, next request could cost ${worst:.4f}")
    return cfg.daily_budget_usd - spent


# --- storage ----------------------------------------------------------------

def store_posts(conn: sqlite3.Connection, address: str,
                posts: Sequence[_feed.Post], now: int,
                symbol: str | None = None,
                launch_ts: int | None = None) -> tuple[int, int]:
    """Insert posts, ignoring ones already held.

    Every post is stored, because we paid for it and evidence is worth keeping.
    Only the ones `attribution.match` recognises carry a `matched` value, and
    only those are counted. Returns (new rows, of which counted).
    """
    new = counted = 0
    for p in posts:
        if p.author and (p.author_name or p.author_avatar):
            # Last one wins: people change their picture, and the freshest
            # answer the provider gave us is the right one to keep.
            conn.execute(
                """INSERT INTO authors (handle, name, avatar, seen_ts)
                   VALUES (?,?,?,?)
                   ON CONFLICT(handle) DO UPDATE SET
                       name   = COALESCE(excluded.name, authors.name),
                       avatar = COALESCE(excluded.avatar, authors.avatar),
                       seen_ts = excluded.seen_ts""",
                (p.author, p.author_name, p.author_avatar, now))
        m = _attr.match(p.text, address, symbol, p.created_at, launch_ts)
        cur = conn.execute(
            """INSERT OR IGNORE INTO posts
                   (post_id, token_address, author, created_at, text,
                    ingested_at, matched)
               VALUES (?,?,?,?,?,?,?)""",
            (p.post_id, address, p.author, p.created_at, p.text, now, m))
        if cur.rowcount:
            new += 1
            counted += 1 if m else 0
    if new:
        # Rebuild rather than increment: minute_counts is then a pure function
        # of posts and can never drift out of step with it after a crash or a
        # duplicated page.
        _detector.rebuild_minutes(conn, address)
    return new, counted


def read_cursor(conn: sqlite3.Connection, address: str) -> tuple[str | None, int]:
    row = conn.execute("SELECT since_id, updated_ts FROM cursors WHERE token_address = ?",
                       (address,)).fetchone()
    return (row[0], row[1] or 0) if row else (None, 0)


def observed_from(conn: sqlite3.Connection, address: str) -> int | None:
    row = conn.execute("SELECT observed_from FROM cursors WHERE token_address = ?",
                       (address,)).fetchone()
    return row[0] if row and row[0] else None


def write_cursor(conn: sqlite3.Connection, address: str, since_id: str | None,
                 now: int, observed: int | None = None) -> None:
    conn.execute(
        """INSERT INTO cursors (token_address, since_id, updated_ts, observed_from)
           VALUES (?,?,?,?)
           ON CONFLICT(token_address) DO UPDATE SET
               since_id = COALESCE(excluded.since_id, cursors.since_id),
               updated_ts = excluded.updated_ts,
               -- only ever moves earlier, and only if it was never set
               observed_from = COALESCE(cursors.observed_from,
                                        excluded.observed_from)""",
        (address, since_id, now, observed))


def record_read(conn: sqlite3.Connection, address: str, page: _feed.Page,
                cost: float, since_ts: int | None, now: int) -> None:
    link_posts = sum(1 for p in page.posts if "http" in p.text)
    conn.execute(
        """INSERT INTO reads (ts, token_address, posts_returned, link_posts,
                              est_cost_usd, since_id, http_status)
           VALUES (?,?,?,?,?,?,?)""",
        (now, address, page.count, link_posts, cost,
         str(since_ts) if since_ts else None, page.http_status))


# --- one token --------------------------------------------------------------

def since_for(conn: sqlite3.Connection, address: str, cfg, now: int) -> int:
    """Ask for posts newer than this.

    A token polled before: its last poll, minus a small overlap so nothing is
    lost at the seam.

    A token never polled: **the whole backfill window**, because a token joins
    the watchlist at the moment it starts getting traction, and the detector
    needs six closed windows of baseline before it may speak. Measured on live
    data, one token's entire event was 13, 11 and 14 posts in three consecutive
    windows and then it was over - all of it inside the warm-up. Reading the
    hour behind admission buys that baseline immediately, and the windows are
    genuinely observed: we asked the feed about them and it answered.
    """
    _, last = read_cursor(conn, address)
    if not last:
        return now - cfg.x.backfill_min * 60
    return max(0, last - cfg.x.overlap_s)


def poll_token(conn: sqlite3.Connection, provider, cfg, *, address: str,
               symbol: str | None, now: int,
               ambiguous: frozenset[str] = frozenset(),
               launch_ts: int | None = None) -> PollResult:
    query = build_query(address, symbol, cfg.watchlist, ambiguous,
                        exclude_links=cfg.x.exclude_links)
    cold = observed_from(conn, address) is None
    since_ts = since_for(conn, address, cfg, now)
    res = PollResult(address=address, query=query, since_ts=since_ts,
                     backfilled=cold)

    cursor = None
    for _ in range(cfg.x.max_pages_per_poll):
        check_budget(conn, provider, cfg.x, now)   # raises BudgetStop, never trims
        page = provider.search(query, since_ts=since_ts, cursor=cursor)
        cost = provider.cost_usd(page.count)
        res.requests += 1
        res.posts += page.count
        res.cost_usd += cost
        record_read(conn, address, page, cost, since_ts, now)
        added, counted = store_posts(conn, address, page.posts, now, symbol,
                                     launch_ts)
        res.new_posts += added
        res.counted += counted
        cursor = page.next_cursor
        if not cursor or page.count == 0:
            break
    else:
        res.truncated = True

    # Only after the whole walk: a cursor moved mid-pagination leaves a hole
    # that nothing ever fills.
    #
    # observed_from is clamped to the launch: windows before the token existed
    # are not quiet, they are not applicable, and counting them as silence
    # would flatter every multiplier after it.
    observed = max(since_ts, launch_ts) if (cold and launch_ts) else (
        since_ts if cold else None)
    write_cursor(conn, address, None, now, observed)
    return res


# --- one round --------------------------------------------------------------

def run_once(conn: sqlite3.Connection, provider, cfg,
             now: int | None = None, rows=None) -> RoundResult:
    now = int(time.time()) if now is None else now
    rows = _db.active_watchlist(conn) if rows is None else rows
    amb = ambiguous_symbols(rows)
    out = RoundResult()
    for r in rows:
        try:
            out.polls.append(poll_token(conn, provider, cfg,
                                        address=r["address"], symbol=r["symbol"],
                                        now=now, ambiguous=amb,
                                        launch_ts=r["launch_ts"]))
        except BudgetStop as e:
            out.stopped = str(e)
            break
        except _feed.RateLimited as e:
            out.stopped = f"rate limited, resets in {e.wait_s}s"
            break
        except _feed.AuthError:
            raise
        except _feed.FeedError as e:
            out.polls.append(PollResult(address=r["address"], query="", error=str(e)))
    return out


def params_from(cfg) -> _detector.Params:
    return _detector.Params(
        window_s=cfg.detector.rate_window_min * 60,
        baseline_windows=cfg.detector.baseline_windows,
        floor=cfg.detector.floor,
        spike_x=cfg.detector.spike_multiplier,
        warm_x=cfg.detector.warm_multiplier,
        cooldown_s=cfg.detector.cooldown_min * 60,
        baseline_min=cfg.detector.min_baseline)


def collection_start(conn: sqlite3.Connection, address: str) -> int | None:
    """When we first actually paid to read this token. None if never."""
    row = conn.execute("SELECT MIN(ts) FROM reads WHERE token_address = ?",
                       (address,)).fetchone()
    return row[0] if row and row[0] else None


def watch_start_for(conn: sqlite3.Connection, address: str,
                    added_ts: int) -> int | None:
    """When history begins for this token, as far as the detector is concerned.

    Not `added_ts` — a token can sit on the watchlist long before the collector
    is switched on, and the detector would take those windows for silence when
    nobody was watching them. Not the first poll either, now that the first
    poll reads an hour backwards: those windows *were* asked about, so they are
    observed and they count.

    It is `observed_from`: the earliest moment we have actually put a question
    to the feed about this token, clamped to its launch. Getting this wrong is
    not a rounding error in either direction — too early invents silence and
    makes the first real post an infinite multiplier; too late throws away the
    baseline we paid for and leaves the detector mute through the only event
    worth catching.
    """
    return observed_from(conn, address)


def rate_tokens(conn: sqlite3.Connection, cfg, now: int, rows,
                listed: frozenset[str], curve=None) -> list[tuple[object, object]]:
    """Detector verdicts for tokens whose last window was actually collected."""
    params = params_from(cfg)
    curve = _cohort.build(conn, params.window_s) if curve is None else curve
    rated_window_end = _detector.window_start(now, params.window_s)
    rated = rated_window_end - params.window_s
    out = []
    for r in rows:
        _, polled_ts = read_cursor(conn, r["address"])
        watch_start = watch_start_for(conn, r["address"], r["added_ts"])
        if watch_start is None or polled_ts < rated_window_end:
            # Never collected, or the rated window closed after our last poll:
            # its count is an undercount, so it is not a verdict.
            out.append((r, None))
            continue
        cb = (curve.baseline_for(rated - r["launch_ts"])
              if r["launch_ts"] else None)
        out.append((r, _detector.tick(conn, r["address"], now, watch_start,
                                      params=params, listed=listed,
                                      cohort_baseline=cb)))
    return out


# --- recall probe -----------------------------------------------------------
# The first live round returned one post across ten tokens, and that post
# matched on the cashtag while carrying a *different* contract address. Both
# halves of that sentence are a problem, and neither can be argued about from a
# desk. This measures them.
#
#   address-only    what we can trust completely, and almost nobody writes
#   cashtag-only    what people actually write, and it is not ours to claim
#   overlap         cashtag hits that also carry our address = the honest join
#
# The ratio between the first two is the recall question the whole attention
# half stands on. If nobody ever posts an address, an address-anchored counter
# counts nothing; if a cashtag pulls in three other tokens with the same name,
# a cashtag-anchored counter counts a lie.

@dataclass
class Probe:
    symbol: str
    address: str
    addr_posts: int = 0
    addr_authors: int = 0
    tag_posts: int = 0
    tag_authors: int = 0
    tag_with_addr: int = 0
    counted: int = 0
    cost_usd: float = 0.0
    sample: str = ""


def _walk(provider, query: str, since_ts: int, max_pages: int):
    posts, cost, cursor = [], 0.0, None
    for _ in range(max_pages):
        page = provider.search(query, since_ts=since_ts, cursor=cursor)
        posts.extend(page.posts)
        cost += provider.cost_usd(page.count)
        cursor = page.next_cursor
        if not cursor or page.count == 0:
            break
    return posts, cost


def probe_token(provider, address: str, symbol: str | None, *, since_ts: int,
                max_pages: int = 3, conn: sqlite3.Connection | None = None,
                now: int | None = None, launch_ts: int | None = None) -> Probe:
    out = Probe(symbol=symbol or address[:10], address=address)
    addr_q = f'"{address}" -is:retweet'
    posts, cost = _walk(provider, addr_q, since_ts, max_pages)
    out.addr_posts = len(posts)
    out.addr_authors = len({p.author for p in posts if p.author})
    out.cost_usd += cost
    if conn is not None:
        store_posts(conn, address, posts, now or 0, symbol, launch_ts)

    if symbol and SAFE_SYMBOL.match(symbol.strip().lstrip("$") or ""):
        tag = symbol.strip().lstrip("$")
        posts, cost = _walk(provider, f"${tag} -is:retweet", since_ts, max_pages)
        out.tag_posts = len(posts)
        out.tag_authors = len({p.author for p in posts if p.author})
        out.tag_with_addr = sum(1 for p in posts if address.lower() in p.text.lower())
        out.counted = sum(1 for p in posts
                          if _attr.match(p.text, address, symbol))
        out.cost_usd += cost
        if conn is not None:
            store_posts(conn, address, posts, now or 0, symbol, launch_ts)
        if posts:
            t = posts[0]
            out.sample = f"@{t.author}: {' '.join(t.text.split())[:110]}"
    return out


# --- calibration ------------------------------------------------------------
# `floor = 8` and `spike = 3.0x` were guesses, and the README has said so since
# the day they were written. They cannot be argued into correctness: they are
# claims about how loud this chain actually gets, and that is measurable from
# posts already paid for and stored. This reads them back and reports the one
# distribution the thresholds live or die by - how many posts a token gets in a
# five-minute window, across a real day.

def horizons_from_reads(conn: sqlite3.Connection) -> int:
    """Fill in `observed_from` from the paper trail, where it is missing.

    `reads` records the `since` of every request ever made, which is precisely
    the statement "we asked this feed about this token from this moment
    onward". So the horizon never has to be guessed: it is the earliest thing
    we ever asked about, and it is already written down. This repairs rows
    written before the column existed, and rows written by --probe, which asks
    about a day of history without being a poll.
    """
    fixed = 0
    for addr, first_since, last_ts in conn.execute(
            """SELECT token_address, MIN(CAST(since_id AS INTEGER)), MAX(ts)
                 FROM reads WHERE since_id IS NOT NULL
                GROUP BY token_address""").fetchall():
        if not first_since:
            continue
        launch = conn.execute("SELECT launch_ts FROM tokens WHERE address = ?",
                              (addr,)).fetchone()
        horizon = max(first_since, launch[0]) if (launch and launch[0]) else first_since
        cur = conn.execute(
            """INSERT INTO cursors (token_address, since_id, updated_ts,
                                    observed_from)
               VALUES (?, NULL, ?, ?)
               ON CONFLICT(token_address) DO UPDATE SET
                   updated_ts = MAX(COALESCE(cursors.updated_ts, 0),
                                    excluded.updated_ts),
                   observed_from = COALESCE(cursors.observed_from,
                                            excluded.observed_from)""",
            (addr, last_ts, horizon))
        fixed += cur.rowcount or 0
    return fixed


def reattribute(conn: sqlite3.Connection, rows) -> int:
    """Recompute `matched` on every stored post against the current rules.

    Free, offline, and idempotent. Called before every calibration so the
    report can never describe a rule the code no longer applies - posts stored
    under an older version of attribution.py would otherwise quietly skew it.
    """
    changed = 0
    for r in rows:
        addr, sym = r["address"], r["symbol"]
        for p in conn.execute(
                "SELECT post_id, text, matched, created_at FROM posts "
                "WHERE token_address = ?",
                (addr,)).fetchall():
            m = _attr.match(p["text"] or "", addr, sym,
                            p["created_at"], r["launch_ts"])
            if m != p["matched"]:
                conn.execute("UPDATE posts SET matched = ? WHERE post_id = ?",
                             (m, p["post_id"]))
                changed += 1
        if changed:
            _detector.rebuild_minutes(conn, addr)
    return changed


def calibrate(conn: sqlite3.Connection, cfg, rows) -> list[dict]:
    w = cfg.detector.rate_window_min * 60
    out = []
    for r in rows:
        addr = r["address"]
        posts = conn.execute(
            """SELECT created_at, author, matched FROM posts
                WHERE token_address = ? ORDER BY created_at""", (addr,)).fetchall()
        counted = [p for p in posts if p["matched"]]
        if not posts:
            continue
        buckets: dict[int, int] = {}
        authors: dict[str, int] = {}
        for p in counted:
            b = _detector.window_start(p["created_at"], w)
            buckets[b] = buckets.get(b, 0) + 1
            authors[p["author"] or "?"] = authors.get(p["author"] or "?", 0) + 1
        span = (posts[-1]["created_at"] - posts[0]["created_at"]) or 1
        top = max(authors.values()) if authors else 0
        out.append({
            "symbol": r["symbol"] or addr[:10],
            "paid": len(posts),
            "counted": len(counted),
            "authors": len(authors),
            "top_share": (100 * top // len(counted)) if counted else 0,
            "hours": span / 3600,
            "busiest": max(buckets.values()) if buckets else 0,
            "windows_1": sum(1 for v in buckets.values() if v >= 1),
            "windows_3": sum(1 for v in buckets.values() if v >= 3),
            "windows_8": sum(1 for v in buckets.values() if v >= 8),
        })
    return out


# --- cli --------------------------------------------------------------------

def api_key(cfg) -> str:
    for env in ENV_KEYS:
        if os.environ.get(env, "").strip():
            return os.environ[env].strip()
    return cfg.x.api_key.strip()


def pin_cli_tokens(conn: sqlite3.Connection, specs: Sequence[str], now: int) -> None:
    """`--tokens 0xabc:WIF,0xdef` - watch these and nothing else."""
    for spec in specs:
        addr, _, sym = spec.partition(":")
        addr = addr.strip().lower()
        if not addr.startswith("0x"):
            raise SystemExit(f"not an address: {spec}")
        _db.upsert_token(conn, address=addr, symbol=(sym.strip() or None),
                         name=None, decimals=None, launch_block=0,
                         launch_ts=None, related=(), now_ts=now)
        _db.add_to_watchlist(conn, addr, "pin", now, "cli --tokens")


def _fmt_round(res: RoundResult) -> str:
    lines = []
    for p in res.polls:
        if p.error:
            lines.append(f"  {p.address[:10]}  ERROR  {p.error}")
            continue
        flag = "  TRUNCATED" if p.truncated else ""
        lines.append(f"  {p.address[:10]}  {p.posts:>4} posts "
                     f"({p.new_posts:>4} new, {p.counted:>3} counted)  "
                     f"{p.requests} req  ${p.cost_usd:.4f}{flag}")
    lines.append(f"  round: {res.posts} posts, ${res.cost_usd:.4f}")
    if res.stopped:
        lines.append(f"  STOPPED: {res.stopped}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TRACE collector (stage 2)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--provider", default=None, help='override: twitterapi.io | x')
    ap.add_argument("--check", action="store_true",
                    help="prove the key works. One empty call, the price of a "
                         "seventh of a cent.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact queries and the bill, make no request")
    ap.add_argument("--once", action="store_true", help="one round, then exit")
    ap.add_argument("--budget", type=float, default=None,
                    help="override daily_budget_usd for this run")
    ap.add_argument("--probe", action="store_true",
                    help="measure recall: how many posts carry the contract "
                         "address, how many carry the cashtag, and how much "
                         "the two overlap. Costs cents, answers the only "
                         "question the thresholds depend on.")
    ap.add_argument("--cohort", action="store_true",
                    help="print the measured age curve: what a token of a "
                         "given age normally gets. Free, no request.")
    ap.add_argument("--calibrate", action="store_true",
                    help="read back the posts already stored and report how "
                         "loud this chain actually gets. Costs nothing and "
                         "makes no request - it is arithmetic on paid data.")
    ap.add_argument("--probe-hours", type=int, default=24,
                    help="how far back --probe looks (default 24)")
    ap.add_argument("--refresh-min", type=int, default=15,
                    help="re-run watchlist discovery this often while polling. "
                         "0 turns it off. A token's whole event is over inside "
                         "fifteen minutes, so a watchlist left standing for an "
                         "hour is a list of funerals.")
    ap.add_argument("--tokens", default=None,
                    help="comma separated 0xaddr[:SYMBOL] - pin these instead "
                         "of using the watchlist")
    args = ap.parse_args(argv)

    cfg = _config.load(args.config)
    if args.budget is not None:
        cfg = _config.replace_x(cfg, daily_budget_usd=args.budget)
    if args.provider:
        cfg = _config.replace_x(cfg, provider=args.provider)
    conn = _db.connect(cfg.db_file)
    now = int(time.time())

    if args.tokens:
        pin_cli_tokens(conn, [s for s in args.tokens.split(",") if s.strip()], now)

    rows = _db.active_watchlist(conn)
    if not rows and not args.check:
        # On a server the collector starts before anything has ever run stage
        # 1, and exiting here put systemd in a restart loop: start, print this,
        # die, wait twenty seconds, repeat. If it is going to refresh the
        # watchlist every quarter of an hour anyway it can do the first one
        # now. Discovery reads the chain and costs nothing.
        if args.refresh_min:
            print("watchlist is empty - running discovery first", file=sys.stderr)
            try:
                from . import watchlist as _wl
                from .rpc import Rpc
                _wl.refresh(cfg, conn, Rpc(cfg.chain.rpc_url, cfg.chain.timeout_s))
                rows = _db.active_watchlist(conn)
            except Exception as e:
                print(f"  discovery failed ({type(e).__name__}: {e})", file=sys.stderr)
        if not rows:
            print("watchlist is empty - run python -m trace.watchlist, or pass "
                  "--tokens 0xaddr:SYMBOL", file=sys.stderr)
            return 1
    amb = ambiguous_symbols(rows)

    if args.cohort:
        horizons_from_reads(conn)
        curve = _cohort.build(conn, cfg.detector.rate_window_min * 60)
        print("what a token normally gets, by how old it is\n")
        print(f"{'age':>10} {'windows':>9} {'posts':>7} {'normal':>14}")
        print("-" * 44)
        for b in _cohort.BUCKETS:
            m = curve.mean(b)
            print(f"{_cohort.label(b):>10} {curve.windows.get(b, 0):>9} "
                  f"{curve.posts.get(b, 0):>7} "
                  f"{('not measured' if m is None else f'{m:.2f} / window'):>14}")
        print("-" * 44)
        print(f"{curve.total_windows} observed windows behind this curve")
        print(f"a bucket says nothing until {_cohort.MIN_WINDOWS} windows are "
              f"behind it - the honest answer to 'what is normal' with four "
              f"data points is that we do not know yet")
        return 0

    if args.calibrate:
        horizons_from_reads(conn)
        moved = reattribute(conn, rows)
        if moved:
            print(f"re-attributed {moved} stored posts against the current "
                  f"rules\n")
        stats = calibrate(conn, cfg, rows)
        if not stats:
            print("no posts stored yet - run --probe --probe-hours 24 first",
                  file=sys.stderr)
            return 1
        w = cfg.detector.rate_window_min
        print(f"what {w}-minute windows actually look like, from stored posts\n")
        print(f"{'token':<12} {'paid':>5} {'counted':>8} {'acct':>5} "
              f"{'top%':>5} {'hours':>6} {'busiest':>8} "
              f"{'w>=1':>5} {'w>=3':>5} {'w>=8':>5}")
        print("-" * 78)
        for t in stats:
            print(f"{t['symbol'][:12]:<12} {t['paid']:>5} {t['counted']:>8} "
                  f"{t['authors']:>5} {t['top_share']:>4}% {t['hours']:>6.1f} "
                  f"{t['busiest']:>8} {t['windows_1']:>5} {t['windows_3']:>5} "
                  f"{t['windows_8']:>5}")
        print("-" * 78)
        paid = sum(t["paid"] for t in stats)
        counted = sum(t["counted"] for t in stats)
        busiest = max(t["busiest"] for t in stats)
        w8 = sum(t["windows_8"] for t in stats)
        w3 = sum(t["windows_3"] for t in stats)
        print(f"{'total':<12} {paid:>5} {counted:>8}")
        print(f"\nof {paid} posts paid for, {counted} are provably about the "
              f"token they were filed under "
              f"({100 * counted // max(paid, 1)}%). The rest are other "
              f"people's assets sharing a ticker, and are stored but not "
              f"counted.")
        print(f"busiest single window anywhere: {busiest} posts")
        print(f"windows reaching the current floor of {cfg.detector.floor}: "
              f"{w8}.  Windows reaching 3: {w3}.")
        if busiest < cfg.detector.floor:
            print(f"\n  the floor of {cfg.detector.floor} was never once "
                  f"reached in this data. On these tokens it does not filter "
                  f"noise, it switches the detector off. Either the floor "
                  f"comes down, or the window gets longer, or the watchlist "
                  f"picks tokens people actually talk about - and that is a "
                  f"decision, not a bug.")
        return 0

    if args.dry_run:
        cold = sum(1 for r in rows if not read_cursor(conn, r["address"])[1])
        print(f"provider {cfg.x.provider} · {len(rows)} tokens · "
              f"spent today ${spend_today(conn, now):.4f} of "
              f"${cfg.x.daily_budget_usd:.2f}")
        for r in rows:
            q = build_query(r["address"], r["symbol"], cfg.watchlist, amb,
                            exclude_links=cfg.x.exclude_links)
            _, polled = read_cursor(conn, r["address"])
            print(f"  {(r['symbol'] or r['address'][:10]):<12} {q}")
            print(f"  {'':<12} since_time={since_for(conn, r['address'], cfg, now)}"
                  f"  last_poll="
                  f"{str((now - polled) // 60) + 'm ago' if polled else 'never'}")
        probe = build_provider(cfg, "dry-run-no-key-needed")
        floor = len(rows) * probe.cost_usd(0)
        worst = len(rows) * worst_case_request_usd(probe)
        cadence = cadence_s(cfg)
        per_day = 86400 / max(cadence, 1)
        print(f"one round: ${floor:.4f} if nobody posted, ${worst:.4f} if every "
              f"token filled a page ({probe.page_size} posts)")
        print(f"schedule: a round every {cadence}s ({per_day:.0f} a day) "
              f"-> ${floor * per_day:.2f}/day floor, before a single post; "
              f"next wake in {next_wake(now, cfg)}s")
        if worst > cfg.x.daily_budget_usd:
            print(f"  note: one worst-case round (${worst:.2f}) already exceeds "
                  f"the ${cfg.x.daily_budget_usd:.2f} daily budget, so a very "
                  f"loud round would stop the collector rather than overspend")
        print(f"{cold} of {len(rows)} tokens have never been polled and will "
              f"ask for one window of history, no more")
        return 0

    key = api_key(cfg)
    if not key:
        print(f"no API key. Put it in {ENV_KEYS[0]} or [x] api_key in "
              f"config.toml", file=sys.stderr)
        return 2
    try:
        provider = build_provider(cfg, key)
    except _feed.AuthError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 2

    if args.check:
        try:
            page = provider.search(f'"tracekeycheck{os.urandom(6).hex()}"')
        except _feed.AuthError as e:
            print(f"key rejected: {e}", file=sys.stderr)
            if provider.name == "x" and not xapi.looks_like_bearer(key):
                print(f"\n  the key is {len(key)} characters long. An official X "
                      f"bearer token is well over a hundred.\n"
                      f"  A key starting 'new1_' is a twitterapi.io key - set "
                      f"provider = \"twitterapi.io\" in config.toml.",
                      file=sys.stderr)
            elif provider.name == "twitterapi.io":
                print("\n  check the key on twitterapi.io/dashboard, and that "
                      "the credit balance is above zero.", file=sys.stderr)
            return 2
        print(f"key works · provider {provider.name}")
        print(f"  probe returned {page.count} posts, "
              f"cost ${provider.cost_usd(page.count):.5f}")
        if provider.name == "twitterapi.io":
            print(f"  billing: {twitterapi.CREDITS_PER_POST} credits a post, "
                  f"{twitterapi.MIN_CREDITS_PER_CALL} minimum a call, "
                  f"{twitterapi.CREDITS_PER_USD:,} credits to the dollar")
        r = page.rate
        if r.limit:
            print(f"  rate limit: {r.remaining}/{r.limit} left in the window")
        print(f"  spent today: ${spend_today(conn, now):.4f} of "
              f"${cfg.x.daily_budget_usd:.2f}")
        if rows:
            print(f"  watchlist: {len(rows)} tokens, a round every "
                  f"{next_wake(now, cfg)}s")
        return 0

    if args.probe:
        since = now - args.probe_hours * 3600
        worst = len(rows) * 2 * 3 * provider.cost_usd(provider.page_size)
        print(f"probing {len(rows)} tokens over {args.probe_hours}h · "
              f"at most ${worst:.2f}, usually far less\n")
        print(f"{'token':<12} {'by address':>11} {'by cashtag':>11} "
              f"{'both':>6} {'counted':>8}  {'accounts':>8}")
        print("-" * 66)
        total = 0.0
        probes = []
        for r in rows:
            try:
                check_budget(conn, provider, cfg.x, now)
            except BudgetStop as e:
                print(f"\nstopped: {e}")
                break
            pr = probe_token(provider, r["address"], r["symbol"], since_ts=since,
                             conn=conn, now=now, launch_ts=r["launch_ts"])
            lt = r["launch_ts"]
            write_cursor(conn, r["address"], None, now,
                         max(since, lt) if lt else since)
            probes.append(pr)
            total += pr.cost_usd
            conn.execute(
                """INSERT INTO reads (ts, token_address, posts_returned,
                                      link_posts, est_cost_usd, since_id,
                                      http_status)
                   VALUES (?,?,?,?,?,?,?)""",
                (now, r["address"], pr.addr_posts + pr.tag_posts, 0,
                 pr.cost_usd, str(since), 200))
            print(f"{pr.symbol[:12]:<12} {pr.addr_posts:>11} {pr.tag_posts:>11} "
                  f"{pr.tag_with_addr:>6} {pr.counted:>8}  "
                  f"{max(pr.addr_authors, pr.tag_authors):>8}")
        addr = sum(p.addr_posts for p in probes)
        tag = sum(p.tag_posts for p in probes)
        both = sum(p.tag_with_addr for p in probes)
        print("-" * 66)
        print(f"{'total':<12} {addr:>11} {tag:>11} {both:>6} "
              f"{sum(p.counted for p in probes):>8}")
        print(f"\nspent ${total:.4f}")
        if tag:
            print(f"of every 100 posts using a ticker, {100 * both // tag} also "
                  f"carry the contract address")
        if addr == 0 and tag == 0:
            print("nobody is posting about these tokens at all, by either "
                  "handle. That is a fact about the watchlist, not a bug in "
                  "the collector: it ranks tokens by transfers, which is the "
                  "candle, and the product is about the talking.")
        elif addr * 5 < tag:
            print("the address is far too rare to count on its own. The cashtag "
                  "is where the posting is - and the 'both' column is how much "
                  "of it is provably about the token we mean.")
        for p in probes:
            if p.sample:
                print(f"\n  {p.symbol}: {p.sample}")
        return 0

    listed = _accounts.load()
    last_refresh = 0

    def refresh_watchlist(now: int) -> str | None:
        """Re-run stage 1 in process.

        Without this the collector polls whatever the watchlist held when it
        started. Measured on this chain, a token's entire event is over inside
        fifteen minutes and attention falls thirtyfold by its second hour - so
        an hour-old watchlist is a list of tokens whose moment has passed, and
        every poll of it is money spent watching a funeral.
        """
        from . import watchlist as _wl
        from .rpc import Rpc
        try:
            rpc = Rpc(cfg.chain.rpc_url, cfg.chain.timeout_s)
            sel, _ = _wl.refresh(cfg, conn, rpc)
            added = len(sel.by_action("add"))
            removed = len(sel.by_action("remove"))
            if added or removed:
                return f"watchlist: +{added} -{removed}"
            # Saying nothing when nothing moved makes silence ambiguous: a
            # watchlist that has not rotated in five hours looks exactly like a
            # refresh that never ran, and the difference is money. So it says
            # it looked.
            held = len(_db.active_watchlist(conn))
            return f"watchlist: re-checked, nothing moved ({held} held)"
        except Exception as e:                    # RPC is not the collector's job
            return f"watchlist refresh failed ({type(e).__name__}), keeping the old one"

    while True:
        now = int(time.time())
        if args.refresh_min and now - last_refresh >= args.refresh_min * 60:
            note = refresh_watchlist(now)
            last_refresh = now
            if note:
                print(f"{time.strftime('%H:%M:%S', time.gmtime(now))}  {note}")
            rows = _db.active_watchlist(conn)
        try:
            res = run_once(conn, provider, cfg, now=now, rows=rows)
        except _feed.AuthError as e:
            print(f"key rejected mid-run: {e}", file=sys.stderr)
            return 2
        print(time.strftime("%H:%M:%S", time.gmtime(now)) + "  poll")
        print(_fmt_round(res))

        for r, v in rate_tokens(conn, cfg, now, rows, listed):
            sym = (r["symbol"] or r["address"][:10])[:12]
            if v is None:
                print(f"  {sym:<12} (window not fully collected yet)")
                continue
            mark = ""
            if v.first_known:
                mark = f"  @{v.first_known} ON THE LIST"
            if v.state == _detector.SPIKE and v.first_author:
                mark += f"  first: @{v.first_author}"
            cash = ""
            if v.backed:
                cash = f"  ${v.money_usd:,.0f} behind it ({v.backed} holding)"
            print(f"  {sym:<12} {v.state:<8} rate {v.rate:>4}  "
                  f"acct {v.authors:>3}  base {v.baseline:>6.2f} "
                  f"{(v.baseline_kind or '-'):<6} {v.multiplier:>5.1f}x{cash}{mark}")

        if res.stopped and "budget" in res.stopped:
            print("budget spent. Not polling again today.", file=sys.stderr)
            return 3
        if args.once:
            return 0
        rows = _db.active_watchlist(conn)
        time.sleep(next_wake(int(time.time()), cfg))


if __name__ == "__main__":
    raise SystemExit(main())
