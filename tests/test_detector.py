"""Stage 3, tested without a network and without a clock.

This is the test that must never break. Every case below is a rule the
product states in public on the site, so a failure here is not a bug in a
helper, it is the product saying something untrue.

Deliberately no import of trace.config: the detector takes its parameters as
arguments, so these tests run on a bare interpreter with nothing installed.
"""

import unittest

from trace import db
from trace import detector as d

W = 300
T0 = (1_780_000_000 // W) * W          # aligned to a window boundary
ADDR = "0xaaaa000000000000000000000000000000000001"


def conn():
    return db.connect(":memory:")


def seed(c, plan, *, t0=T0, addr=ADDR, authors=None):
    """plan: {window index: number of posts}. Posts land one second apart
    inside their window, so every post is unambiguously in one window."""
    authors = authors or {}
    pid = 0
    for k in sorted(plan):
        for i in range(plan[k]):
            pid += 1
            who = authors.get(k, "anon") if i == 0 else "anon"
            c.execute(
                """INSERT INTO posts (post_id, token_address, author, created_at,
                                      text, ingested_at, matched)
                   VALUES (?,?,?,?,?,?,?)""",
                # 'address' = this post provably names this token. The seeded
                # posts here stand in for counted ones; trace/attribution.py is
                # what decides that for real posts, and is tested separately.
                (f"p{pid:05d}", addr, who, t0 + k * W + i, "", t0, "address"))
    d.rebuild_minutes(c, addr)
    return c


def flat(n, upto):
    return {k: n for k in range(upto)}


# ---------------------------------------------------------------- windows ---

class TestWindows(unittest.TestCase):
    def test_alignment_is_epoch_not_first_post(self):
        self.assertEqual(d.window_start(T0), T0)
        self.assertEqual(d.window_start(T0 + 299), T0)
        self.assertEqual(d.window_start(T0 + 300), T0 + 300)

    def test_boundary_post_belongs_to_the_later_window(self):
        c = conn()
        for pid, ts in (("a", T0 + W - 1), ("b", T0 + W)):
            c.execute("""INSERT INTO posts (post_id, token_address, author,
                                            created_at, text, ingested_at,
                                            matched)
                         VALUES (?,?,?,?,?,?,?)""",
                      (pid, ADDR, "x", ts, "", T0, "address"))
        d.rebuild_minutes(c, ADDR)
        counts = d.counts_for(c, ADDR, T0, T0 + 2 * W)
        self.assertEqual(counts.get(T0), 1)
        self.assertEqual(counts.get(T0 + W), 1)

    def test_counts_by_window_matches_storage(self):
        times = [T0 + 5, T0 + 299, T0 + 300, T0 + 901]
        self.assertEqual(d.counts_by_window(times),
                         {T0: 2, T0 + W: 1, T0 + 3 * W: 1})


# ---------------------------------------------------------------- warm-up ---

class TestWarmup(unittest.TestCase):
    def test_warming_until_six_full_baseline_windows(self):
        counts = {T0 + k * W: 3 for k in range(12)}
        for k in range(6):
            v = d.evaluate(counts=counts, now=T0 + (k + 1) * W, watch_start=T0)
            self.assertEqual(v.state, d.WARMING, f"window {k} should still warm")
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        self.assertNotEqual(v.state, d.WARMING)

    def test_first_verdict_lands_35_minutes_after_admission(self):
        counts = {T0 + k * W: 3 for k in range(12)}
        warm = d.evaluate(counts=counts, now=T0 + 2040, watch_start=T0)   # 34 min
        live = d.evaluate(counts=counts, now=T0 + 2100, watch_start=T0)   # 35 min
        self.assertEqual(warm.state, d.WARMING)
        self.assertNotEqual(live.state, d.WARMING)

    def test_a_huge_spike_during_warmup_never_fires(self):
        counts = {T0: 2, T0 + W: 2, T0 + 2 * W: 400}
        v = d.evaluate(counts=counts, now=T0 + 3 * W, watch_start=T0)
        self.assertEqual(v.state, d.WARMING)

    def test_unobserved_history_is_missing_not_zero(self):
        """Same counts, two admission times. The token admitted a moment ago
        is WARMING even though the data exists - we did not see those windows,
        so they are not zeros we may average."""
        counts = {T0 + k * W: 3 for k in range(12)}
        counts[T0 + 11 * W] = 40
        now = T0 + 12 * W
        old = d.evaluate(counts=counts, now=now, watch_start=T0)
        new = d.evaluate(counts=counts, now=now, watch_start=now - 2 * W)
        self.assertEqual(old.state, d.SPIKE)
        self.assertEqual(new.state, d.WARMING)

    def test_observed_but_empty_windows_do_count(self):
        """Silence is data. A token watched for an hour with nothing said
        about it has a baseline of zero, not a missing baseline."""
        counts = {T0 + 11 * W: 10}
        v = d.evaluate(counts=counts, now=T0 + 12 * W, watch_start=T0)
        self.assertEqual(v.baseline, 0.0)
        self.assertEqual(v.multiplier, 10.0)     # clamped divisor of 1.0
        self.assertEqual(v.state, d.SPIKE)


# --------------------------------------------------------------- the math ---

class TestMath(unittest.TestCase):
    def base(self, rate, baseline_per_window=3, watch=T0):
        counts = {T0 + k * W: baseline_per_window for k in range(6)}
        counts[T0 + 6 * W] = rate
        return d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=watch)

    def test_baseline_is_the_mean_of_the_previous_six(self):
        counts = {T0 + k * W: v for k, v in
                  enumerate([1, 2, 3, 4, 5, 6, 99])}
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        self.assertAlmostEqual(v.baseline, 21 / 6)
        self.assertEqual(v.rate, 99)

    def test_current_window_is_excluded_from_its_own_baseline(self):
        counts = {T0 + k * W: 3 for k in range(6)}
        counts[T0 + 6 * W] = 60
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        self.assertAlmostEqual(v.baseline, 3.0)

    def test_quiet_below_one_and_a_half(self):
        self.assertEqual(self.base(4).state, d.QUIET)

    def test_warm_between_one_and_a_half_and_three(self):
        v = self.base(6)
        self.assertEqual(v.state, d.WARM)
        self.assertAlmostEqual(v.multiplier, 2.0)

    def test_spike_at_three_times_and_above_the_floor(self):
        v = self.base(40)
        self.assertEqual(v.state, d.SPIKE)
        self.assertAlmostEqual(v.multiplier, 40 / 3)

    def test_the_floor_is_what_stops_one_post_becoming_four(self):
        """1 post -> 4 posts is a 4x multiplier and means nothing."""
        counts = {T0 + k * W: 1 for k in range(6)}
        counts[T0 + 6 * W] = 4
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        self.assertAlmostEqual(v.multiplier, 4.0)
        self.assertEqual(v.rate, 4)
        self.assertNotEqual(v.state, d.SPIKE)

    def test_the_boundary_is_inclusive_on_both_thresholds(self):
        """Exactly 3.0x with exactly the floor fires. One less of either
        does not. Both edges are stated on the site, so both are tested."""
        def at(rate, baseline_per_window):
            counts = {T0 + k * W: baseline_per_window for k in range(6)}
            counts[T0 + 6 * W] = rate
            return d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)

        # Written against the floor rather than against the number 8: the
        # floor is a calibration and it has already moved once, on live data.
        # What must never move is the meaning of the two boundaries.
        F = d.DEFAULTS.floor

        exact = at(3 * F, F)                  # exactly 3.0x, comfortably above
        self.assertAlmostEqual(exact.multiplier, 3.0)
        self.assertEqual(exact.state, d.SPIKE)

        under_x = at(3 * F - 1, F)            # a hair under 3.0x
        self.assertEqual(under_x.state, d.WARM)

        on_floor = at(F, 1)                   # rate exactly the floor
        self.assertEqual(on_floor.state, d.SPIKE)

        under_floor = at(F - 1, 1)            # one post short of it
        self.assertEqual(under_floor.state, d.WARM)

    def test_a_dead_baseline_cannot_manufacture_a_multiplier(self):
        rate = d.DEFAULTS.floor - 1
        counts = {T0 + k * W: 0 for k in range(6)}
        counts[T0 + 6 * W] = rate
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        self.assertEqual(v.multiplier, float(rate))   # baseline clamped to 1.0
        self.assertNotEqual(v.state, d.SPIKE)         # but still below the floor


