"""The curve as a table, and the difference between zero and unknown.

`trace.cohort` was a library with no way in: README, the Makefile and
docs/ATTENTION.md all told people to run `python -m trace.cohort`, and running
it printed nothing at all. These cover the table it prints now, and the one
distinction the whole finding rests on - a bucket that was measured and came
back quiet is not the same as a bucket nobody has looked at yet.
"""

import unittest

from trace import cohort


def curve(**windows_and_posts):
    """Build a Curve directly: {bucket_index: (windows, posts)}."""
    w, p = {}, {}
    for i, (nw, np_) in windows_and_posts.items():
        b = cohort.BUCKETS[int(i)]
        w[b], p[b] = nw, np_
    return cohort.Curve(posts=p, windows=w)


class TestTheTable(unittest.TestCase):
    def test_every_bucket_is_present_even_when_empty(self):
        # A missing row and a quiet row look identical once they are missing.
        t = cohort.rows(curve())
        self.assertEqual(len(t), len(cohort.BUCKETS))
        self.assertTrue(all(r["windows"] == 0 for r in t))
        self.assertTrue(all(r["normal"] is None for r in t))

    def test_a_thin_bucket_reports_nothing_rather_than_a_number(self):
        t = {r["lo_min"]: r for r in cohort.rows(
            curve(**{"2": (cohort.MIN_WINDOWS - 1, 99)}))}
        self.assertIsNone(t[10]["normal"])
        self.assertEqual(t[10]["windows"], cohort.MIN_WINDOWS - 1)
        self.assertEqual(t[10]["posts"], 99)

    def test_one_more_window_is_enough(self):
        t = {r["lo_min"]: r for r in cohort.rows(
            curve(**{"2": (cohort.MIN_WINDOWS, 24)}))}
        self.assertAlmostEqual(t[10]["normal"], 24 / cohort.MIN_WINDOWS)

    def test_measured_and_quiet_is_zero_not_none(self):
        t = {r["lo_min"]: r for r in cohort.rows(curve(**{"3": (40, 0)}))}
        self.assertEqual(t[20]["normal"], 0.0)
        self.assertIsNotNone(t[20]["normal"])

    def test_the_open_ended_bucket_has_no_upper_bound(self):
        last = cohort.rows(curve())[-1]
        self.assertIsNone(last["hi_min"])
        self.assertTrue(last["label"].endswith("m+"))

    def test_the_table_is_in_age_order(self):
        los = [r["lo_min"] for r in cohort.rows(curve())]
        self.assertEqual(los, sorted(los))


class TestTheCliExists(unittest.TestCase):
    def test_the_module_is_runnable(self):
        # The bug this file was written for: the module had no main().
        self.assertTrue(callable(getattr(cohort, "main", None)))

    def test_json_and_table_describe_the_same_curve(self):
        c = curve(**{"2": (20, 12), "3": (40, 10)})
        t = cohort.rows(c)
        for r in t:
            b = cohort.BUCKETS[[x["lo_min"] for x in t].index(r["lo_min"])]
            self.assertEqual(r["normal"], c.mean(b))


if __name__ == "__main__":
    unittest.main()
