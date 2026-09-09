"""Does this post talk about *this* token?

The live probe settled an argument that could not be settled at a desk. Over
twenty-four hours, across ten watched tokens:

    by contract address    93 posts
    by cashtag            235 posts
    carrying both          61 posts

The gap is not slack, it is other people's assets. `$TER` returned 37 posts and
none of them carried our address, because TER is **Teradyne, Inc.**, a company
on the NASDAQ, and most of those posts were in Korean. `$STAG` returned 8, and
STAG is a listed industrial REIT. Counting those as attention on a Robinhood
Chain memecoin is not a rounding error - it is the counter reporting a number
about a different asset entirely, which is the one failure this project cannot
survive, because every verdict downstream is a ratio against it.

So the query stays wide and the **counting gets narrow**. We ask the feed for
`("0x..." OR $SYM)` because that is where the posts are, we pay for everything
it returns - at $0.00015 a post that is three cents a day for ten tokens - and
then we decide for ourselves, here, in code anyone can read, whether each post
is about the token we mean.

Two ways to qualify, and no third:

1. **The post carries the contract address.** Unambiguous. Nobody types 42
   hex characters about a different token.
2. **The post carries the cashtag *and* says which chain.** The live sample is
   full of exactly this shape: "Fork $FORK on Robinhood CA: 0x…",
   "ROBOSHARE $ROBOSHARE on Robinhood", "$TRADE on Robinhood". A Teradyne
   earnings note in Korean has no reason to mention Robinhood Chain, and does
   not.

Everything else is stored, because we paid for it and throwing away evidence is
how you end up guessing later, but it is **not counted**. `matched` on the
posts row says which rule let it through, and the detector reads only rows
where that is set.
"""

from __future__ import annotations

import re

# Phrases that pin a post to this chain. Deliberately literal and few: every
# entry here is a way for someone else's asset to get counted as ours, so the
# list earns additions the way accounts.txt does - with an example.
CHAIN_MARKERS = (
    "robinhood chain",
    "robinhoodchain",
    "on robinhood",
    "robinhood ca",
    "#robinhoodchain",
    "chain 4663",
)

ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}")

BY_ADDRESS = "address"
BY_CASHTAG_AND_CHAIN = "cashtag+chain"


def cashtag_in(text: str, symbol: str | None) -> bool:
    if not symbol:
        return False
    sym = symbol.strip().lstrip("$")
    if not sym:
        return False
    return re.search(rf"\${re.escape(sym)}\b", text, re.IGNORECASE) is not None


def chain_marker_in(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in CHAIN_MARKERS)


def other_addresses(text: str, address: str) -> list[str]:
    """Contract addresses in the post that are not ours."""
    ours = (address or "").lower()
    return [a for a in ADDRESS.findall(text) if a.lower() != ours]


def match(text: str, address: str, symbol: str | None,
          created_at: int | None = None, launch_ts: int | None = None) -> str | None:
    """Which rule lets this post count, or None if none does.

    The third rule is a veto, and it was written from the very first post this
    project ever stored:

        PumpFun added Robinhood chain launches and streams
        $ROBINHOOD is one of the first to go live
        0x53d2e1225AaCe4551194eCBB6bbE5c44d94AA5fa

    Cashtag: ours. Chain: ours. Contract: **somebody else's**. Rule 2 alone
    would have counted it, and it is a post about a different token that
    happens to share a name. So: a post that names a contract at all, and none
    of them is ours, is not about our token no matter what else it agrees
    with. Naming a contract is the most specific thing a post can do, and when
    it does it, we believe it over the ticker.
    """
    if not text:
        return None
    # A post written before the contract existed cannot be about the contract.
    # This is not a heuristic, it is arithmetic, and it is worth stating
    # separately because the cashtag rule leaks badly without it: `$ROBINHOOD`
    # has been posted about the company and the chain for months, and 27 000
    # seconds of that chatter attached itself to a token minutes old.
    if created_at is not None and launch_ts and created_at < launch_ts:
        return None
    if address and address.lower() in text.lower():
        return BY_ADDRESS
    if cashtag_in(text, symbol) and chain_marker_in(text):
        if other_addresses(text, address):
            return None
        return BY_CASHTAG_AND_CHAIN
    return None