# --------------------------------------------------------------- cooldown ---

class TestCooldown(unittest.TestCase):
    def test_a_sustained_spike_fires_once(self):
        c = seed(conn(), {**flat(12, 12), 12: 40, 13: 38, 14: 36, 15: 34})
        out = d.replay(c, ADDR, T0, T0 + 16 * W, watch_start=T0)
        self.assertEqual(sum(1 for v in out if v.fired), 1)

    def test_it_can_fire_again_once_the_cooldown_expires(self):
        plan = {**flat(3, 12), 12: 40}
        plan.update({k: 3 for k in range(13, 20)})
        plan[20] = 45
        plan.update({k: 3 for k in range(21, 24)})
        c = seed(conn(), plan)
        out = d.replay(c, ADDR, T0, T0 + 24 * W, watch_start=T0)
        fired = [v.window for v in out if v.fired]
        self.assertEqual(fired, [T0 + 12 * W, T0 + 20 * W])

    def test_cooldown_is_thirty_minutes_exactly(self):
        counts = {T0 + k * W: 3 for k in range(6)}
        counts[T0 + 6 * W] = 40
        v = d.evaluate(counts=counts, now=T0 + 7 * W, watch_start=T0)
        self.assertEqual(v.cooldown_until, T0 + 7 * W + 1800)


