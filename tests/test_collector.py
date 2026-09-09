"""Stage 2, tested without a network and without spending a cent.

The collector is the only module that costs money, so these tests exist to
answer three questions before the first real request: *would this thing ever
pay twice for the same post, keep paying after the budget is gone, or poll more
often than the detector can use?*

Every test drives the real client and the real collector functions. The only
thing replaced is the transport — the function that would otherwise put a
request on the wire. The query string, the cursor handling, the pagination walk
and the cost arithmetic under test are byte for byte the ones that will run
against twitterapi.io.
"""

import unittest
from datetime import datetime, timezone

from trace import collector as c
from trace import config as _config
from trace import db
from trace import detector as d
from trace import cohort
from trace import feed
from trace import twitterapi, xapi

W = 300
T0 = (1_780_000_000 // W) * W
A1 = "0xaaaa000000000000000000000000000000000001"
A2 = "0xbbbb000000000000000000000000000000000002"
# A key has to look like one now: check_key refuses placeholders
# and anything too short to be real. See trace/feed.py.
KEY = "new1_09da0ec4d402404681aaaabbbb"


def tw_date(ts: int) -> str:
    """Twitter's legacy format, which is what twitterapi.io returns."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%a %b %d %H:%M:%S +0000 %Y")


def body(ids, *, t0=T0, authors=None, cursor=None, texts=None, addr=A1):
    """A twitterapi.io advanced_search response. Newest first.

    The default text carries the contract address, because that is what a post
    that actually counts looks like — see trace/attribution.py."""
    authors, texts = authors or {}, texts or {}
    tweets = []
    for i, pid in enumerate(ids):
        who = authors.get(pid, "anon")
        tweets.append({"type": "tweet", "id": pid,
                       "text": texts.get(pid, f"gm {addr}"),
                       "createdAt": tw_date(t0 - i),
                       "author": {"type": "user", "userName": who.upper()}})
    return {"tweets": tweets, "has_next_page": bool(cursor),
            "next_cursor": cursor or ""}


class Fake:
    """A transport that hands back canned pages and records what was asked."""

    def __init__(self, *pages, headers=None):
        self.pages = list(pages)
        self.headers = headers or {}
        self.calls = []

    def __call__(self, url, params, hdrs):
        self.calls.append(dict(params))
        self.headers_sent = dict(hdrs)
        if not self.pages:
            return 200, self.headers, body([])
        item = self.pages.pop(0)
        return item if isinstance(item, tuple) else (200, self.headers, item)

    @property
    def queries(self):
        return [k["query"] for k in self.calls]


def client(*pages, **kw):
    return twitterapi.Client(KEY, transport=Fake(*pages), **kw)


def cfg(**x):
    base = _config.Config()
    return _config.replace_x(base, **x) if x else base


def conn():
    return db.connect(":memory:")


def watch(conn_, addr, symbol, *, added_ts=T0):
    db.upsert_token(conn_, address=addr, symbol=symbol, name=None, decimals=None,
                    launch_block=0, launch_ts=added_ts, related=(), now_ts=added_ts)
    db.add_to_watchlist(conn_, addr, "pin", added_ts, "test")


def polled(conn_, addr=A1, ts=T0, observed=None):
    """A token already polled once, so it is no longer cold. `observed` is how
    far back history has actually been asked about — see the backfill."""
    c.write_cursor(conn_, addr, None, ts, observed if observed is not None
                   else ts - 20 * W)


# --- the query --------------------------------------------------------------

class TestQuery(unittest.TestCase):
    def setUp(self):
        self.w = _config.Config().watchlist

    def test_address_and_cashtag(self):
        self.assertEqual(c.build_query(A1, "WIF", self.w),
                         f'("{A1}" OR $WIF) -is:retweet')

    def test_retweets_always_excluded(self):
        # A retweet is the same voice again, and it bills like a new one.
        self.assertIn("-is:retweet", c.build_query(A1, "WIF", self.w))

    def test_links_are_not_excluded_by_default(self):
        # No provider charges more to read a post with a link. The $0.20 price
        # everyone quotes is for writing one.
        self.assertNotIn("-has:links", c.build_query(A1, "WIF", self.w))

    def test_links_excluded_when_asked(self):
        self.assertIn("-has:links",
                      c.build_query(A1, "WIF", self.w, exclude_links=True))

    def test_shared_symbol_drops_the_cashtag(self):
        # Three live tokens called Fork at once: $FORK would count all three
        # as one. The address is smaller and true.
        self.assertEqual(c.build_query(A1, "FORK", self.w, frozenset({"fork"})),
                         f'("{A1}") -is:retweet')

    def test_unusable_symbols_drop_the_cashtag(self):
        for sym in ("", None, "A", "x" * 40, "WI F", "WI-F", "🚀"):
            with self.subTest(sym=sym):
                self.assertNotIn("$", c.build_query(A1, sym, self.w))

    def test_ambiguous_symbols_detects_collisions(self):
        cn = conn()
        watch(cn, A1, "Fork")
        watch(cn, A2, "FORK")
        self.assertEqual(c.ambiguous_symbols(db.active_watchlist(cn)),
                         frozenset({"fork"}))

    def test_distinct_symbols_are_not_ambiguous(self):
        cn = conn()
        watch(cn, A1, "WIF")
        watch(cn, A2, "BONK")
        self.assertEqual(c.ambiguous_symbols(db.active_watchlist(cn)), frozenset())


# --- the cursor, which is the whole cost story ------------------------------

class TestSince(unittest.TestCase):
    def test_a_cold_token_reads_the_backfill_window(self):
        # A token joins the watchlist because it is already getting traction,
        # so the burst is happening now and the detector needs six closed
        # windows before it may speak. Buy them.
        cn = conn()
        watch(cn, A1, "WIF")
        self.assertEqual(c.since_for(cn, A1, cfg(backfill_min=60), T0),
                         T0 - 3600)

    def test_the_backfill_is_long_enough_to_end_the_warm_up_at_once(self):
        need = (d.DEFAULTS.baseline_windows + 1) * d.DEFAULTS.window_s
        self.assertGreaterEqual(_config.Config().x.backfill_min * 60, need)

    def test_a_backfilled_token_is_rateable_immediately(self):
        # The whole point. Before this, the first verdict landed 35 minutes
        # after admission and the event was over by then.
        cn = conn()
        watch(cn, A1, "WIF", added_ts=T0)
        c.poll_token(cn, client(body(["9"], t0=T0 - 3000)), cfg(),
                     address=A1, symbol="WIF", now=T0)
        out = c.rate_tokens(cn, cfg(), T0 + 10, db.active_watchlist(cn),
                            frozenset())
        self.assertIsNotNone(out[0][1])
        self.assertNotEqual(out[0][1].state, d.WARMING)

    def test_the_backfill_horizon_is_clamped_to_the_launch(self):
        # Windows before the token existed are not quiet, they are not
        # applicable. Counting them as silence flatters every later multiplier.
        cn = conn()
        launch = T0 - 600
        watch(cn, A1, "WIF", added_ts=launch)
        c.poll_token(cn, client(body(["9"])), cfg(), address=A1, symbol="WIF",
                     now=T0, launch_ts=launch)
        self.assertEqual(c.observed_from(cn, A1), launch)

    def test_the_horizon_is_set_once_and_never_moves(self):
        cn = conn()
        watch(cn, A1, "WIF")
        cl = client(body(["9"]), body(["8"]))
        c.poll_token(cn, cl, cfg(), address=A1, symbol="WIF", now=T0)
        first = c.observed_from(cn, A1)
        c.poll_token(cn, client(body(["8"])), cfg(), address=A1, symbol="WIF",
                     now=T0 + 3000)
        self.assertEqual(c.observed_from(cn, A1), first)

    def test_a_polled_token_asks_from_its_last_poll_less_the_overlap(self):
        cn = conn()
        watch(cn, A1, "WIF")
        polled(cn, ts=T0)
        self.assertEqual(c.since_for(cn, A1, cfg(overlap_s=30), T0 + 300),
                         T0 - 30)

    def test_since_time_reaches_the_wire(self):
        cn = conn()
        watch(cn, A1, "WIF")
        polled(cn, ts=T0)
        f = Fake(body(["9"]))
        c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                     address=A1, symbol="WIF", now=T0 + 300)
        self.assertIn(f"since_time:{T0 - 30}", f.queries[0])

    def test_the_overlap_is_what_stops_a_hole_at_the_seam(self):
        # Same post returned twice across two polls, stored once.
        cn = conn()
        watch(cn, A1, "WIF")
        f = Fake(body(["9", "8"]), body(["10", "9"]))
        cl = twitterapi.Client(KEY, transport=f)
        c.poll_token(cn, cl, cfg(), address=A1, symbol="WIF", now=T0)
        r2 = c.poll_token(cn, cl, cfg(), address=A1, symbol="WIF", now=T0 + 300)
        self.assertEqual(r2.posts, 2)          # paid for two
        self.assertEqual(r2.new_posts, 1)      # kept one
        self.assertEqual(cn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)

    def test_pagination_follows_the_cursor(self):
        cn = conn()
        watch(cn, A1, "WIF")
        f = Fake(body(["9", "8"], cursor="p2"), body(["7", "6"]))
        r = c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                         address=A1, symbol="WIF", now=T0)
        self.assertEqual(f.calls[1]["cursor"], "p2")
        self.assertEqual((r.requests, r.posts), (2, 4))

    def test_a_page_without_has_next_page_ends_the_walk(self):
        cn = conn()
        watch(cn, A1, "WIF")
        f = Fake(body(["9", "8"]), body(["7", "6"]))
        c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                     address=A1, symbol="WIF", now=T0)
        self.assertEqual(len(f.calls), 1)

    def test_the_walk_is_capped(self):
        cn = conn()
        watch(cn, A1, "WIF")
        pages = [body([str(i)], cursor="more") for i in range(30)]
        f = Fake(*pages)
        r = c.poll_token(cn, twitterapi.Client(KEY, transport=f),
                         cfg(max_pages_per_poll=3),
                         address=A1, symbol="WIF", now=T0)
        self.assertEqual(len(f.calls), 3)
        self.assertTrue(r.truncated)


# --- storage ----------------------------------------------------------------

class TestAttribution(unittest.TestCase):
    """$TER returned 37 posts and none were about our token: TER is Teradyne,
    on the NASDAQ. Counting those would make every ratio downstream a statement
    about somebody else's asset."""

    def test_the_address_always_counts(self):
        from trace import attribution as at
        self.assertEqual(at.match(f"gm {A1.upper()} sending", A1, "WIF"),
                         at.BY_ADDRESS)

    def test_a_bare_cashtag_does_not_count(self):
        from trace import attribution as at
        self.assertIsNone(at.match("Teradyne, Inc.(TER) 엣지 검증 $TER", A1, "TER"))
        self.assertIsNone(at.match("If you hold these: $STAG $BLUE $ORA", A1, "STAG"))

    def test_a_cashtag_plus_the_chain_counts(self):
        from trace import attribution as at
        self.assertEqual(at.match("Fork $FORK on Robinhood CA: 0xdead", A1, "Fork"),
                         at.BY_CASHTAG_AND_CHAIN)

    def test_a_post_naming_someone_elses_contract_never_counts(self):
        # The first post this project ever stored. Cashtag ours, chain ours,
        # contract somebody else's.
        from trace import attribution as at
        text = ("PumpFun added Robinhood chain launches and streams\n"
                "$ROBINHOOD is one of the first to go live\n"
                "0x53d2e1225AaCe4551194eCBB6bbE5c44d94AA5fa")
        self.assertIsNone(at.match(text, A1, "ROBINHOOD"))

    def test_the_veto_does_not_fire_when_our_contract_is_the_one_named(self):
        from trace import attribution as at
        text = f"$WIF on Robinhood Chain CA: {A1}"
        self.assertEqual(at.match(text, A1, "WIF"), at.BY_ADDRESS)

    def test_reattribution_is_idempotent_and_fixes_old_rows(self):
        cn = conn()
        watch(cn, A1, "WIF")
        cn.execute("""INSERT INTO posts (post_id, token_address, author,
                                         created_at, text, ingested_at, matched)
                      VALUES (?,?,?,?,?,?,?)""",
                   ("p1", A1, "x", T0, f"gm {A1}", T0, None))   # stored stale
        self.assertEqual(c.reattribute(cn, db.active_watchlist(cn)), 1)
        self.assertEqual(c.reattribute(cn, db.active_watchlist(cn)), 0)
        self.assertEqual(
            cn.execute("SELECT matched FROM posts").fetchone()[0], "address")

    def test_someone_elses_contract_is_spotted(self):
        from trace import attribution as at
        other = "0x53d2e1225AaCe4551194eCBB6bbE5c44d94AA5fa"
        self.assertEqual(at.other_addresses(f"$ROBINHOOD live {other}", A1),
                         [other])

    def test_unattributed_posts_are_stored_but_not_counted(self):
        # We paid for them. Evidence is worth keeping; a count is not.
        cn = conn()
        watch(cn, A1, "TER")
        f = Fake(body(["9", "8"], texts={"9": "Teradyne (TER) $TER report",
                                         "8": "$TER 엣지 검증"}))
        r = c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                         address=A1, symbol="TER", now=T0)
        self.assertEqual((r.new_posts, r.counted), (2, 0))
        self.assertEqual(cn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 2)
        self.assertEqual(d.counts_for(cn, A1, T0 - W, T0 + W), {})

    def test_a_mixed_page_counts_only_what_it_should(self):
        cn = conn()
        watch(cn, A1, "TER")
        f = Fake(body(["9", "8", "7"], texts={"9": f"ours {A1}",
                                              "8": "$TER on Robinhood Chain",
                                              "7": "Teradyne (TER) earnings"}))
        r = c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                         address=A1, symbol="TER", now=T0)
        self.assertEqual((r.new_posts, r.counted), (3, 2))


