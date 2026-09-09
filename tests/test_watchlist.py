"""Stage 1 selection, tested without a network.

The property these tests exist to protect: the watchlist must not churn. The
detector needs 20 minutes of warm-up and an hour of history before a token can
fire, so a selector that swaps its members every pass produces a product that
can never emit a single SPIKE."""

import dataclasses
import unittest

from trace.config import Config, WatchlistConfig
from trace.watchlist import Candidate, select, ticker_ok

NOW = 1_780_000_000


def cfg(**over) -> Config:
    return Config(watchlist=dataclasses.replace(
        WatchlistConfig(max_tokens=5, enter_min_holders=12, enter_min_transfers=25,
                        stay_min_holders=3, stay_min_transfers=5, min_age_min=10,
                        max_age_hours=72, displace_factor=2.0),
        **over))


def cand(sym, holders, transfers, *, incumbent=False, source="auto",
         age_min=30, addr=None) -> Candidate:
    return Candidate(
        address=addr or ("0x" + f"{abs(hash((sym, holders, transfers, incumbent))):040x}"[:40]),
        launch_block=1, launch_ts=NOW - age_min * 60, symbol=sym,
        holders=holders, transfers=transfers, incumbent=incumbent, source=source)


def watched(sel):
    return [c.symbol for c in sorted(sel.watched, key=lambda c: c.score, reverse=True)]


class TestEntry(unittest.TestCase):
    def test_fills_by_score_up_to_cap(self):
        cs = [cand(f"T{i}", 100 - i, 500) for i in range(8)]
        sel = select(cs, cfg(), NOW)
        self.assertEqual(watched(sel), ["T0", "T1", "T2", "T3", "T4"])

    def test_below_entry_traction_is_skipped(self):
        sel = select([cand("WEAK", 11, 500), cand("OK", 12, 25)], cfg(), NOW)
        self.assertEqual(watched(sel), ["OK"])

    def test_too_young_to_score(self):
        sel = select([cand("BABY", 900, 9000, age_min=2)], cfg(), NOW)
        self.assertEqual(watched(sel), [])

    def test_unusable_tickers_never_enter(self):
        # The X query is ("0xADDR" OR "$TICKER"); a ticker with a space in it
        # cannot be written into one.
        for bad in ("Elf Xuan", "", "A", "x" * 30, "SO-ME", None):
            self.assertFalse(ticker_ok(bad, cfg()), bad)
        for good in ("TRADE", "GROK32", "a_b"):
            self.assertTrue(ticker_ok(good, cfg()), good)

    def test_duplicate_ticker_keeps_only_the_strongest(self):
        # Three live tokens called Fork is a real thing on this chain.
        cs = [cand("Fork", 500, 2000, addr="0x" + "a" * 40),
              cand("Fork", 250, 700, addr="0x" + "b" * 40),
              cand("Fork", 90, 300, addr="0x" + "c" * 40)]
        sel = select(cs, cfg(), NOW)
        self.assertEqual([c.address for c in sel.watched], ["0x" + "a" * 40])
        self.assertEqual(len(sel.by_action("skip")), 2)


class TestStickiness(unittest.TestCase):
    def test_incumbent_below_entry_but_alive_is_kept(self):
        """The whole point of two thresholds. A token that entered hot and
        cooled to 6 holders keeps its collected history."""
        cs = [cand("OLD", 6, 20, incumbent=True)]
        sel = select(cs, cfg(), NOW)
        self.assertEqual(watched(sel), ["OLD"])
        self.assertEqual(sel.by_action("remove"), [])

    def test_incumbent_that_went_quiet_is_removed(self):
        sel = select([cand("DEAD", 2, 4, incumbent=True)], cfg(), NOW)
        self.assertEqual(watched(sel), [])
        self.assertIn("went quiet", sel.by_action("remove")[0].reason)

    def test_incumbent_past_max_age_is_removed(self):
        sel = select([cand("ANCIENT", 400, 4000, incumbent=True, age_min=73 * 60)],
                     cfg(), NOW)
        self.assertEqual(watched(sel), [])
        self.assertIn("older than", sel.by_action("remove")[0].reason)

    def test_full_list_of_healthy_incumbents_does_not_churn(self):
        """The regression that matters: fifty fresher, hotter launches must not
        turn the list over while the incumbents are still trading."""
        inc = [cand(f"I{i}", 100, 500, incumbent=True) for i in range(5)]
        new = [cand(f"N{i}", 400 + i, 4000) for i in range(50)]
        sel = select(inc + new, cfg(), NOW)
        self.assertEqual(sorted(watched(sel)), ["I0", "I1", "I2", "I3", "I4"])
        self.assertEqual(sel.by_action("add"), [])
        self.assertEqual(sel.by_action("remove"), [])


class TestDisplacement(unittest.TestCase):
    def test_much_stronger_candidate_displaces_a_fading_incumbent(self):
        inc = [cand(f"I{i}", 100, 500, incumbent=True) for i in range(4)]
        fading = cand("FADE", 8, 20, incumbent=True)      # alive, below entry
        hot = cand("HOT", 300, 3000)
        sel = select(inc + [fading, hot], cfg(), NOW)
        self.assertIn("HOT", watched(sel))
        self.assertNotIn("FADE", watched(sel))
        self.assertIn("displaced", sel.by_action("remove")[0].reason)

    def test_no_displacement_when_incumbent_is_still_above_entry(self):
        inc = [cand(f"I{i}", 100, 500, incumbent=True) for i in range(5)]
        sel = select(inc + [cand("HOT", 9999, 99999)], cfg(), NOW)
        self.assertNotIn("HOT", watched(sel))

    def test_no_displacement_below_the_factor(self):
        inc = [cand(f"I{i}", 100, 500, incumbent=True) for i in range(4)]
        fading = cand("FADE", 8, 20, incumbent=True)
        lukewarm = cand("MEH", 15, 30)   # above entry, but < 8 * 2.0
        sel = select(inc + [fading, lukewarm], cfg(), NOW)
        self.assertIn("FADE", watched(sel))
        self.assertNotIn("MEH", watched(sel))


class TestPins(unittest.TestCase):
    def test_pin_enters_regardless_of_traction_and_consumes_a_slot(self):
        pin = cand("PIN", 0, 0, source="pin")
        news = [cand(f"N{i}", 500, 5000) for i in range(6)]
        sel = select([pin] + news, cfg(), NOW)
        self.assertIn("PIN", watched(sel))
        self.assertEqual(len(sel.watched), 5)   # the bill does not care why

    def test_pins_over_the_cap_are_reported_not_silently_polled(self):
        pins = [cand(f"P{i}", 0, 0, source="pin") for i in range(7)]
        sel = select(pins, cfg(), NOW)
        self.assertEqual(len(sel.watched), 5)
        self.assertEqual(len(sel.by_action("skip")), 2)


if __name__ == "__main__":
    unittest.main()
