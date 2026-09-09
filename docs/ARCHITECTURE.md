# Architecture

```
                        Robinhood Chain (id 4663)
                                  │
                          eth_getLogs, eth_call
                                  ▼
                        ┌───────────────────┐
                        │  trace.watchlist  │  Pons v2 factory → traction →
                        └─────────┬─────────┘  ten tokens, hysteresis, 4h sticky
                                  │
                                  ▼
        paid feed ───▶  ┌───────────────────┐
   twitterapi.io / X    │  trace.collector  │  one poll per window per token
                        └─────────┬─────────┘
                                  │  every post stored, matched or not
                                  ▼
                        ┌───────────────────┐
                        │  trace.attribution│  contract address, or cashtag
                        └─────────┬─────────┘  + chain marker. Vetoes.
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
          ┌───────────────────┐       ┌───────────────────┐
          │   trace.cohort    │──────▶│  trace.detector   │  QUIET / WARM / SPIKE
          │ normal-for-an-age │       └─────────┬─────────┘
          └───────────────────┘                 │
                                    ┌───────────┴───────────┐
                                    ▼                       ▼
                          ┌───────────────────┐   ┌───────────────────┐
                          │   trace.export    │   │    trace.serve    │
                          │  public snapshot  │   │  the live desk    │
                          └───────────────────┘   └─────────┬─────────┘
                                                            │ localhost only
                                                            ▼
                                                     web/desk.html

          web/book.html ──── your browser ────▶ any RPC you choose
                            (never our server)
```

---

## The modules

| module | lines | what it owns |
|---|---|---|
| `rpc.py` | 226 | JSON-RPC, range splitting, the read-only allow-list |
| `chain.py` | 145 | factory logs, ERC-20 reads, hand-rolled ABI decode |
| `watchlist.py` | 324 | traction scoring, hysteresis, eviction |
| `feed.py` | 195 | the provider vocabulary — `Post`, `Page`, the errors |
| `twitterapi.py` | 166 | the cheap provider, and the default |
| `xapi.py` | 157 | the official X provider, kept working |
| `attribution.py` | 115 | what counts as a post about *this* token |
| `collector.py` | 970 | the loop, the cursor, the budget, the CLI |
| `cohort.py` | 124 | normal-for-an-age, rebuilt from the corpus |
| `detector.py` | 409 | windows, baselines, verdicts, replay |
| `callers.py` | 128 | who keeps showing up |
| `export.py` | 295 | the public snapshot |
| `serve.py` | 179 | the live desk, loopback only |
| `db.py`, `config.py`, `accounts.py` | 290 | schema, migrations, settings, the list |

One runtime dependency. Everything else is the standard library, which is not
austerity for its own sake — it is what lets a stranger clone this and have it
running before deciding whether to trust it.

---

## Three boundaries that matter

### 1. The transport boundary

Every network call in the project goes through a transport function:

```python
Transport = "callable(url, params, headers) -> (status, headers, json)"
```

Which is why the entire collector runs in tests, offline, for free, before a
cent is spent. Swap the transport, feed it recorded pages, and the loop, the
cursor arithmetic, the budget stop and the attribution filter all execute for
real against fixtures.

### 2. The provider boundary

The detector never learns which feed paid. `since_id` versus
`since_time:` inside a query string, credits versus dollars, twenty posts a page
versus a hundred — all of that stops at `feed.Provider` and none of it reaches
anything that produces a verdict.

The practical payoff: a 33× price difference between feeds is a configuration
line, not a rewrite.

### 3. The server/browser boundary

The attention half is server-side because its cost scales with posts read, not
with users — one collector serves everybody flat.

The wallet half is browser-side because its cost scales with users and its risk
scales with what a server would have to be told. It is not "we promise not to
log your address"; it is that no address is ever sent, so there is nothing to
log, and `trace.export` could not leak wallet data if it tried because there is
none in the database.

Those two facts are the same fact seen from either end, and between them they
determine the whole shape of this repository.

---

## Data, briefly

```sql
tokens         address, symbol, launch_ts, first_seen_ts
cursors        token_address, since_id, updated_ts, observed_from
posts          post_id, token_address, author, created_at, text, matched
authors        handle, name, avatar, seen_ts
minute_counts  rebuilt from posts, never incremented
reads          every request, what it returned, what it cost
positions      caller stakes from pump.fun callouts (source-labelled)
```

Two of those deserve a note:

- **`minute_counts` is rebuilt, never incremented.** It is then a pure function
  of `posts` and cannot drift out of step with it after a crash or a duplicated
  page. Rebuilding is cheap; a silently wrong count is not.
- **`cursors.observed_from`** is the moment we first *asked* the feed about a
  token, which is not when it entered the watchlist. The whole history rule
  hangs off that column — see [DETECTOR.md](DETECTOR.md).

---

## The two front ends

**`web/book.html`** — one dependency-free file. Real JSON-RPC, `eth_getLogs`
with range splitting, hand-rolled ABI decode, ERC-20 balances and transfer
totals. It refuses any non-read-only method before the request leaves the page.
Serve it over `http://` rather than `file://`; some nodes refuse `file://`
origins.

**`web/desk.html`** — the live terminal, served by `trace.serve` from
`127.0.0.1`. Polls `/api/live.json` every fifteen seconds, which is rebuilt per
request straight out of SQLite. It never talks to X: the collector buys posts,
the desk reads what was already bought, so leaving it open all day costs
nothing. If the collector stops, the page says so in red instead of quietly
showing stale numbers.