class TestStorage(unittest.TestCase):
    def test_posts_are_stored_once(self):
        cn = conn()
        watch(cn, A1, "WIF")
        cl = client(body(["9", "8"]), body(["9", "8"]))
        r1 = c.poll_token(cn, cl, cfg(), address=A1, symbol="WIF", now=T0)
        cl2 = client(body(["9", "8"]))
        r2 = c.poll_token(cn, cl2, cfg(), address=A1, symbol="WIF", now=T0 + 300)
        self.assertEqual((r1.new_posts, r2.new_posts), (2, 0))
        self.assertEqual(cn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 2)

    def test_authors_are_lowercased(self):
        cn = conn()
        watch(cn, A1, "WIF")
        c.poll_token(cn, client(body(["9"], authors={"9": "cobie"})), cfg(),
                     address=A1, symbol="WIF", now=T0)
        self.assertEqual(cn.execute("SELECT author FROM posts").fetchone()[0],
                         "cobie")

    def test_minute_counts_reach_the_detector(self):
        cn = conn()
        watch(cn, A1, "WIF")
        c.poll_token(cn, client(body([str(i) for i in range(9)], t0=T0 + 60)),
                     cfg(), address=A1, symbol="WIF", now=T0)
        self.assertEqual(d.counts_for(cn, A1, T0 - W, T0 + W).get(T0), 9)

    def test_every_request_is_written_to_the_paper_trail(self):
        cn = conn()
        watch(cn, A1, "WIF")
        f = Fake(body(["9", "8"], cursor="p2"), body(["7"]))
        c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                     address=A1, symbol="WIF", now=T0)
        rows = cn.execute("SELECT posts_returned, est_cost_usd FROM reads "
                          "ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [2, 1])
        self.assertAlmostEqual(rows[0][1], 30 / 100_000)   # 2 posts x 15 credits
        self.assertAlmostEqual(rows[1][1], 15 / 100_000)   # 1 post, at the floor


# --- money ------------------------------------------------------------------

class TestCredits(unittest.TestCase):
    def test_a_post_costs_fifteen_credits(self):
        self.assertAlmostEqual(twitterapi.Client.credits_for(100), 1500)
        self.assertAlmostEqual(twitterapi.Client(KEY).cost_usd(1000), 0.15)

    def test_an_empty_call_still_costs_the_floor(self):
        # This is the number that shapes the schedule: polling is not free.
        self.assertEqual(twitterapi.Client.credits_for(0), 15)
        self.assertAlmostEqual(twitterapi.Client(KEY).cost_usd(0), 0.00015)

    def test_thirty_three_times_cheaper_than_the_official_api(self):
        official = xapi.Client("A" * 80).cost_usd(1000)
        third_party = twitterapi.Client(KEY).cost_usd(1000)
        self.assertAlmostEqual(official, 5.00)
        self.assertAlmostEqual(third_party, 0.15)
        self.assertGreater(official / third_party, 30)

    def test_an_empty_poll_is_still_recorded_and_billed(self):
        cn = conn()
        watch(cn, A1, "WIF")
        r = c.poll_token(cn, client(body([])), cfg(),
                         address=A1, symbol="WIF", now=T0)
        self.assertEqual(r.posts, 0)
        self.assertAlmostEqual(r.cost_usd, 0.00015)
        self.assertAlmostEqual(c.spend_today(cn, T0), 0.00015)


class TestBudget(unittest.TestCase):
    def test_no_request_is_made_when_it_could_overshoot(self):
        # The check prices the WORST case of the next request, not the average.
        cn = conn()
        watch(cn, A1, "WIF")
        f = Fake(body(["9"]))
        with self.assertRaises(c.BudgetStop):
            c.poll_token(cn, twitterapi.Client(KEY, transport=f),
                         cfg(daily_budget_usd=0.001),   # a full page costs 0.003
                         address=A1, symbol="WIF", now=T0)
        self.assertEqual(f.calls, [])

    def test_budget_stops_mid_pagination_without_moving_the_cursor(self):
        # We paid for page one and kept it. The cursor stays put, so the next
        # run refetches the gap instead of leaving a hole in a baseline.
        cn = conn()
        watch(cn, A1, "WIF")
        polled(cn, ts=T0 - 600)
        page = body([str(i) for i in range(20)], cursor="p2")
        f = Fake(page, page)
        with self.assertRaises(c.BudgetStop):
            c.poll_token(cn, twitterapi.Client(KEY, transport=f),
                         cfg(daily_budget_usd=0.004),
                         address=A1, symbol="WIF", now=T0)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(c.read_cursor(cn, A1)[1], T0 - 600)   # did not move
        self.assertAlmostEqual(c.spend_today(cn, T0), 0.003)

    def test_a_round_stops_and_says_so(self):
        cn = conn()
        watch(cn, A1, "WIF")
        watch(cn, A2, "BONK")
        full = body([str(i) for i in range(20)])
        res = c.run_once(cn, client(full, full), cfg(daily_budget_usd=0.004),
                         now=T0)
        self.assertEqual(len(res.polls), 1)
        self.assertIn("budget", res.stopped)

    def test_spend_resets_on_the_next_day(self):
        cn = conn()
        watch(cn, A1, "WIF")
        c.poll_token(cn, client(body(["1", "2"])), cfg(),
                     address=A1, symbol="WIF", now=T0)
        self.assertAlmostEqual(c.spend_today(cn, T0 + 86400), 0.0)


# --- the schedule -----------------------------------------------------------

class TestSchedule(unittest.TestCase):
    def test_the_default_wake_lands_just_after_a_window_closes(self):
        # Polling faster cannot change a verdict — the detector only rates a
        # window that has already closed — and every call costs the floor.
        self.assertEqual(c.next_wake(T0, cfg(poll_delay_s=20)), W + 20)
        self.assertEqual(c.next_wake(T0 + 100, cfg(poll_delay_s=20)), 200 + 20)

    def test_an_explicit_interval_wins(self):
        self.assertEqual(c.next_wake(T0 + 7, cfg(poll_interval_s=60)), 60)

    def test_cadence_is_the_window_not_the_distance_to_the_next_one(self):
        # next_wake shrinks as the window runs out; using it to extrapolate a
        # daily bill overstates it, sometimes fivefold.
        self.assertEqual(c.cadence_s(cfg()), W)
        self.assertEqual(c.cadence_s(cfg(poll_interval_s=60)), 60)

    def test_once_per_window_is_five_times_cheaper_than_once_a_minute(self):
        floor = twitterapi.Client(KEY).cost_usd(0) * 10       # ten tokens
        per_window = floor * 86400 / c.cadence_s(cfg())
        per_minute = floor * 86400 / 60
        self.assertAlmostEqual(per_minute, 2.16, places=2)
        self.assertLess(per_window, per_minute / 4)


# --- failures ---------------------------------------------------------------

class TestFailures(unittest.TestCase):
    def test_bad_key_raises_and_is_not_retried(self):
        f = Fake((401, {}, {"msg": "invalid api key"}))
        with self.assertRaises(feed.AuthError):
            twitterapi.Client(KEY, transport=f).search("q")
        self.assertEqual(len(f.calls), 1)

    def test_out_of_credit_is_an_auth_error_not_a_retry(self):
        f = Fake((402, {}, {"msg": "insufficient credits"}))
        with self.assertRaises(feed.AuthError):
            twitterapi.Client(KEY, transport=f).search("q")

    def test_the_key_goes_in_the_x_api_key_header(self):
        f = Fake(body([]))
        twitterapi.Client(KEY, transport=f).search("q")
        self.assertEqual(f.headers_sent["X-API-Key"], KEY)
        self.assertNotIn("Authorization", f.headers_sent)

    def test_rate_limit_carries_its_reset_time(self):
        f = Fake((429, {"x-rate-limit-reset": str(T0 + 90)}, {}))
        with self.assertRaises(feed.RateLimited) as cm:
            twitterapi.Client(KEY, transport=f).search("q")
        self.assertEqual(cm.exception.reset_ts, T0 + 90)

    def test_rate_limit_does_not_move_the_cursor(self):
        cn = conn()
        watch(cn, A1, "WIF")
        c.poll_token(cn, client(body(["9"])), cfg(),
                     address=A1, symbol="WIF", now=T0)
        f = Fake((429, {"x-rate-limit-reset": str(T0 + 90)}, {}))
        with self.assertRaises(feed.RateLimited):
            c.poll_token(cn, twitterapi.Client(KEY, transport=f), cfg(),
                         address=A1, symbol="WIF", now=T0 + 300)
        self.assertEqual(c.read_cursor(cn, A1)[1], T0)

    def test_a_dropped_network_is_weather_not_a_crash(self):
        # The collector is meant to run unattended for hours. Without this it
        # is one closed laptop lid away from dying with a stack trace.
        slept, calls = [], []
        def flaky(url, params, headers):
            calls.append(1)
            if len(calls) < 3:
                raise feed.TransportError("ConnectionError: network is down")
            return 200, {}, body(["9"])
        cl = twitterapi.Client(KEY, transport=flaky, sleep=slept.append)
        self.assertEqual(cl.search("q").count, 1)
        self.assertEqual(slept, [2, 4])

    def test_a_network_that_never_comes_back_still_gives_up(self):
        slept = []
        def dead(url, params, headers):
            raise feed.TransportError("ConnectionError")
        cl = twitterapi.Client(KEY, transport=dead, max_retries=2,
                               sleep=slept.append)
        with self.assertRaises(feed.TransportError):
            cl.search("q")

    def test_one_token_failing_does_not_stop_the_round(self):
        cn = conn()
        watch(cn, A1, "WIF")
        watch(cn, A2, "BONK")
        calls = []
        def one_bad(url, params, headers):
            calls.append(1)
            if len(calls) == 1:
                return 500, {}, {}
            return 200, {}, body(["9"], addr=A2)
        res = c.run_once(cn, twitterapi.Client(KEY, transport=one_bad,
                                               max_retries=0, sleep=lambda s: None),
                         cfg(), now=T0)
        self.assertEqual(len(res.polls), 2)
        self.assertIsNotNone(res.polls[0].error)
        self.assertIsNone(res.stopped)

    def test_server_error_is_retried_then_raises(self):
        slept = []
        f = Fake((503, {}, {}), (503, {}, {}), (503, {}, {}), (503, {}, {}))
        cl = twitterapi.Client(KEY, transport=f, max_retries=3, sleep=slept.append)
        with self.assertRaises(feed.FeedError):
            cl.search("q")
        self.assertEqual(len(f.calls), 4)
        self.assertEqual(slept, [2, 4, 8])

    def test_undated_posts_are_skipped_not_guessed(self):
        f = Fake((200, {}, {"tweets": [{"id": "9", "text": "x"}],
                            "has_next_page": False}))
        self.assertEqual(twitterapi.Client(KEY, transport=f).search("q").count, 0)

    def test_client_never_prints_the_key(self):
        cl = twitterapi.Client("new1_supersecretvalue_0000", transport=Fake())
        self.assertNotIn("supersecret", repr(cl))
        self.assertNotIn("supersecret", str(cl))

    def test_empty_key_is_refused_up_front(self):
        with self.assertRaises(feed.AuthError):
            twitterapi.Client("")

    def test_a_pasted_placeholder_is_refused_with_a_readable_message(self):
        # The real failure: the chat placeholder was pasted whole, and requests
        # answered with "latin-1 codec can't encode characters in position
        # 8-11", which is true and useless.
        for bad in ("new1_...свой_ключ", "new1_...your_key_here", "<KEY>"):
            with self.subTest(key=bad):
                with self.assertRaises(feed.AuthError) as cm:
                    twitterapi.Client(bad)
                self.assertIn("placeholder", str(cm.exception))

    def test_quotes_and_whitespace_around_a_key_are_forgiven(self):
        cl = twitterapi.Client("  'new1_09da0ec4d402404681abc'  ", transport=Fake())
        cl.search("q")
        self.assertEqual(cl._transport.headers_sent["X-API-Key"],
                         "new1_09da0ec4d402404681abc")

    def test_a_short_value_is_not_mistaken_for_an_x_bearer_token(self):
        # What actually happened: a twitterapi.io key was sent to api.x.com.
        self.assertFalse(xapi.looks_like_bearer("new1_09da0ec4d402404681"))
        self.assertFalse(xapi.looks_like_bearer("hunter2"))
        self.assertTrue(xapi.looks_like_bearer("A" * 80 + "%2F" + "b7-x_y.z~"))


# --- both providers, one interface ------------------------------------------

class TestProviders(unittest.TestCase):
    def test_the_collector_accepts_either_provider(self):
        for name in ("twitterapi.io", "x"):
            with self.subTest(provider=name):
                p = c.build_provider(cfg(provider=name), "A" * 80)
                self.assertEqual(p.name, name)

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(SystemExit):
            c.build_provider(cfg(provider="nitter"), "k")

    def test_the_official_client_speaks_the_same_page(self):
        def transport(url, params, headers):
            return 200, {}, {"data": [{"id": "9", "author_id": "u1",
                                       "created_at": "2026-09-09T12:00:00.000Z",
                                       "text": "gm"}],
                             "includes": {"users": [{"id": "u1",
                                                     "username": "COBIE"}]},
                             "meta": {"result_count": 1, "newest_id": "9"}}
        page = xapi.Client("A" * 80, transport=transport).search("q", since_ts=T0)
        self.assertEqual(page.count, 1)
        self.assertEqual(page.posts[0].author, "cobie")
        self.assertIsNone(page.next_cursor)

    def test_the_official_client_sends_start_time_not_a_raw_timestamp(self):
        seen = {}
        def transport(url, params, headers):
            seen.update(params)
            return 200, {}, {"meta": {"result_count": 0}}
        xapi.Client("A" * 80, transport=transport).search("q", since_ts=T0)
        self.assertEqual(seen["start_time"], xapi.rfc3339(T0))


# --- normal, by age ---------------------------------------------------------

class TestCohort(unittest.TestCase):
    """A three-minute-old memecoin has no own normal.

    Measured on live launches: the first post lands two to four minutes after
    deployment, the whole event is over inside fifteen, and the detector needs
    thirty-five minutes of the token's own history before it may speak. The two
    never overlap, so the reference changes from "what this token usually gets"
    to "what a token this old usually gets" - one global curve, no per-token
    tuning, still a ratio against a measured denominator.
    """

    def curve(self, cn, addr=A1, launch=None, posts=(), observed_h=4):
        launch = launch if launch is not None else T0
        db.upsert_token(cn, address=addr, symbol="WIF", name=None, decimals=None,
                        launch_block=0, launch_ts=launch, related=(), now_ts=launch)
        db.add_to_watchlist(cn, addr, "pin", launch, "test")
        for i, (offset, n) in enumerate(posts):
            for k in range(n):
                cn.execute("""INSERT INTO posts (post_id, token_address, author,
                                                 created_at, text, ingested_at,
                                                 matched)
                              VALUES (?,?,?,?,?,?,?)""",
                           (f"c{addr[-4:]}{i}_{k}", addr, "a",
                            launch + offset + k, "",
                            launch, "address"))
        c.write_cursor(cn, addr, None, launch + observed_h * 3600, launch)
        return cohort.build(cn)

    def test_buckets_cover_the_first_hours_finely_and_the_rest_coarsely(self):
        self.assertEqual(cohort.bucket_for(0), (0, 300))
        self.assertEqual(cohort.bucket_for(700), (600, 1200))
        self.assertEqual(cohort.bucket_for(10 ** 6), (9600, 1 << 31))
        self.assertIsNone(cohort.bucket_for(-5))

    def test_a_thin_bucket_refuses_to_say_what_normal_is(self):
        cur = cohort.Curve(posts={(0, 300): 40}, windows={(0, 300): 3})
        self.assertIsNone(cur.mean((0, 300)))          # 3 windows is not a curve
        cur = cohort.Curve(posts={(0, 300): 40},
                           windows={(0, 300): cohort.MIN_WINDOWS})
        self.assertAlmostEqual(cur.mean((0, 300)), 40 / cohort.MIN_WINDOWS)

    def test_windows_before_the_launch_are_not_counted_as_quiet(self):
        cn = conn()
        cur = self.curve(cn, launch=T0, posts=[(60, 5)])
        # nothing before T0 contributes: the token did not exist
        self.assertEqual(cur.windows.get((0, 300)), 1)

    def test_the_curve_measures_posts_per_window(self):
        cn = conn()
        cur = self.curve(cn, posts=[(700, 8)], observed_h=1)
        self.assertEqual(cur.posts[(600, 1200)], 8)
        self.assertEqual(cur.windows[(600, 1200)], 2)

    def test_the_curve_is_one_curve_for_every_token(self):
        # No per-token tuning, ever. Two tokens, one shared denominator.
        cn = conn()
        self.curve(cn, addr=A1, launch=T0, posts=[(700, 6)], observed_h=1)
        self.curve(cn, addr=A2, launch=T0, posts=[(700, 2)], observed_h=1)
        cur = cohort.build(cn)
        self.assertEqual(cur.posts[(600, 1200)], 8)
        self.assertEqual(cur.windows[(600, 1200)], 4)


class TestCohortInTheDetector(unittest.TestCase):
    def counts(self, rate):
        return {T0 + 6 * W: rate}

    def test_a_young_token_is_rated_against_the_cohort(self):
        v = d.evaluate(counts=self.counts(14), now=T0 + 7 * W,
                       watch_start=T0 + 6 * W, cohort_baseline=1.55)
        self.assertEqual(v.baseline_kind, "cohort")
        self.assertAlmostEqual(v.multiplier, 14 / 1.55, places=2)
        self.assertEqual(v.state, d.SPIKE)

    def test_without_a_cohort_a_young_token_is_still_warming(self):
        v = d.evaluate(counts=self.counts(14), now=T0 + 7 * W,
                       watch_start=T0 + 6 * W, cohort_baseline=None)
        self.assertEqual(v.state, d.WARMING)
        self.assertIsNone(v.baseline_kind)

    def test_its_own_history_wins_once_it_has_one(self):
        counts = {T0 + k * W: 4 for k in range(7)}
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0,
                       cohort_baseline=0.05)
        self.assertEqual(v.baseline_kind, "own")
        self.assertAlmostEqual(v.baseline, 4.0)

    def test_the_cohort_never_rescues_an_unobserved_window(self):
        # The rated window itself must have been seen. Nothing substitutes for
        # that - the cohort replaces the baseline, not the observation.
        v = d.evaluate(counts=self.counts(14), now=T0 + 7 * W,
                       watch_start=T0 + 7 * W, cohort_baseline=1.55)
        self.assertEqual(v.state, d.WARMING)


