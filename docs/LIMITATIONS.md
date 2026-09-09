# Limitations

Written down because a tool that lists what it cannot do is easier to trust with
what it can.

---

## It cannot tell you a coin is good

It counts people posting. That is the entire claim.

A room getting loud is a fact about the room, not about the asset, and the two
most common reasons a room gets loud are (a) something real and (b) a
coordinated call. TRACE cannot distinguish them and does not try. What it gives
you is the count and the number of distinct voices behind it, early enough to go
look yourself.

There is no target, no score, no confidence, no buy or sell verdict, and there
will not be. See [SAFETY.md](SAFETY.md).

---

## One chain

Robinhood Chain, id 4663, Pons v2 factory only. The traction rules, the cohort
curve and the floor are all measured *here* and none of them transfer for free.
The code would port; the numbers would have to be re-derived from scratch.

---

## The feed is a third party

`twitterapi.io` is not X Corp. It is thirty-three times cheaper and it is
somebody's business that could stop existing on a Tuesday. That is exactly why
the collector talks to a provider interface with two implementations — the
official X API is kept working as an alternative, at $5.00 per thousand posts
instead of $0.15.

A feed can also be wrong: rate-limited, lagging its own index, or quietly
dropping posts. The collector re-asks slightly behind its last cursor
(`overlap_s`) so nothing is lost at the seam, and duplicates die on `post_id` —
but a post the feed never returns is a post we never count.

---

## The cohort curve is one day and ten tokens

355 observed windows. The shape — steep early decay, roughly thirtyfold between
the first twenty minutes and the second hour — is unlikely to be wrong. The
exact **1.55** will move.

The 160m+ bucket reads *higher* than the bucket before it, because a few tokens
get a second wind hours later and there is no further bucket to separate them
from the dead ones. Do not read the tail as a trend.

Run the collector for a month and your curve is better than this one:
`python -m trace.cohort`.

---

## Five minutes is coarse

The window is chosen so a "burst" cannot be one account posting eleven times in
ninety seconds. That costs latency: a room that gets loud at 12:01 is called at
12:05 at the earliest, and the detector only ever rates windows that have
already closed.

A shorter window would be faster and much noisier, and the floor would have to
move with it. It is a trade, made once, in public.

---

## Attribution is conservative on purpose

A post counts if it carries the contract address, or the cashtag together with a
chain marker. That misses real posts — somebody writing `$HODLster is running`
with no address and no chain word is talking about our token and will not be
counted.

The alternative is worse. `$TER` is Teradyne on NASDAQ, `$STAG` is an industrial
REIT, `$FORK` is also a Solana coin, and on the measured day **two accounts
posted our ticker over somebody else's contract entirely**. Of 316 posts bought,
107 counted; a looser filter would have counted noise as attention and reported
`SPIKE` on an earnings rumour.

Under-counting produces a quiet instrument. Over-counting produces a lying one.

---

## Warm-up still exists

The cohort baseline shrinks it a lot — a token can be rated from its second
window instead of its seventh — but a token still cannot be rated in the first
five minutes of its life, because there is no complete window yet and the two
youngest cohort buckets do not have twelve observations behind them.

If the whole event happens in the first four minutes, TRACE misses it.

---

## The caller list is a mark, not a rating

`covered >= 2` means an account showed up on two of *your* watched tokens in the
period *you* collected. It is not a reputation, it does not say the calls were
good, and it changes no verdict by construction. See [CALLERS.md](CALLERS.md).

---

## The book half prices nothing yet

`web/book.html` reads a wallet's real entries, transfers and ERC-20 balances
straight off chain RPC, in your browser. It does **not** price them, and it says
so on the page rather than inventing a PnL. Cost basis, realised and unrealised
figures are stage 6 and not finished.

---

## No alerts, no notifications, no bot

There is no Telegram bot, no webhook, no email. The desk refreshes and you look
at it. Anything that pushes a message to a person about a memecoin is one
product decision away from being a signal group, and that is a different
project with different incentives.

---

## Open, but not a service

There is no hosted API. `trace.serve` binds to `127.0.0.1` and holds no
credentials. If you want it running, you run it, with your own key, and the bill
is yours — which is also the only version of this that can honestly promise the
four constraints in [SAFETY.md](SAFETY.md).
