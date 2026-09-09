"""twitterapi.io - the provider TRACE actually runs on.

A third party, not X Corp, and the reason the economics of this project work
at all: $0.15 per thousand posts against X's $5.00, a factor of thirty-three.
That is the difference between watching ten tokens and watching fifty.

What is different from the official API, and why the collector cannot just
swap the base URL:

    auth        `X-API-Key: <key>`, not `Authorization: Bearer <token>`
    endpoint    GET /twitter/tweet/advanced_search
    paging      `cursor` ("" for the first page), and `has_next_page` tells you
                whether to ask again. Pages hold up to 20 posts, not 100.
    "since"     there is no since_id. Recency is expressed inside the query
                string as `since_time:<unix seconds>`, which is in some ways
                better: we can ask for exactly the stretch we have not seen.
    shape       {"tweets": [{id, text, createdAt, author: {userName}}],
                 "has_next_page": bool, "next_cursor": str}
    createdAt   Twitter's legacy format, "Tue Dec 10 07:00:30 +0000 2024",
                not RFC3339.

Billing, from twitterapi.io/pricing (checked September 2026):

    1 USD = 100 000 credits
    15 credits per post returned          -> $0.00015 a post
    minimum 15 credits per call           -> an empty poll is not free

That minimum is the one number that shapes the collector's schedule. Polling
ten tokens every minute costs 216 000 credits a day, $2.16, before anybody has
posted a word. Polling them once per five-minute window costs a fifth of that
and loses nothing, because the detector only ever rates a window that has
already closed. See `collector.next_wake`.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Mapping

from .feed import (AuthError, TransportError, check_key, Page, Post, RateLimit, RateLimited,  # noqa: F401
                   FeedError, detail, parse_created_at, requests_transport)

BASE = "https://api.twitterapi.io"
SEARCH_URL = f"{BASE}/twitter/tweet/advanced_search"

PAGE_SIZE = 20                  # their page, not our choice
CREDITS_PER_USD = 100_000
CREDITS_PER_POST = 15
MIN_CREDITS_PER_CALL = 15


class Client:
    """One twitterapi.io key, one endpoint, counted requests."""

    name = "twitterapi.io"
    page_size = PAGE_SIZE

    def __init__(self, api_key: str, *, transport=None, max_retries: int = 3,
                 sleep=time.sleep, query_type: str = "Latest"):
        self._key = check_key(api_key)
        self._transport = transport or requests_transport
        self._max_retries = max_retries
        self._sleep = sleep
        self._query_type = query_type
        self.requests_made = 0
        self.posts_read = 0
        self.credits_spent = 0
        self.last_rate = RateLimit()

    def __repr__(self) -> str:          # never leak the key into a traceback
        return (f"<twitterapi.Client requests={self.requests_made} "
                f"posts={self.posts_read} credits={self.credits_spent}>")

    __str__ = __repr__

    # --- money --------------------------------------------------------------

    @staticmethod
    def credits_for(posts: int) -> int:
        """What one call costs. The floor is charged even for an empty page."""
        return max(MIN_CREDITS_PER_CALL, CREDITS_PER_POST * posts)

    def cost_usd(self, posts: int) -> float:
        return self.credits_for(posts) / CREDITS_PER_USD

    # --- the wire -----------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._key, "User-Agent": "trace-collector/0.1"}

    @staticmethod
    def with_since(query: str, since_ts: int | None) -> str:
        """Their `since_id` equivalent, and it lives inside the query string."""
        if since_ts is None:
            return query
        return f"{query} since_time:{int(since_ts)}"

    def search(self, query: str, *, since_ts: int | None = None,
               cursor: str | None = None) -> Page:
        params: dict[str, Any] = {
            "query": self.with_since(query, since_ts),
            "queryType": self._query_type,
            "cursor": cursor or "",
        }
        attempt = 0
        while True:
            try:
                status, headers, body = self._transport(SEARCH_URL, params,
                                                        self._headers)
            except TransportError:
                # The network, not the provider. Weather: wait and try again.
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                self._sleep(min(30, 2 ** attempt))
                continue
            self.requests_made += 1
            self.last_rate = RateLimit.from_headers(headers or {})

            if status == 200:
                return self._page(body or {}, status)
            if status in (401, 403):
                raise AuthError(f"HTTP {status}: {detail(body)}")
            if status == 402:
                raise AuthError(f"HTTP 402, out of credit: {detail(body)}")
            if status == 429:
                raise RateLimited(self.last_rate.reset_ts,
                                  f"HTTP 429: {detail(body)}")
            if 500 <= status < 600 and attempt < self._max_retries:
                attempt += 1
                self._sleep(min(30, 2 ** attempt))
                continue
            raise FeedError(f"HTTP {status}: {detail(body)}")

    def _page(self, body: Mapping[str, Any], status: int) -> Page:
        posts = []
        for t in body.get("tweets", []) or []:
            try:
                created = parse_created_at(t["createdAt"])
            except (KeyError, ValueError):
                continue                      # undated post cannot be counted
            a = t.get("author") or {}
            who = (a.get("userName") or "").lower() or None
            posts.append(Post(post_id=str(t["id"]), author=who,
                              created_at=created, text=t.get("text", ""),
                              author_name=a.get("name") or None,
                              author_avatar=a.get("profilePicture") or None))
        self.posts_read += len(posts)
        self.credits_spent += self.credits_for(len(posts))
        nxt = body.get("next_cursor") or None
        if not body.get("has_next_page"):
            nxt = None
        return Page(posts=tuple(posts), next_cursor=nxt, http_status=status,
                    rate=self.last_rate,
                    newest_id=posts[0].post_id if posts else None)

    def verify(self) -> Page:
        """Prove the key works for the price of one empty call.

        A query that cannot match anything returns zero posts, so the charge is
        the 15-credit floor - $0.00015, a seventh of a cent - and it exercises
        auth, the endpoint and the billing path for real.
        """
        return self.search(f'"tracekeycheck{secrets.token_hex(12)}"')