# --- the collector's half of the history rule -------------------------------

class TestRating(unittest.TestCase):
    def test_history_begins_where_we_actually_asked_not_where_we_were_added(self):
        """A token can sit on the watchlist long before the collector runs.

        Those windows are not silence, they are windows nobody watched, and
        treating them as zeros would make the first real post an infinite
        multiplier. History begins at the earliest moment we put a question to
        the feed — which, thanks to the backfill, is an hour before the first
        poll rather than at it.
        """
        cn = conn()
        watch(cn, A1, "WIF", added_ts=T0 - 400 * W)      # admitted long ago
        c.poll_token(cn, client(body(["9"])), cfg(backfill_min=60),
                     address=A1, symbol="WIF", now=T0)
        self.assertEqual(c.watch_start_for(cn, A1, T0 - 400 * W), T0 - 3600)

    def test_a_token_never_polled_is_not_rated_at_all(self):
        cn = conn()
        watch(cn, A1, "WIF", added_ts=T0 - 40 * W)
        self.assertIsNone(c.watch_start_for(cn, A1, T0 - 40 * W))
        self.assertIsNone(c.observed_from(cn, A1))
        out = c.rate_tokens(cn, cfg(), T0 + 10, db.active_watchlist(cn), frozenset())
        self.assertIsNone(out[0][1])


    def test_a_half_collected_window_is_not_rated(self):
        cn = conn()
        watch(cn, A1, "WIF", added_ts=T0 - 20 * W)
        polled(cn, ts=T0 - 2 * W)
        out = c.rate_tokens(cn, cfg(), T0 + 10, db.active_watchlist(cn), frozenset())
        self.assertIsNone(out[0][1])

    def test_a_collected_window_is_rated(self):
        cn = conn()
        watch(cn, A1, "WIF", added_ts=T0 - 20 * W)
        polled(cn, ts=T0 + 5)
        out = c.rate_tokens(cn, cfg(), T0 + 10, db.active_watchlist(cn), frozenset())
        self.assertIsNotNone(out[0][1])


