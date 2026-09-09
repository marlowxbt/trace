# Cost

X moved to pay-per-use. This document is the arithmetic, the traps we walked
into, and the four rules that keep the bill from surprising anybody.

---

## The 33× spread

| feed | per post read | per 1 000 posts |
|---|---|---|
| twitterapi.io — third party, **default** | $0.0000015 × 100 credits | **$0.15** |
| official X API | $0.005 | $5.00 |

Thirty-three times apart for the same sentence. A project whose whole public
claim is *run the collector yourself with your own key* should not force the
reader to buy the expensive one, so the collector talks to a **provider** — any
object with `search`, `cost_usd` and `verify` — and the detector never learns
which one paid.

```
trace/feed.py          the vocabulary: Post, Page, RateLimit, the errors
trace/twitterapi.io    the cheap one, and the default
trace/xapi.py          the official one, kept working as an alternative
```

The provider owns the dialect too. *"Give me posts newer than this moment"* is
`since_id` on one and `since_time:` inside the query string on the other; that
difference stops at the provider boundary and never reaches the collector.

### twitterapi.io pricing, exactly

```
100 000 credits = $1
15 credits per post
15 credits minimum per call, even one that returns nothing
20 posts per page
```

An empty poll is not free. That single fact drives most of what follows.

---

## Rule 1 — one poll per window

Polling faster than the window **cannot change a verdict**: the detector only
ever rates a window that has already closed. So a faster poll buys latency on a
number that does not exist yet, and pays the 15-credit minimum every time.

| schedule | ten tokens, per day, before anybody posts |
|---|---|
| once a minute | **$2.16** |
| once per five-minute window | **$0.43** |

`poll_interval_s = 0` means "wake just after each window boundary", plus
`poll_delay_s` grace for the feed's own indexing lag.

---

## Rule 2 — a cold token buys one minimum page

The first version paged back through five pages of 100 posts on first contact
with a token: **$2.50 per token**, spent on history nobody asked for, ten times
over as the watchlist rotated.

Now the first poll buys **one minimum page**, and `backfill_min` (default 60)
decides how far back that one page reaches. About two cents per token, once.

The backfill is not a fudge. A token joins the watchlist *because* it already
has traction, which means the burst is happening at that moment and the detector
needs thirty-five minutes of baseline before it may speak. One historical read
at admission buys the baseline outright — we ask the feed about those windows
and it answers, so they are observed, merely observed late.

```python
def test_a_cold_token_buys_one_minimum_page(): ...
```

---

## Rule 3 — the budget is a hard stop

```toml
daily_budget_usd = 1.00
```

When the next request could cross the line, **the collector exits**. It does not
poll less often, shorten pages, or drop tokens to stay under.

This is the important one. A collector that degrades quietly under a budget
reports `QUIET` for a token that is screaming, and a wrong `QUIET` is worse than
no answer at all — it is an answer you would act on. Loud failure, always.

---

## Rule 4 — every request is written down

```sql
CREATE TABLE reads (
  id, ts, token_address, posts_returned, link_posts,
  est_cost_usd, since_id, http_status
);
```

Every call, with what it returned and what it cost, so the bill reconciles
against the provider's own dashboard line by line. If the two disagree, one of
them is wrong and you can find out which.

```bash
python -m trace.collector --dry-run   # every query, and the bill, without a request
python -m trace.collector --check     # prove the key works, for one empty call
python -m trace.collector --budget    # what today has cost so far
```

---

## The trap we walked into: `-has:links`

Every query carried `-has:links` for weeks, on the belief that a post containing
a URL costs $0.20 instead of $0.005 — forty times more.

It is a **write** price. Posting a tweet that contains a URL is what costs $0.20.
Reading one costs exactly what reading any other post costs. Checked against
`docs.x.com` and removed.

The filter was also actively harmful: most organic posts about a fresh contract
carry a screener link, so we were paying to read the feed and then discarding
the half of it that mattered.

**The general lesson:** a cost optimisation that also happens to filter your data
deserves twice the scepticism, because it will look like it is working.

---

## What a real day costs

9 September 2026, ten tokens, the full pipeline:

```
316 posts bought
254 requests
$0.1424
```

Fourteen cents. The expensive part of this project is not the API.

---

## Scaling, honestly

The bill scales with **posts read**, not with users. One collector serves
everybody at flat cost — which is the entire reason the attention half is
server-side while the wallet half runs in your browser.

Rough shape at current prices:

| watchlist | posts/day, order of magnitude | per month |
|---|---|---|
| 10 tokens | ~300 | **~$5** |
| 30 tokens | ~1 500 | ~$25 |
| 50 tokens | ~4 000 | ~$60 |

Do not raise `max_tokens` until a week of real bills is on the table. The
estimate above is arithmetic; the dashboard is truth.
