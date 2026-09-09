# Who keeps showing up

The original spec said `accounts.txt` would carry a hand-kept list of callers,
that a name on it would put a red edge on a row, and that it would **never**
change a verdict. One account per pull request; no paid placement, ever; ships
empty on purpose.

`trace/callers.py` is the same idea with the hand taken out. The list is
computed from what the collector already measured, so nobody has to be trusted
to keep it honest and nobody can buy a place on it.

```bash
python -m trace.callers            # the ranked table
python -m trace.callers --json     # the same thing, for the exporter
```

---

## What is measured

Three things, all counts:

```
covered   how many of the watched tokens this account posted about,
          counted posts only
first_at  minutes after launch of its earliest counted post
led       tokens where it was among the first three counted voices
```

That is the entire feature set. No follower count, no engagement, no
reach, no score. Those are all available and all refused, for the same reason
the detector refuses price: the moment a number can be gamed, publishing it
tells you about the gaming.

---

## The rule

```python
LISTED_MIN_TOKENS = 2       # showed up on this many watched tokens
LED_RANK          = 3       # "among the first three voices" on a token
EARLY_MIN         = 5       # a first post inside this many minutes is early
```

`covered >= 2` earns the mark. It is deliberately dull: an account that turns up
on two different launches on the same chain in one day is either a caller or a
bot, and either way it is worth seeing marked. It says **nothing** about whether
the call was good, and this module has no opinion about that.

---

## What it found

One day, ten tokens. Sixty-eight accounts wrote something that counted. **Nine
of them turned up on more than one launch.**

```
  account                tokens  led  first  median  posts  showed up on
* @yosefperal1539             4    1   +17m    +41m      4  Fork, HODLster, ROB, TRADE
* @solanatren59241            2    2    +2m     +5m      2  AIP, ROB
* @mostviewcrypto             2    2    +2m     +7m      2  AIP, TRADE
* @dexevents_1                2    1    +5m    +42m      2  Fork, ROBINHOOD
* @robinhoodradar             2    1    +7m    +13m      2  HODLster, ROBOSHARE
* @pf_streams                 2    1   +21m   +177m      8  HODLster, ROBINHOOD
* @denvermalo8346             2    1   +62m    +62m      2  ROBINHOOD, ROBOSHARE
* @solinsidr                  2    0    +4m    +12m      5  Fork, HODLster
* @dex_kolwatcher             2    0   +15m    +55m      2  Fork, ROBINHOOD
  @robladeintern              1    1    +0m   +115m      2  ROB
  @thexcaller                 1    1    +2m     +2m      1  ROBOSHARE
  @shdow_sol                  1    1    +2m     +2m      2  HODLster
```

`@solanatren59241` and `@mostviewcrypto` are the interesting shape: two tokens
each, first post **two minutes** after launch, and among the first three voices
on both. Whatever they are — a person with a scanner, or a scanner with a
posting schedule — they are consistently early, and that is a fact about the
feed worth having on the screen.

`@robladeintern` posting at **+0m** on `$ROB` is the other shape: a project
account announcing its own launch. Also worth seeing, also not a signal.

---

## Where you see it

On the desk (`python -m trace.serve`), in two places and only two:

* **A red left edge and a red handle** on any post whose author is on the list,
  with a `N tokens` chip beside the name. Hovering the row gives the sentence
  that put them there. An `on the list` filter narrows the feed to those posts.
* **A panel of its own** down the right-hand side: handle, avatar, how many
  watched tokens, how often they were a first voice, how early they arrive, and
  the rule itself printed above the list in full.

The list travels beside the posts in `/api/live.json`, never inside them:

```jsonc
{
  "posts":   [ /* unchanged, with no idea the list exists */ ],
  "callers": [ /* the list */ ],
  "caller_rules": { "min_tokens": 2, "sentence": "..." }
}
```

That shape is the claim made structural. A post object is byte-identical
whether or not its author is listed, so the mark can only ever be paint.

---

## What the mark does not do

Nothing. This is the constraint the whole feature hangs on.

`detector.evaluate` takes counts, `authors`, and two list fields, and passes the
list fields **straight through without reading them**. `tests/test_callers.py`
replays two recorded hours twice — once with an empty list, once with a list
matching every single author — and asserts every state, every baseline and every
multiplier is identical. A second test reads the detector's own source and
asserts the word `callers` does not appear in it.

If those tests ever fail, the feature is wrong, not the test.

The reason is simple: the moment a name on a list can move a verdict, the list
becomes worth money, and a list worth money is a list somebody is selling. The
project's answer to that is not "we would never" — it is that there is nothing
to buy, because the number is computed and the mark is inert.

---

## Why not follower counts

They are one API call away and they would look great on the page.

They are also the single most manipulated number in this market, they say
nothing about whether an account was early, and importing them would put a
purchasable quantity on a screen that is otherwise entirely made of things we
measured ourselves. `covered`, `first_at` and `led` cost nothing extra, cannot
be bought without actually posting early about actual launches, and are
reproducible from the same database as every other figure in this repository.

---

## `accounts.txt` still exists

It ships empty and it is still read, for the case the computed list cannot
cover: an account you know about that has not yet posted on two of *your*
watched tokens. Same rules as before — one account per pull request, the note
must say what they called and when with a link, no paid placement — and the same
guarantee: it marks a row and changes nothing.
