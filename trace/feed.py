"""The shape of a feed, independent of who sells it.

TRACE reads posts from a paid API. There is more than one of those, they cost
wildly different money, and none of them is a safe thing to marry:

    official X       $5.00 per 1 000 posts read
    twitterapi.io    $0.15 per 1 000 posts read   (third party, not X Corp)

A 33x spread is not a detail, and a project whose whole public claim is "run
the collector yourself with your own key" should not force the reader to buy
the expensive one. So the collector talks to a *provider* - anything with the
three methods below - and the two implementations live in `xapi.py` and
`twitterapi.py`. The detector never learns which one paid.

The provider owns the dialect. "Give me posts newer than this moment" is
`since_id` on one and `since_time:` inside the query string on the other; that
difference stops here and never reaches the collector.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

# A transport takes (url, params, headers) and returns
# (http_status, response_headers, parsed_json_body). Every network call in the
# project goes through one, which is why the whole collector runs offline in
# tests, for free, before a cent is spent.
Transport = "callable(url, params, headers) -> (status, headers, json)"


class FeedError(RuntimeError):
    """Any failure that came back from the provider."""


class AuthError(FeedError):
    """The key is wrong, expired, out of credit, or for a different service."""


class TransportError(FeedError):
    """The request never reached the provider: DNS, TLS, a dropped wifi.

    Its own class because it is the one failure that says nothing about the
    provider, the key or the query - only that the network blinked. A collector
    meant to run unattended for hours must treat it as weather, retry, and
    carry on; without this it is one closed laptop lid away from dying with a
    stack trace.
    """


class RateLimited(FeedError):
    def __init__(self, reset_ts: int | None, message: str = ""):
        super().__init__(message or "rate limited")
        self.reset_ts = reset_ts

    @property
    def wait_s(self) -> int:
        if not self.reset_ts:
            return 60
        return max(1, int(self.reset_ts - time.time()))


@dataclass(frozen=True)
class RateLimit:
    limit: int | None = None
    remaining: int | None = None
    reset_ts: int | None = None

    @classmethod
    def from_headers(cls, h: Mapping[str, str]) -> "RateLimit":
        def num(k: str) -> int | None:
            v = h.get(k) or h.get(k.title()) or h.get(k.upper())
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        return cls(num("x-rate-limit-limit"),
                   num("x-rate-limit-remaining"),
                   num("x-rate-limit-reset"))


@dataclass(frozen=True)
class Post:
    post_id: str
    author: str | None       # username, lowercased; None if the provider hid it
    created_at: int          # unix seconds
    text: str
    # Identity, when the provider volunteers it in the same response. We have
    # already paid for this post; re-fetching the same account's picture from a
    # separate endpoint later would be paying twice for a fact we were handed.
    author_name: str | None = None
    author_avatar: str | None = None


@dataclass(frozen=True)
class Page:
    posts: tuple[Post, ...]
    next_cursor: str | None      # opaque; hand it straight back to the provider
    http_status: int
    rate: RateLimit = RateLimit()
    newest_id: str | None = None

    @property
    def count(self) -> int:
        return len(self.posts)


class Provider(Protocol):
    """What the collector needs, and nothing else."""

    name: str
    page_size: int

    def search(self, query: str, *, since_ts: int | None = None,
               cursor: str | None = None) -> Page: ...

    def cost_usd(self, posts: int) -> float: ...

    def verify(self) -> Page: ...


# --- shared parsing ---------------------------------------------------------

def check_key(key: str) -> str:
    """Reject a key that cannot possibly work, with a sentence a human can act
    on, before requests turns it into forty lines of urllib3 traceback.

    Written after a real one: the placeholder from a chat message was pasted in
    whole, Cyrillic and all, and the failure that came back was
    `UnicodeEncodeError: 'latin-1' codec can't encode characters in position
    8-11`. That is a true statement about HTTP header encoding and a useless
    one about what went wrong.
    """
    key = (key or "").strip().strip("'\"")
    if not key:
        raise AuthError("no API key. Set TRACE_X_BEARER, or [x] api_key in "
                        "config.toml")
    if not key.isascii():
        bad = "".join(sorted({c for c in key if not c.isascii()}))[:12]
        raise AuthError(
            f"the API key contains characters that cannot go in an HTTP "
            f"header ({bad}). This is almost always a placeholder pasted "
            f"whole - copy the real key from the provider's dashboard.")
    if "..." in key or key.startswith("<") or key.endswith(">"):
        raise AuthError("the API key still looks like a placeholder - replace "
                        "it with the real one from the provider's dashboard.")
    if len(key) < 16:
        raise AuthError(f"the API key is only {len(key)} characters long, which "
                        f"is too short to be one.")
    return key


def parse_iso(s: str) -> int:
    """RFC3339 with a Z. Python 3.10 will not eat the Z on its own."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp())


# twitterapi.io returns Twitter's own legacy format: "Tue Dec 10 07:00:30 +0000 2024"
_TWITTER_FMT = "%a %b %d %H:%M:%S %z %Y"


def parse_created_at(s: str) -> int:
    """Accept either format, because the two providers disagree."""
    s = s.strip()
    try:
        return parse_iso(s)
    except ValueError:
        return int(datetime.strptime(s, _TWITTER_FMT).timestamp())


def detail(body) -> str:
    if isinstance(body, Mapping):
        for k in ("detail", "title", "msg", "message", "error", "status"):
            if body.get(k):
                return str(body[k])[:200]
        if body.get("errors"):
            return str(body["errors"])[:200]
    return str(body)[:200]


def requests_transport(url, params, headers):
    import requests
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as e:
        raise TransportError(f"{type(e).__name__}: {str(e)[:160]}") from e
    try:
        body = r.json()
    except ValueError:
        body = {"_raw": r.text[:500]}
    return r.status_code, r.headers, body