# ------------------------------------------------------------- first post ---

class TestFirstPost(unittest.TestCase):
    def test_the_spike_reports_who_posted_first_in_that_window(self):
        c = seed(conn(), {**flat(3, 12), 12: 40},
                 authors={12: "lucidwiz"})
        out = d.replay(c, ADDR, T0, T0 + 13 * W, watch_start=T0)
        spike = [v for v in out if v.fired]
        self.assertEqual(len(spike), 1)
        self.assertEqual(spike[0].first_author, "lucidwiz")
        self.assertEqual(spike[0].first_post_ts, T0 + 12 * W)

    def test_no_first_post_is_reported_when_nothing_fired(self):
        c = seed(conn(), flat(3, 13), authors={12: "lucidwiz"})
        out = d.replay(c, ADDR, T0, T0 + 13 * W, watch_start=T0)
        self.assertTrue(all(v.first_author is None for v in out))


# ------------------------------------------------------- the recorded hour ---

RECORDED = {**{k: 3 for k in range(12)},
            12: 40, 13: 30, 14: 25,
            **{k: 3 for k in range(15, 20)},
            20: 45,
            **{k: 3 for k in range(21, 24)}}


class TestReplay(unittest.TestCase):
    def test_two_hours_produce_exactly_two_spikes(self):
        c = seed(conn(), RECORDED, authors={12: "lucidwiz", 20: "hanakoxbt"})
        out = d.replay(c, ADDR, T0, T0 + 24 * W, watch_start=T0)
        self.assertEqual([v.window for v in out if v.fired],
                         [T0 + 12 * W, T0 + 20 * W])
        self.assertEqual([v.first_author for v in out if v.fired],
                         ["lucidwiz", "hanakoxbt"])

    def test_states_in_order(self):
        c = seed(conn(), RECORDED)
        out = d.replay(c, ADDR, T0, T0 + 24 * W, watch_start=T0)
        self.assertEqual([v.state for v in out[:6]], [d.WARMING] * 6)
        self.assertEqual(out[12].state, d.SPIKE)
        self.assertEqual(out[13].state, d.WARM)      # still hot, in cooldown
        self.assertEqual(out[20].state, d.SPIKE)

    def test_the_same_recording_replays_identically(self):
        a = d.replay(seed(conn(), RECORDED), ADDR, T0, T0 + 24 * W, watch_start=T0)
        b = d.replay(seed(conn(), RECORDED), ADDR, T0, T0 + 24 * W, watch_start=T0)
        self.assertEqual(a, b)

    def test_a_quiet_token_never_fires_in_two_hours(self):
        c = seed(conn(), flat(4, 24))
        out = d.replay(c, ADDR, T0, T0 + 24 * W, watch_start=T0)
        self.assertEqual([v for v in out if v.fired], [])
        self.assertTrue(all(v.state in (d.WARMING, d.QUIET) for v in out))


