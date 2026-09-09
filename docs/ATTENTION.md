# Attention has a half-life

The single measurement this project exists to make, and the reason the detector
works at all on a chain that launches thirty thousand coins a day.

---

## The problem

A three-minute-old memecoin has no normal of its own.

The obvious baseline — this token's own recent history — needs six complete
five-minute windows, which is thirty minutes. On Robinhood Chain the entire
event is usually over by then. Here is a real one, `$HODLster`, 9 September
2026:

```
age    posts   accounts   what the detector could say
+1m      13        9      WARMING — no baseline yet
+6m      11        7      WARMING — no baseline yet
+11m     14       11      WARMING — no baseline yet
+16m      3        3      WARMING
+21m      1        1      WARMING
+26m      3        3      WARMING
+31m      2        2      QUIET   — baseline finally exists: 7.5
+36m      3        2      QUIET
```

Thirty-eight posts from twenty-seven accounts in the first eleven minutes, and
the instrument was silent through all of it. By the time it could speak, the
room had emptied — and its first honest sentence was *"this is quieter than
usual"*, which was true and useless.

Waiting for a token's own history means always arriving after the thing you were
built to notice.

---

## The measurement

So: measure what a token **that age** normally gets, across every token in the
corpus, and use that as the baseline until the token has six windows of its own.

Buckets are by age since launch. A bucket reports nothing until **twelve
observed windows** stand behind it — an unobserved window is missing, not zero,
and a curve built on three windows would be noise with a decimal point.

| age | posts per 5-min window | windows measured |
|---|---|---|
| 0–5m | not measured | 10 |
| 5–10m | not measured | 10 |
| 10–20m | **1.55** | 20 |
| 20–40m | 0.25 | 40 |
| 40–80m | 0.10 | 80 |
| 80–160m | 0.04 | 160 |
| 160m+ | 0.08 | 205 |

```bash
python -m trace.cohort      # rebuilds the table from the database
```

---

## What it says

**Attention falls about thirtyfold between a token's first twenty minutes and
its second hour.**

1.55 posts per window at fifteen minutes old. 0.04 at two hours. That is the
whole finding, and it has one large consequence:

> A coin's own past **is** its old age — thirty times quieter than its youth,
> and therefore useless as a yardstick for its youth.

Any tool on this chain that baselines a young token against its own history is
comparing a launch against a graveyard and will call everything a spike, or
nothing.

---

## How it is used

```
if the token has six fully observed windows of its own:
    baseline = mean of those six              baseline_kind = "own"
elif a cohort normal exists for its age:
    baseline = that                           baseline_kind = "cohort"
else:
    WARMING
```

Every verdict carries `baseline_kind`, so a reader can always tell which
comparison produced the number. On the same `$HODLster` run, with the cohort
baseline in place:

```
age    posts   baseline   kind      multiplier   state
+1m      13      —        —              —       WARMING
+6m      11      —        —              —       WARMING
+11m     14     1.55      cohort       9.0×      SPIKE     ← eleven minutes in
+16m      3     1.55      cohort       1.9×      WARM
+21m      1     0.25      cohort       1.0×      QUIET
+31m      2     7.50      own          0.3×      QUIET
```

Same posts, same rules, same code — one honest baseline instead of a missing
one, and the call lands at minute eleven instead of never.

---

## Caveats worth stating plainly

- **One day, ten tokens, 355 observed windows.** It is measured, not
  authoritative. The shape (steep early decay) is unlikely to be wrong; the
  exact 1.55 will move.
- **The 160m+ bucket is noisy.** It reads 0.08, *above* the 80–160m bucket's
  0.04, because a handful of tokens get a second wind hours later and there is
  no third bucket to separate them from the dead ones. Do not read the tail as a
  trend.
- **It is a corpus of watched tokens, not of all tokens.** The watchlist admits
  coins that already have traction, so this curve describes attention decay
  *among coins that got some attention*. That is the right population for the
  question the detector asks, and the wrong one for "what does an average launch
  get" — which is approximately zero.
- **Re-derive it, don't inherit it.** `trace.cohort` rebuilds from whatever is in
  the database. If you run the collector for a month, your curve is better than
  this one.