# --- the config must not quietly differ from the detector -------------------

class TestConfigMatchesTheDetector(unittest.TestCase):
    """The site publishes these numbers. The detector takes them as arguments,
    which is what makes it testable — and also what lets a config file feed the
    live collector a rule no test ever checked. This is that guard."""

    def test_shipped_config_matches_detector_defaults(self):
        p, cd = d.DEFAULTS, _config.Config().detector
        self.assertEqual(cd.rate_window_min * 60, p.window_s)
        self.assertEqual(cd.baseline_windows, p.baseline_windows)
        self.assertEqual(cd.floor, p.floor)
        self.assertEqual(cd.cooldown_min * 60, p.cooldown_s)
        self.assertEqual(cd.spike_multiplier, p.spike_x)
        self.assertEqual(cd.warm_multiplier, p.warm_x)
        self.assertEqual(cd.min_baseline, p.baseline_min)

    def test_example_toml_matches_detector_defaults(self):
        cd = _config.load("config.example.toml").detector
        self.assertEqual(cd.baseline_windows, d.DEFAULTS.baseline_windows)
        self.assertEqual(cd.floor, d.DEFAULTS.floor)

    def test_params_from_config_equal_the_detector_defaults(self):
        self.assertEqual(c.params_from(_config.Config()), d.DEFAULTS)

    def test_first_verdict_lands_35_minutes_after_admission(self):
        p = d.DEFAULTS
        self.assertEqual((p.baseline_windows + 1) * p.window_s, 35 * 60)


