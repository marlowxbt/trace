"""Config loading. TOML via stdlib tomllib, so the collector has one
dependency (requests) and nothing to install for config parsing."""

from __future__ import annotations

try:
    import tomllib                     # Python 3.11+
except ModuleNotFoundError:            # 3.10 self-hosters
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ChainConfig:
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    chain_id: int = 4663
    factory: str = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
    timeout_s: int = 20
    log_page_blocks: int = 50_000


@dataclass(frozen=True)
class WatchlistConfig:
    max_tokens: int = 10
    pinned_tokens: tuple[str, ...] = ()
    discovery_window_min: int = 120
    min_age_min: int = 10
    max_age_hours: int = 72
    traction_window_min: int = 15
    enter_min_transfers: int = 25
    enter_min_holders: int = 12
    stay_min_transfers: int = 5
    stay_min_holders: int = 3
    displace_factor: float = 2.0
    ticker_min_len: int = 2
    ticker_max_len: int = 15


@dataclass(frozen=True)
class DetectorConfig:
    rate_window_min: int = 5
    baseline_windows: int = 6      # must match detector.Params - see the toml
    floor: int = 5
    cooldown_min: int = 30
    warm_multiplier: float = 1.5
    spike_multiplier: float = 3.0
    min_baseline: float = 1.0

    @property
    def window_s(self) -> int:
        return self.rate_window_min * 60


@dataclass(frozen=True)
class XConfig:
    # Which paid feed to read. "twitterapi.io" is a third party and costs
    # $0.15/1k posts; "x" is the official API and costs $5.00/1k. The detector
    # cannot tell them apart - see trace/feed.py.
    provider: str = "twitterapi.io"
    api_key: str = ""               # prefer the TRACE_X_BEARER env var
    query_type: str = "Latest"      # twitterapi.io only: Latest | Top
    # 0 means "wake just after each detector window closes". Polling faster
    # than the window buys nothing - the detector only rates windows that have
    # already closed - and on twitterapi.io every call costs at least 15
    # credits whether or not anybody posted.
    poll_interval_s: int = 0
    poll_delay_s: int = 20          # grace after the boundary, for slow indexing
    # Re-ask this far behind the last poll, so a post that landed mid-poll is
    # not lost at the seam. Duplicates are dropped on post_id; the only cost is
    # a few re-read posts, which is $0.0003 a poll on twitterapi.io.
    overlap_s: int = 30
    # How far back to read the first time a token is polled.
    #
    # Measured on live data: a token joins the watchlist *because* it is
    # already getting traction, so the burst is happening at that moment - and
    # the detector needs 35 minutes of baseline before it may speak. Without
    # this, TRACE structurally misses the one event that got the token onto the
    # list. One historical read at admission buys the baseline outright, and it
    # is not a fudge: we ask the feed about those windows and it answers, so
    # they are observed, just observed late.
    backfill_min: int = 60
    # Hard stop, not a throttle. When the next request could cross this line the
    # collector exits rather than polling less or trimming pages: a detector fed
    # a thinned stream calls QUIET on a token that is screaming.
    daily_budget_usd: float = 1.00
    max_pages_per_poll: int = 10
    # Exclude posts containing links. Off: on neither provider does a link cost
    # more to read, and most organic posts about a new contract carry a screener
    # link. The $0.20 link price everyone quotes is for *writing* a post.
    exclude_links: bool = False


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "trace.db"


@dataclass(frozen=True)
class Config:
    chain: ChainConfig = field(default_factory=ChainConfig)
    watchlist: WatchlistConfig = field(default_factory=WatchlistConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    x: XConfig = field(default_factory=XConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @property
    def db_file(self) -> Path:
        p = Path(self.storage.db_path)
        return p if p.is_absolute() else REPO_ROOT / p


def _section(raw: dict[str, Any], name: str, cls):
    known = {f for f in cls.__dataclass_fields__}
    given = raw.get(name, {}) or {}
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"[{name}] has unknown keys: {sorted(unknown)}")
    if name == "watchlist" and "pinned_tokens" in given:
        given = dict(given, pinned_tokens=tuple(given["pinned_tokens"]))
    return cls(**given)


def load(path: str | Path | None = None) -> Config:
    """Load config.toml, falling back to config.example.toml, then defaults."""
    if path is None:
        for cand in (REPO_ROOT / "config.toml", REPO_ROOT / "config.example.toml"):
            if cand.exists():
                path = cand
                break
        else:
            return Config()
    raw = tomllib.loads(Path(path).read_text())
    return Config(
        chain=_section(raw, "chain", ChainConfig),
        watchlist=_section(raw, "watchlist", WatchlistConfig),
        detector=_section(raw, "detector", DetectorConfig),
        x=_section(raw, "x", XConfig),
        storage=_section(raw, "storage", StorageConfig),
    )


def replace_x(cfg: Config, **kw) -> Config:
    """Copy of cfg with some [x] fields overridden - for CLI flags."""
    from dataclasses import replace
    return replace(cfg, x=replace(cfg.x, **kw))