# ------------------------------------------------------------ persistence ---

class TestStorage(unittest.TestCase):
    def test_tick_agrees_with_the_pure_function(self):
        c = seed(conn(), {**flat(3, 12), 12: 40})
        now = T0 + 13 * W
        stored = d.tick(c, ADDR, now, watch_start=T0)
        pure = d.evaluate(counts=d.counts_by_window(
            [r[0] for r in c.execute(
                "SELECT created_at FROM posts WHERE token_address=?", (ADDR,))]),
            now=now, watch_start=T0,
            first_post=d.first_post_in(c, ADDR, T0 + 12 * W, T0 + 13 * W))
        self.assertEqual(stored.state, pure.state)
        self.assertEqual(stored.rate, pure.rate)
        self.assertAlmostEqual(stored.baseline, pure.baseline)

    def test_state_is_written_and_cooldown_survives_the_next_tick(self):
        c = seed(conn(), {**flat(3, 12), 12: 40, 13: 38})
        d.tick(c, ADDR, T0 + 13 * W, watch_start=T0)
        row = c.execute("SELECT state, cooldown_until FROM token_state "
                        "WHERE token_address=?", (ADDR,)).fetchone()
        self.assertEqual(row[0], d.SPIKE)
        second = d.tick(c, ADDR, T0 + 14 * W, watch_start=T0)
        self.assertNotEqual(second.state, d.SPIKE)   # cooldown held

    def test_no_visitor_data_is_ever_written(self):
        """Constraint 4 is enforced by the schema having nowhere to put it."""
        c = conn()
        cols = {r[1] for t in ("posts", "minute_counts", "token_state")
                for r in c.execute(f"PRAGMA table_info({t})")}
        for banned in ("wallet", "user_address", "visitor", "ip", "session"):
            self.assertNotIn(banned, cols)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ------------------------------------------------------------- author count --

class TestAuthors(unittest.TestCase):
    """Forty posts from four accounts is not forty voices. The count never
    gates a verdict - it sits next to the rate so a reader can tell the
    difference the multiplier cannot."""

    def seed_authors(self, names):
        c = seed(conn(), {**flat(3, 12)})
        for i, who in enumerate(names):
            c.execute("""INSERT INTO posts (post_id, token_address, author,
                                            created_at, text, ingested_at,
                                            matched)
                         VALUES (?,?,?,?,?,?,?)""",
                      (f"z{i:04d}", ADDR, who, T0 + 12 * W + i, "", T0,
                       "address"))
        d.rebuild_minutes(c, ADDR)
        return c

    def test_many_posts_few_accounts(self):
        c = self.seed_authors(["a", "b", "a", "b", "a", "b", "a", "b", "a", "b"])
        v = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0)
        self.assertEqual(v.rate, 10)
        self.assertEqual(v.authors, 2)
        self.assertEqual(v.state, d.SPIKE)     # the count never blocks a call

    def test_many_posts_many_accounts(self):
        c = self.seed_authors([f"a{i}" for i in range(10)])
        v = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0)
        self.assertEqual(v.rate, 10)
        self.assertEqual(v.authors, 10)

    def test_authors_are_counted_per_window_not_overall(self):
        c = self.seed_authors(["a", "b", "c"])
        v = d.tick(c, ADDR, T0 + 14 * W, watch_start=T0)   # the next window
        self.assertEqual(v.authors, 0)


