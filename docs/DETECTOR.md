# The detector

Every number, where it came from, and what moved it. Nothing here is tuned per
token — the same shape applies to every coin on the chain, forever, because a
per-token knob is a per-token opinion and this project does not have opinions.

---

## The rules

```
window      300 seconds, aligned to the epoch
rate        posts in the last COMPLETE window
baseline    mean of this token's six previous complete windows,
            or the cohort normal for its age if it has fewer than six
multiplier  rate / max(baseline, 1.0)
floor       5 posts in a window
cooldown    1800 seconds per token after a SPIKE
accounts    distinct authors in the window — reported, never gates
```

| state | condition |
|---|---|
| `WARMING` | fewer than six fully observed windows behind it, and no cohort normal for its age |
| `QUIET` | multiplier < 1.5 |
| `WARM` | multiplier ≥ 1.5, and either not loud enough to call or in cooldown |
| `SPIKE` | multiplier ≥ 3.0 **and** rate ≥ 5 **and** out of cooldown |

---

## Why the window is aligned to the epoch

Not "the last five minutes" — the last five-minute block, on the clock. Two
consequences, both wanted:

- **A verdict is reproducible.** Anyone with the same posts computes the same
  windows, so a claim on the site can be checked by a stranger.
- **A poll cannot buy a better answer.** Polling at 12:03 and again at 12:04
  rates the same closed window. That is what makes "one poll per window" a cost
  decision and not a quality trade-off.

---

## Why the floor gates SPIKE only

An earlier version applied the floor to `WARM` as well. It produced this:

```
7 posts, baseline 2.0   -> 3.5×  -> below the floor -> QUIET
30 posts, baseline 10.3 -> 2.9×  -> above the floor -> WARM
```

Acceleration turning into silence as it speeds up. The floor exists to stop
*"one post became four posts, 4×!!"* from firing an alert, which is a statement
about calling something, not about describing it. So it gates the call and
nothing else.

---

## Why the floor is 5 and not 8

`floor 8` was a guess, written before any live data existed, and the replay
tests proved the logic rather than the threshold.

Measured over a day of real launches: the busiest five-minute window on any
watched token held **14** posts and the next busiest held **6**. A floor of 8
admitted one event and refused the other, and nothing in the data justified the
gap between them — the 6-post window was 3.9× its cohort normal from five
separate accounts, which is exactly the shape the detector exists to notice.

At 5 both are admitted and no window of ordinary noise comes close. Sample is
one day and ten tokens, so:

```bash
python -m trace.collector --calibrate
```

re-derives it as more accumulates. Move it because the data moved, and write
down what the data said.

---

## The history rule

> A window counts once the whole of it falls after the moment we actually
> started asking about that token.

An observed window with zero posts is observed. An unobserved one is **missing,
not zero**. No partial averaging, no zero padding, no grace period.

This one bit us. The first version used `added_ts` — when the token entered the
watchlist — as the horizon. But the collector backfills an hour of history on
first contact, so posts arrived for windows *before* the token was ever watched,
and the windows in between were counted as genuine zeros. Result: a baseline of
0.3 on a token nobody had been watching, and a `SPIKE` on three posts.

The fix is a separate column, `cursors.observed_from`, recording the moment we
first asked the feed about that token — including the backfilled hour, because
we did ask about it and it did answer. Tests assert that history begins where we
actually asked and nowhere else.

---

## Warm-up is not a setting

There is no `warm_up_min`. Warm-up is what `baseline_windows` already implies: a
token cannot be rated until six windows have been fully observed, i.e. 35
minutes after we start. A second knob for the same rule is a second place for it
to disagree with itself.

`TestConfigMatchesTheDetector` fails the build if `config.example.toml` and the
detector's defaults drift apart, because the numbers in this document are
published on a website and a collector running a different rule than the one
described is worse than no collector.

---

## The cohort baseline

Six windows is thirty minutes, and on this chain the entire event is usually
over by then. Measured: one token's whole run was 13, 11 and 14 posts in three
consecutive windows — **all of it inside the warm-up** — and a detector waiting
for its own baseline was still silent when it ended.

So when a token has fewer than six of its own windows, it is measured against
what a token *that age* normally gets. Same curve for every coin, derived from
the corpus, rebuilt on every run. See [ATTENTION.md](ATTENTION.md).

A verdict says which baseline it used (`baseline_kind` is `own` or `cohort`), so
you can always tell whether you are looking at a token compared with itself or
with its cohort. That field is on the public snapshot too.

---

## Cooldown

Thirty minutes per token after a `SPIKE`. One moment must not become forty
notifications, and a burst that stays loud is still one burst.

During cooldown a token that would otherwise fire reports `WARM`, not `QUIET` —
it *is* still loud, and saying otherwise would be a lie of convenience.

---

## Accounts are reported, never gating

`accounts` is the number of distinct authors in the window. It is on every
verdict and on every row of the site, and it changes nothing.

The temptation is obvious: require, say, five distinct accounts before firing.
It was refused because it is a second threshold doing the first one's job badly
— fourteen posts from eleven accounts and fourteen posts from three accounts are
different events, and the honest move is to show you both numbers rather than
collapse them into one gate and hide which happened.

---

## What a replay proves

```bash
python -m trace.detector
```

replays every stored window for every token and prints every verdict. Two
recorded hours of a real stream are committed as a fixture, and a test asserts
the replay produces byte-identical verdicts. If a refactor changes a single
multiplier, the build fails.
