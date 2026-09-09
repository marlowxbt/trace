"""The official X API v2 - kept as an alternative, not as the default.

TRACE runs on twitterapi.io because it is thirty-three times cheaper. This
module exists so that nobody cloning the repository is *forced* through a
third party: point `provider = "x"` at an official app-only bearer token and
the collector behaves identically. The detector cannot tell the difference.

Prices, from docs.x.com/x-api/getting-started/pricing (September 2026):

    $0.005 per post read, flat
    $0.015 to create a post, $0.200 to create one containing a URL

That third number is the famous forty-times figure and it is a *write* price.
It has nothing to do with reading, which is why this project carries no
`-has:links` filter. Pay-per-use reads are capped at 3M a month.

Dialect notes, the things `feed.Provider` exists to hide:

    auth        Authorization: Bearer <token>
    endpoint    GET /2/tweets/search/recent
    paging      `next_token`, up to 100 posts a page
    "since"     `start_time`, RFC3339. (`since_id` also exists, but a
                timestamp is what the collector actually has and what the
                other provider speaks, so this one uses it too.)
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .feed import (AuthError, TransportError, check_key, FeedError, Page, Post, RateLimit,  # noqa: F401
                   RateLimited, detail, parse_created_at, requests_transport)

BASE = "https://api.x.com/2"
SEARCH_URL = f"{BASE}/tweets/search/recent"

TWEET_FIELDS = "created_at,author_id"
EXPANSIONS = "author_id"
USER_FIELDS = "username"

PAGE_SIZE = 100
MIN_RESULTS = 10
PRICE_PER_POST_USD = 0.005

# An app-only bearer token is a long URL-safe string, in practice well over a
# hundred characters. We never refuse a key on format - X may change it - but a
# 401 on something short is almost always a value that is not a token at all:
# an API key, a project id, or a key for an entirely different service.
_BEARER_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                    "0123456789%-._~+/=")


def looks_like_bearer(token: str) -> bool:
    return len(token) >= 60 and set(token) <= _BEARER_CHARS


def rfc3339(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    name = "x"
    page_size = PAGE_SIZE

    def __init__(self, bearer_token: str, *, transport=None, max_retries: int = 3,
                 sleep=time.sleep, page_size: int | None = None):
        self._token = check_key(bearer_token)
        self._transport = transport or requests_transport
        self._max_retries = max_retries
        self._sleep = sleep
        self.page_size = page_size or PAGE_SIZE
        self.requests_made = 0
        self.posts_read = 0
        self.last_rate = RateLimit()

    def __repr__(self) -> str:          # never leak the key into a traceback
        return f"<xapi.Client requests={self.requests_made} posts={self.posts_read}>"

    __str__ = __repr__

    def cost_usd(self, posts: int) -> float:
        return posts * PRICE_PER_POST_USD

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}",
                "User-Agent": "trace-collector/0.1"}

    def search(self, query: str, *, since_ts: int | None = None,
               cursor: str | None = None, max_results: int | None = None) -> Page:
        params: dict[str, Any] = {
            "query": query,
            "max_results": max(MIN_RESULTS, min(PAGE_SIZE,
                                                int(max_results or self.page_size))),
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "user.fields": USER_FIELDS,
        }
        if since_ts is not None:
            params["start_time"] = rfc3339(since_ts)
        if cursor:
            params["next_token"] = cursor

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
            if status == 429:
                raise RateLimited(self.last_rate.reset_ts, f"HTTP 429: {detail(body)}")
            if 500 <= status < 600 and attempt < self._max_retries:
                attempt += 1
                self._sleep(min(30, 2 ** attempt))
                continue
            raise FeedError(f"HTTP {status}: {detail(body)}")

    def _page(self, body: Mapping[str, Any], status: int) -> Page:
        users = {u["id"]: str(u.get("username", "")).lower()
                 for u in (body.get("includes", {}) or {}).get("users", [])}
        posts = []
        for t in body.get("data", []) or []:
            try:
                created = parse_created_at(t["created_at"])
            except (KeyError, ValueError):
                continue
            posts.append(Post(post_id=str(t["id"]),
                              author=users.get(t.get("author_id")),
                              created_at=created,
                              text=t.get("text", "")))
        self.posts_read += len(posts)
        meta = body.get("meta", {}) or {}
        return Page(posts=tuple(posts), next_cursor=meta.get("next_token"),
                    http_status=status, rate=self.last_rate,
                    newest_id=meta.get("newest_id"))

    def verify(self) -> Page:
        """Prove the key works without paying for it: reads are billed per post
        returned, and a query that cannot match anything returns none."""
        return self.search(f'"tracekeycheck{secrets.token_hex(12)}"',
                           max_results=MIN_RESULTS)