# ---------------------------------------------------------------- the list --

class TestTheList(unittest.TestCase):
    """The list marks a row. It must never decide one.

    If any test in this class ever has to change so that a listed account
    makes a token fire, the product has quietly become a follow tracker and
    the claim on the site is a lie."""

    LISTED = frozenset({"lucidwiz", "hanakoxbt"})

    def test_the_list_does_not_create_a_spike(self):
        """A listed account posting into a quiet token changes nothing."""
        c = seed(conn(), {**flat(2, 12), 12: 3}, authors={12: "lucidwiz"})
        bare = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0)
        marked = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0, listed=self.LISTED)
        self.assertEqual(bare.state, marked.state)
        self.assertEqual(bare.multiplier, marked.multiplier)
        self.assertEqual(marked.known, 1)
        self.assertNotEqual(marked.state, d.SPIKE)

    def test_the_list_does_not_suppress_a_spike(self):
        """A real spike with nobody listed in it still fires, and reports zero."""
        c = seed(conn(), {**flat(3, 12), 12: 40}, authors={12: "someone_else"})
        v = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0, listed=self.LISTED)
        self.assertEqual(v.state, d.SPIKE)
        self.assertEqual(v.known, 0)
        self.assertIsNone(v.first_known)

    def test_it_reports_who_on_the_list_posted_first(self):
        c = seed(conn(), {**flat(3, 12), 12: 40}, authors={12: "lucidwiz"})
        v = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0, listed=self.LISTED)
        self.assertEqual(v.first_known, "lucidwiz")
        self.assertEqual(v.known, 1)

    def test_an_empty_list_marks_nothing(self):
        c = seed(conn(), {**flat(3, 12), 12: 40}, authors={12: "lucidwiz"})
        v = d.tick(c, ADDR, T0 + 13 * W, watch_start=T0, listed=frozenset())
        self.assertEqual(v.known, 0)
        self.assertIsNone(v.first_known)

    def test_verdicts_are_identical_with_and_without_the_list(self):
        """The strongest form of the rule: over two recorded hours, every
        state and every multiplier matches, marked or not."""
        a = d.replay(seed(conn(), RECORDED), ADDR, T0, T0 + 24 * W, watch_start=T0)
        b = d.replay(seed(conn(), RECORDED), ADDR, T0, T0 + 24 * W, watch_start=T0,
                     listed=frozenset({"anon"}))     # every post is by "anon"
        self.assertEqual([v.state for v in a], [v.state for v in b])
        self.assertEqual([v.multiplier for v in a], [v.multiplier for v in b])
        self.assertTrue(all(v.known == 0 for v in a))
        self.assertTrue(any(v.known > 0 for v in b))


class TestListFile(unittest.TestCase):
    def test_the_shipped_file_parses_and_ships_empty(self):
        from trace import accounts
        self.assertEqual(accounts.load(), frozenset())

    def test_handles_are_normalised_and_notes_ignored(self):
        import tempfile, pathlib
        from trace import accounts
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / "a.txt"
            f.write_text("# header\n\n@LucidWiz   # called it early\nhanakoxbt\n  \n")
            self.assertEqual(accounts.load(f), frozenset({"lucidwiz", "hanakoxbt"}))

    def test_a_missing_file_is_an_empty_list_not_a_crash(self):
        from trace import accounts
        self.assertEqual(accounts.load("/nonexistent/accounts.txt"), frozenset())