# --- odds and ends ----------------------------------------------------------

class TestParsing(unittest.TestCase):
    def test_twitter_legacy_dates(self):
        self.assertEqual(feed.parse_created_at("Tue Dec 10 07:00:30 +0000 2024"),
                         int(datetime(2024, 12, 10, 7, 0, 30,
                                      tzinfo=timezone.utc).timestamp()))

    def test_rfc3339_with_a_z(self):
        self.assertEqual(feed.parse_created_at("2026-09-09T12:00:00.000Z"),
                         int(datetime(2026, 9, 9, 12, 0,
                                      tzinfo=timezone.utc).timestamp()))

    def test_the_probe_query_cannot_match_a_real_post(self):
        f = Fake(body([]))
        twitterapi.Client(KEY, transport=f).verify()
        self.assertTrue(f.queries[0].startswith('"tracekeycheck'))
        self.assertNotIn("OR", f.queries[0])


if __name__ == "__main__":
    unittest.main()


# --- the second lane: money behind the talking ------------------------------

class TestMoneyBehindIt(unittest.TestCase):
    """pump.fun carries Robinhood Chain and shows each caller's position.

    The callouts themselves are X posts the collector already reads, so they are
    not a second source. The position is the new thing — and because pump.fun
    *pays* people to call coins out, it rides beside the verdict and never
    inside it.
    """

    def setUp(self):
        from trace import callouts
        self.co = callouts

    def test_their_money_strings_become_numbers(self):
        self.assertEqual(self.co.money("$10.3k"), 10300)
        self.assertEqual(self.co.money("$1.7M"), 1_700_000)
        self.assertAlmostEqual(self.co.money("-$3,979.78"), -3979.78)
        self.assertIsNone(self.co.money("not a number"))

    def test_a_caller_with_no_stake_is_dropped(self):
        # That is the paid-callout noise this lane exists to ignore.
        cn = conn()
        n = self.co.ingest(cn, A1, [{"author": "@paid", "position": None},
                                    {"author": "@real", "position": "$500"}], T0)
        self.assertEqual(n, 1)

    def test_money_is_summed_over_the_accounts_that_posted(self):
        cn = conn()
        watch(cn, A1, "WIF")
        self.co.ingest(cn, A1, [{"author": "@alice", "position": "$6,258.33"},
                                {"author": "@bob", "position": "$4,504.96"},
                                {"author": "@carol", "position": "$99,999"}], T0)
        c.poll_token(cn, client(body(["9", "8"], authors={"9": "alice", "8": "bob"})),
                     cfg(), address=A1, symbol="WIF", now=T0)
        backed, money = self.co.money_in_window(cn, A1, T0 - W, T0 + W)
        self.assertEqual(backed, 2)                 # carol did not post
        self.assertAlmostEqual(money, 6258.33 + 4504.96, places=2)

    def test_an_uncounted_post_cannot_drag_its_author_in(self):
        cn = conn()
        watch(cn, A1, "TER")
        self.co.ingest(cn, A1, [{"author": "@alice", "position": "$500"}], T0)
        c.poll_token(cn, client(body(["9"], authors={"9": "alice"},
                                     texts={"9": "Teradyne (TER) $TER earnings"})),
                     cfg(), address=A1, symbol="TER", now=T0)
        self.assertEqual(self.co.money_in_window(cn, A1, T0 - W, T0 + W), (0, 0.0))

    def test_money_never_changes_a_verdict(self):
        """The same guarantee accounts.txt has, for a sharper reason: this
        number can be bought."""
        counts = {T0 + k * W: 2 for k in range(6)}
        counts[T0 + 6 * W] = 9
        poor = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        rich = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0,
                          backed=40, money_usd=9_000_000.0)
        self.assertEqual(poor.state, rich.state)
        self.assertEqual(poor.multiplier, rich.multiplier)
        self.assertEqual(poor.rate, rich.rate)
        self.assertEqual(rich.money_usd, 9_000_000.0)   # reported, not applied

    def test_the_source_is_always_recorded(self):
        # These numbers do not come from an API anyone can run themselves.
        cn = conn()
        self.co.ingest(cn, A1, [{"author": "@a", "position": "$1"}], T0)
        src = cn.execute("SELECT source FROM positions").fetchone()[0]
        self.assertEqual(src, "pumpfun")
