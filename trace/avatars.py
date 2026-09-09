"""Profile pictures for the accounts a snapshot quotes.

The site is published as a single self-contained page, and that page may not
load an image from another host. So an avatar has to arrive as bytes and get
baked in as a `data:` URI, or it does not arrive at all. This fetches the
picture for every handle in a snapshot and writes them out as one JSON map
of handle -> {name, avatar}.

Cost: the user-info endpoint is charged per lookup, not per post, and every
handle is fetched once and cached in the output file. Forty accounts is well
under a cent. Re-running with an existing --out only fetches what is missing.

Run:  python -m trace.avatars --snapshot web/public.json --out web/avatars.json
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

from .feed import AuthError, check_key

USER_INFO = "https://api.twitterapi.io/twitter/user/info"
# 48px is what X hands out by default and it is soft on a retina screen; the
# 73px variant is the next size up and still only a few kilobytes.
SIZE = "_bigger"
MAX_BYTES = 64 * 1024


def handles(snapshot: dict) -> list[str]:
    posts = snapshot.get("posts") or {}
    seen = []
    for group in ("counted", "rejected"):
        for p in posts.get(group, []):
            if p.get("author") and p["author"] not in seen:
                seen.append(p["author"])
    return seen


def resize_url(url: str) -> str:
    for variant in ("_normal", "_mini", "_bigger", "_400x400"):
        if variant in url:
            return url.replace(variant, SIZE)
    return url


def fetch_user(handle: str, key: str, timeout: float = 20.0) -> dict | None:
    req = urllib.request.Request(
        f"{USER_INFO}?userName={handle}", headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    data = body.get("data") or body
    if not isinstance(data, dict) or not data.get("userName"):
        return None
    return data


def fetch_image(url: str, timeout: float = 20.0) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "trace/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read(MAX_BYTES + 1)
        kind = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if not blob or len(blob) > MAX_BYTES or not kind.startswith("image/"):
        return None
    return f"data:{kind};base64," + base64.b64encode(blob).decode("ascii")


def build(snapshot: dict, key: str, have: dict, log=sys.stderr) -> dict:
    out = dict(have)
    for h in handles(snapshot):
        if h in out and out[h].get("avatar"):
            continue
        try:
            user = fetch_user(h, key)
            if user is None:
                print(f"  {h}: no such account", file=log)
                continue
            pic = user.get("profilePicture") or ""
            img = fetch_image(resize_url(pic)) if pic else None
            out[h] = {"name": user.get("name") or h, "avatar": img,
                      "verified": bool(user.get("isBlueVerified"))}
            print(f"  {h}: {'ok' if img else 'no picture'}", file=log)
        except Exception as exc:                      # one bad handle is not fatal
            print(f"  {h}: {type(exc).__name__}: {exc}", file=log)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="bake avatars into a snapshot")
    ap.add_argument("--snapshot", default="web/public.json")
    ap.add_argument("--out", default="web/avatars.json")
    args = ap.parse_args(argv)

    try:
        key = check_key(os.environ.get("TRACE_X_BEARER", ""))
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with open(args.snapshot) as fh:
        snap = json.load(fh)
    if not snap.get("posts"):
        print("error: that snapshot has no posts in it. Re-run the export "
              "with --with-posts first.", file=sys.stderr)
        return 2

    have = {}
    if os.path.exists(args.out):
        with open(args.out) as fh:
            have = json.load(fh)
        print(f"{len(have)} already cached in {args.out}", file=sys.stderr)

    out = build(snap, key, have)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    got = sum(1 for v in out.values() if v.get("avatar"))
    print(f"wrote {args.out}: {got}/{len(out)} with a picture", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
