"""The caller list: computed, and provably inert.

`accounts.txt` was always going to be a hand-kept list of people worth marking,
and a hand-kept list has two problems that no amount of good intent fixes. It
can be lobbied, and it cannot be checked. This module replaced it with a rule
over the collector's own table, so these tests answer the two questions that
actually matter about it:

  * does the rule say what the page says it says, and
  * can a name on the list move a single number anywhere else?

The second one is the important one. The list is allowed to paint a row red.
It is not allowed to be an input.
"""

import unittest

from trace import callers, db, detector as d

W = 300
T0 = (1_780_000_000 // W) * W
A1 = "0xaaaa000000000000000000000000000000000001"
A2 = "0xbbbb000000000000000000000000000000000002"
A3 = "0xcccc000000000000000000000000000000000003"


def conn():
    return db.connect(":memory:")


def token(c, addr, symbol, launch=T0):
    db.upsert_token(c, address=addr, symbol=symbol, name=None, decimals=None,
                    launch_block=0, launch_ts=launch, related=(), now_ts=launch)


def post(c, pid, addr, author, *, at, matched="address"):
    c.execute("""INSERT INTO posts (post_id, token_address, author, created_at,
                                    text, ingested_at, matched)
                 VALUES (?,?,?,?,?,?,?)""",
              (pid, addr, author, at, "gm " + addr, at, matched))


class TestTheRule(unittest.TestCase):
    def setUp(self):
        self.c = conn()
        token(self.c, A1, "ONE")
        token(self.c, A2, "TWO")
        token(self.c, A3, "THREE")

    def rows(self):
        return {r["handle"]: r for r in callers.build(self.c)}

    def test_an_empty_database_lists_nobody(self):
        # A fresh clone has no posts. It must produce an empty list, not an
        # error and not a placeholder name.
        self.assertEqual(callers.build(self.c), [])
        self.assertEqual(callers.index([]), {})

    def test_one_token_is_not_enough(self):
        post(self.c, "1", A1, "solo", at=T0 + 60)
        post(self.c, "2", A1, "solo", at=T0 + 120)
        self.assertFalse(self.rows()["solo"]["listed"])

    def test_two_tokens_earns_the_mark(self):
        post(self.c, "1", A1, "repeat", at=T0 + 60)
        post(self.c, "2", A2, "repeat", at=T0 + 60)
        r = self.rows()["repeat"]
        self.assertTrue(r["listed"])
        self.assertEqual(r["covered"], 2)

    def test_a_refused_post_earns_nothing(self):
        # Two thirds of what we pay for is somebody else's asset wearing our
        # ticker. Posting about those must not build a reputation here.
        post(self.c, "1", A1, "ghost", at=T0 + 60)
        post(self.c, "2", A2, "ghost", at=T0 + 60, matched=None)
        self.assertFalse(self.rows()["ghost"]["listed"])
        self.assertEqual(self.rows()["ghost"]["covered"], 1)

    def test_volume_on_one_token_never_substitutes_for_reach(self):
        for i in range(50):
            post(self.c, "v%d" % i, A1, "loud", at=T0 + 60 + i)
        post(self.c, "q1", A1, "quiet", at=T0 + 60)
        post(self.c, "q2", A2, "quiet", at=T0 + 60)
        r = self.rows()
        self.assertEqual(r["loud"]["posts"], 50)
        self.assertFalse(r["loud"]["listed"])
        self.assertTrue(r["quiet"]["listed"])

    def test_led_counts_only_the_first_three_voices(self):
        for i, who in enumerate(["a", "b", "c", "d"]):
            post(self.c, "p%d" % i, A1, who, at=T0 + 60 + i)
        r = self.rows()
        self.assertEqual([r[w]["led"] for w in "abc"], [1, 1, 1])
        self.assertEqual(r["d"]["led"], 0)

    def test_first_at_is_minutes_after_launch(self):
        post(self.c, "1", A1, "early", at=T0 + 7 * 60)
        post(self.c, "2", A2, "early", at=T0 + 40 * 60)
        r = self.rows()["early"]
        self.assertEqual(r["first_at"], 7)
        self.assertIn("earliest +7m", r["why"])

    def test_index_holds_only_listed_accounts(self):
        post(self.c, "1", A1, "in", at=T0 + 60)
        post(self.c, "2", A2, "in", at=T0 + 60)
        post(self.c, "3", A1, "out", at=T0 + 60)
        idx = callers.index(callers.build(self.c))
        self.assertEqual(sorted(idx), ["in"])

    def test_the_sentence_matches_the_threshold(self):
        # The page prints RULES["sentence"] verbatim beside the list. If the
        # threshold moves and the sentence does not, the page starts lying.
        self.assertIn(str(callers.LISTED_MIN_TOKENS), callers.RULES["sentence"])
        self.assertEqual(callers.RULES["min_tokens"], callers.LISTED_MIN_TOKENS)


class TestItChangesNothing(unittest.TestCase):
    """The load-bearing test.

    Two hours of a real recording, rated twice: once with nobody marked, once
    with every author on the list. Every state, every multiplier, every
    baseline must come out identical. If this ever fails, the list has become
    an input and the product is a different, worse product.
    """

    def replay(self, *, known):
        counts, out = {}, []
        for i in range(24):
            counts[T0 + i * W] = [0, 1, 1, 0, 2, 1, 1, 0, 1, 2, 1, 1,
                                  3, 9, 14, 11, 6, 3, 2, 1, 1, 0, 1, 1][i]
        cooldown = 0
        for i in range(24):
            now = T0 + (i + 1) * W
            v = d.evaluate(counts={k: n for k, n in counts.items() if k < now},
                           now=now, watch_start=T0, cooldown_until=cooldown,
                           authors=3,
                           known=known,
                           first_known="marked" if known else None,
                           first_post=("marked", now - 30) if known else None)
            cooldown = v.cooldown_until
            out.append((v.state, v.rate, v.baseline, round(v.multiplier, 9)))
        return out

    def test_marking_every_author_changes_no_verdict(self):
        self.assertEqual(self.replay(known=0), self.replay(known=99))

    def test_the_mark_is_carried_through_untouched(self):
        v = d.evaluate(counts={T0: 4}, now=T0 + W, watch_start=T0 - 40 * W,
                       known=7, first_known="marked")
        self.assertEqual(v.known, 7)
        self.assertEqual(v.first_known, "marked")

    def test_the_detector_never_imports_the_list(self):
        # Cheaper and more honest than a mock: the module simply has no way to
        # reach it.
        import inspect
        src = inspect.getsource(d)
        self.assertNotIn("callers", src)


if __name__ == "__main__":
    unittest.main()
