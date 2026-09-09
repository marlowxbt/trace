-- TRACE storage. One SQLite file. No address of any site visitor is ever
-- written here: the wallet half of the product runs entirely in the browser
-- (hard constraint 4). Everything below is public chain data or public posts.

PRAGMA journal_mode = WAL;

-- stage 1 -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tokens (
    address      TEXT PRIMARY KEY,     -- lowercase 0x...
    symbol       TEXT,
    name         TEXT,
    decimals     INTEGER,
    launch_block INTEGER NOT NULL,
    launch_ts    INTEGER,              -- unix seconds, from the launch block
    related      TEXT,                 -- comma separated contracts from the event
    first_seen_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    address    TEXT PRIMARY KEY REFERENCES tokens(address),
    source     TEXT NOT NULL,          -- 'auto' | 'pin'
    added_ts   INTEGER NOT NULL,
    removed_ts INTEGER,                -- NULL means currently watched
    holders    INTEGER,                -- last measured traction
    transfers  INTEGER,
    scored_ts  INTEGER
);
CREATE INDEX IF NOT EXISTS watchlist_active ON watchlist(removed_ts);

-- Why a token entered or left. The watchlist decides what we spend money on,
-- so it keeps a paper trail.
CREATE TABLE IF NOT EXISTS watchlist_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    address TEXT NOT NULL,
    action  TEXT NOT NULL,             -- 'add' | 'remove'
    reason  TEXT
);

-- stage 2 -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS authors (
    handle   TEXT PRIMARY KEY,
    name     TEXT,
    avatar   TEXT,
    seen_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    post_id       TEXT PRIMARY KEY,
    token_address TEXT NOT NULL,
    author        TEXT,
    created_at    INTEGER NOT NULL,    -- unix seconds
    text          TEXT,                -- counted, never republished through the API
    ingested_at   INTEGER NOT NULL,
    -- Which attribution rule let this post count: 'address', 'cashtag+chain',
    -- or NULL for a post we paid for, kept as evidence, and do not count.
    -- See trace/attribution.py: $TER is Teradyne on the NASDAQ, and 37 posts
    -- about it are not attention on a memecoin that happens to share a ticker.
    matched       TEXT
);
CREATE INDEX IF NOT EXISTS posts_token_time ON posts(token_address, created_at);

-- The cursor is cost control, not convenience: X bills per post read, so a
-- poll without since_id pays again for posts we already hold.
CREATE TABLE IF NOT EXISTS cursors (
    token_address TEXT PRIMARY KEY,
    since_id      TEXT,
    updated_ts    INTEGER,
    -- The earliest moment we have actually asked the feed about for this
    -- token. This is what the detector must treat as the start of history -
    -- not when the token joined the watchlist. A window before this is
    -- unobserved, and an unobserved window is missing, not zero.
    observed_from INTEGER
);

CREATE TABLE IF NOT EXISTS reads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    token_address TEXT NOT NULL,
    posts_returned INTEGER NOT NULL,
    link_posts    INTEGER NOT NULL DEFAULT 0,
    est_cost_usd  REAL NOT NULL,
    since_id      TEXT,
    http_status   INTEGER
);
CREATE INDEX IF NOT EXISTS reads_ts ON reads(ts);

-- stage 3 -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS minute_counts (
    token_address TEXT NOT NULL,
    minute_ts     INTEGER NOT NULL,    -- unix seconds, floored to the minute
    n             INTEGER NOT NULL,
    PRIMARY KEY (token_address, minute_ts)
);

CREATE TABLE IF NOT EXISTS token_state (
    token_address TEXT PRIMARY KEY,
    state         TEXT NOT NULL,       -- WARMING | QUIET | WARM | SPIKE
    rate          INTEGER NOT NULL,
    baseline      REAL NOT NULL,
    multiplier    REAL NOT NULL,
    first_author  TEXT,
    first_post_ts INTEGER,
    cooldown_until INTEGER,
    updated_ts    INTEGER NOT NULL
);

-- second lane ---------------------------------------------------------------
-- How much money stands behind the accounts doing the talking. Never part of a
-- verdict: pump.fun pays people to make callouts, so the count of them is
-- bought volume, and a number you can buy must not move a state. The position
-- is the part that costs real money to fake, and it is the only part kept.
-- `source` is always recorded, because this does not come from an API anyone
-- can run for themselves the way the rest of TRACE does.

CREATE TABLE IF NOT EXISTS positions (
    token_address TEXT NOT NULL,
    author        TEXT NOT NULL,     -- X handle, lowercase, joins posts.author
    position_usd  REAL NOT NULL,
    spent_usd     REAL,
    pnl_usd       REAL,
    entry_mc_usd  REAL,              -- average entry, in market cap
    source        TEXT NOT NULL,
    seen_ts       INTEGER NOT NULL,
    PRIMARY KEY (token_address, author)
);
CREATE INDEX IF NOT EXISTS positions_author ON positions(author);
