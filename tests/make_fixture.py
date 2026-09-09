"""Build the replay fixture. Run once; the JSONL it writes is what the test
reads, so the test replays a recording rather than re-deriving it.

    python -m tests.make_fixture

Timeline, in minutes from T0. The replay ticks minute by minute over
[60, 120] so every token except D has a full hour of history behind it.

  A  QUIET      1 post a minute, all the way through. rate 5, baseline 5,
                multiplier 1.0. Must never leave QUIET.
  B  SPIKE      2 posts per 5-minute window, then 45 posts in [75,80), then
                quiet again. Fires once; the cooldown must swallow the four
                or five following ticks that still see the burst.
  C  FLOOR      1 post per window, then 4 posts in [90,95). Multiplier 4.0,
                rate 4. Under the floor, so it must never fire - this is the
                "1 post became 4 posts, 4x!!" case the floor exists for.
  D  WARM-UP    collection starts at minute 108, 50 posts in [115,120).
                Twelve minutes of history is under the twenty required.
  E  COOLDOWN   2 per window throughout, plus 40-post bursts at [70,75),
                [90,95) and [110,115). The second lands inside the cooldown
                the first opened and must be swallowed; the third lands after
                it expired and must fire.

Expected: 3 SPIKEs in total - one for B, two for E, none for anyone else.
"""

from __future__ import annotations

import json
from pathlib import Path

T0 = 1_780_000_000
OUT = Path(__file__).parent / "fixtures" / "replay_hour.jsonl"

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
D = "0x" + "d" * 40
E = "0x" + "e" * 40

COLLECTION_START = {A: T0, B: T0, C: T0, D: T0 + 108 * 60, E: T0}
REPLAY_START, REPLAY_END = T0 + 60 * 60, T0 + 120 * 60


def main() -> None:
    posts: list[dict] = []
    n = 0

    def add(token: str, at_s: int, author: str) -> None:
        nonlocal n
        n += 1
        posts.append({"post_id": f"p{n:06d}", "token": token,
                      "author": author, "created_at": at_s})

    # A: one a minute for two hours.
    for m in range(0, 120):
        add(A, T0 + m * 60, f"@a{m % 7}")

    # B: two per window, placed at the very start of each window so the burst
    # window contains nothing but burst.
    for w in range(0, 15):            # windows [0,5) .. [70,75)
        add(B, T0 + w * 300 + 0, f"@b{w % 5}")
        add(B, T0 + w * 300 + 30, f"@b{(w + 2) % 5}")
    for i in range(45):               # the burst, spread over [75,80)
        add(B, T0 + 75 * 60 + i * (300 // 45), "@first_on_b" if i == 0 else f"@bb{i % 11}")
    for w in range(16, 24):           # back to quiet
        add(B, T0 + w * 300 + 0, f"@b{w % 5}")
        add(B, T0 + w * 300 + 30, f"@b{(w + 2) % 5}")

    # C: one per window, then four in one window.
    for w in range(0, 24):
        if w == 18:                   # [90,95)
            for i in range(4):
                add(C, T0 + 90 * 60 + i * 60, f"@c_hype{i}")
            continue
        add(C, T0 + w * 300, f"@c{w % 4}")

    # D: nothing until collection starts, then a big burst.
    for i in range(50):
        add(D, T0 + 115 * 60 + i * (300 // 50), f"@d{i % 9}")

    # E: steady, with three bursts.
    bursts = {14, 18, 22}             # windows [70,75), [90,95), [110,115)
    for w in range(0, 24):
        add(E, T0 + w * 300 + 0, f"@e{w % 6}")
        add(E, T0 + w * 300 + 30, f"@e{(w + 3) % 6}")
        if w in bursts:
            for i in range(40):
                add(E, T0 + w * 300 + 60 + i * 6, f"@ee{w}_{i % 13}")

    posts.sort(key=lambda p: (p["created_at"], p["post_id"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for p in posts:
            fh.write(json.dumps(p, separators=(",", ":")) + "\n")
    print(f"wrote {len(posts)} posts to {OUT}")


if __name__ == "__main__":
    main()
