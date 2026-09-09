# Safety

Four constraints. Each one is a line of code and a test, not a promise in a
README, because a promise in a README is what every drained wallet was holding.

---

## 1. No signer, no private key, ever

TRACE never builds, signs or sends a transaction. There is nowhere in this
repository that could: no key material is read, no signing library is imported,
and the RPC client will not carry a method that could broadcast one.

The enforcement is an **allow-list**, not a deny-list — `READ_ONLY_METHODS` in
[`trace/rpc.py`](../trace/rpc.py). A deny-list is a list of the attacks somebody
already thought of; an allow-list is a list of what the program is for. A method
that is not on it does not leave the process:

```python
if method not in READ_ONLY_METHODS:
    raise ValueError(f"{method} is not a read-only method")
```

The browser half does the same thing in the browser, before the request leaves
the page — see the guard in [`web/book.html`](../web/book.html).

**What this costs you:** TRACE can never execute anything for you. That is the
point. If you want a tool that buys, this is not it and will not become it.

---

## 2. No buy button

No link anywhere pre-fills a swap, and no page carries a referral or affiliate
parameter to a DEX. A contract address on a page is a contract address; copying
it and deciding what to do is your job.

This one is a rule about product, not about code, so it is easy to erode by
accident. If you are adding a link, the test is simple: could a bored person
click it and end up holding a token? Then it does not go in.

---

## 3. No price prediction

No target, no score, no buy/sell verdict, no "confidence". The output vocabulary
is four words — `WARMING`, `QUIET`, `WARM`, `SPIKE` — and every one of them is a
statement about **how many people are posting**, derived from counts by
arithmetic you can redo by hand.

The detector takes counts and returns a verdict. It has no access to price, to
liquidity, to holders, or to anything else that could tempt it into an opinion
about value. That is a deliberate amputation.

---

## 4. Your wallet never touches our server

The wallet half runs entirely in your browser: address entry, `eth_call`,
`eth_getLogs`, balance reads, and every line of PnL arithmetic. No address is
logged, stored, proxied or sent anywhere. Point it at whatever RPC you like,
including one you run.

There is a second half to this rule that is easy to miss: **no user timestamp
reaches the server either.** Anything that would require the server to learn
*when* you traded is forbidden, including "it's just a lookup". The server
serves one public series that is byte-identical for every visitor, which is what
makes it safe to bake straight into a page.

`trace/export.py` is the only thing that produces a public file, and it is
structurally incapable of leaking wallet data — there is none in the database to
leak. That is not luck; it is what building it browser-side buys.

---

## What the tests actually check

```bash
make test
```

| test | what would otherwise get through |
|---|---|
| RPC method allow-list | a write method reaching a node |
| detector never reads the account list | a "known caller" quietly changing a verdict |
| `config.example.toml` matches the detector defaults | shipping a rule the tests never checked |
| a cold token buys one minimum page | $2.50 a token on first contact |
| history begins at `observed_from` | unobserved windows counted as zeros, firing on noise |
| API key is ASCII and not a placeholder | a pasted example string producing a 401 at 3am |

CI additionally greps the source on every push for signing and
transaction-broadcasting calls, so constraint 1 cannot rot quietly over a year
of commits.

---

## What TRACE does hold

Honesty about the other side of the ledger:

- **Posts.** Post id, author handle, text, timestamp, and whether it counted —
  in a local SQLite file on the machine running the collector. These are public
  posts, bought from a paid API, and keeping them is what makes every number
  auditable after the fact. The public snapshot includes them only when you pass
  `--with-posts`, and always with a link back to the original.
- **Author names and profile pictures.** Handed back by the provider inside the
  same response we already paid for. Stored so the desk can render a face
  without paying twice.
- **Your API key.** Never. It is read from `TRACE_X_BEARER` in your environment;
  the config file has a slot for it and the README tells you not to use it.

No wallet addresses. No user identities. No analytics. No telemetry. The
collector makes exactly one kind of outbound request — to the feed you
configured — and the desk makes none at all.
