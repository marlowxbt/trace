"""The live desk: the collector's own database, served to a browser.

The published marketing page is a snapshot and always will be - a hosted
artifact is not allowed to make network calls, so nothing on it can refresh.
This is the other thing: a small server that runs next to the collector on the
same machine and hands the browser whatever is in `trace.db` *right now*.

    python -m trace.serve                 # then open http://127.0.0.1:8080

It binds to loopback on purpose. There is no authentication here because there
is nothing to authenticate: it reads one sqlite file and serves it to whoever
is sitting at this computer. It never touches the X key, never makes an
outbound request, and cannot spend a cent - polling this page a thousand times
costs exactly nothing, because the collector, not the page, is what buys posts.

What it serves:

    /                     the desk
    /api/live.json        the whole current state, rebuilt per request
    /<file>               anything else in web/

The JSON carries handles, text and profile pictures, because this page is for
the person who paid for those posts, running on their own machine. The public
snapshot (`trace.export`) is the one with rules about what may leave.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import callers as _callers
from . import config as _config
from . import db as _db
from . import export as _export

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
RECENT_POSTS = 60


def recent_posts(conn: sqlite3.Connection, limit: int = RECENT_POSTS) -> list[dict]:
    """The newest posts the collector holds, counted or not.

    Both kinds are here on purpose. A feed that showed only what counted would
    hide the work: two thirds of what we pay for is somebody else's asset
    wearing the same ticker, and watching that get refused is how you come to
    trust the number that survives.
    """
    rows = conn.execute(
        """SELECT p.post_id, p.author, p.created_at, p.text, p.matched,
                  t.symbol, t.address, t.launch_ts,
                  a.name AS author_name, a.avatar AS author_avatar
             FROM posts p
             JOIN tokens t ON t.address = p.token_address
        LEFT JOIN authors a ON a.handle = p.author
            ORDER BY p.created_at DESC
            LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        why = None
        if r["matched"] is None:
            why = _export.why_not(r["text"], r["address"], r["symbol"],
                                  r["created_at"], r["launch_ts"])
        out.append({
            "post_id": r["post_id"],
            "author": r["author"],
            "name": r["author_name"] or r["author"],
            "avatar": r["author_avatar"],
            "created_at": r["created_at"],
            "age_min": (max(0, r["created_at"] - (r["launch_ts"] or r["created_at"]))) // 60,
            "symbol": r["symbol"],
            "address": r["address"],
            "matched": r["matched"],
            "why": why,
            "text": r["text"],
            "url": ("https://x.com/%s/status/%s" % (r["author"], r["post_id"])
                    if r["author"] else None),
        })
    return out


def health(conn: sqlite3.Connection, window_s: int) -> dict:
    """Is the collector actually running?

    A live page that quietly shows stale numbers is worse than one that says
    it is stale, so this is reported rather than hidden.
    """
    row = conn.execute("SELECT MAX(ts) FROM reads").fetchone()
    last = row[0] or 0
    now = int(time.time())
    return {"last_read_ts": last,
            "seconds_ago": (now - last) if last else None,
            # one missed round is a hiccup; two means it stopped
            "running": bool(last) and (now - last) < window_s * 2 + 60}


def live(conn: sqlite3.Connection, cfg) -> dict:
    data = _export.snapshot(conn, cfg)
    data["posts"] = recent_posts(conn)
    data["health"] = health(conn, cfg.detector.rate_window_min * 60)
    # The caller list travels beside the posts, never inside them. A post is the
    # same object whether or not its author is on the list, which is the whole
    # claim: the list is a layer the page paints, not an input to anything.
    data["callers"] = _callers.build(conn)
    data["caller_rules"] = _callers.RULES
    return data


class Desk(BaseHTTPRequestHandler):
    server_version = "trace"

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                    # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            path = "/desk.html"
        if path == "/api/live.json":
            try:
                conn = _db.connect(self.server.cfg.db_file)
                body = json.dumps(live(conn, self.server.cfg)).encode("utf-8")
                conn.close()
            except Exception as exc:                     # never 500 silently
                body = json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}
                                  ).encode("utf-8")
                return self._send(500, body, "application/json")
            return self._send(200, body, "application/json")

        # Static files, confined to web/. A path that resolves outside it is
        # not a file we serve, whatever it spells.
        target = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not target.startswith(WEB + os.sep) or not os.path.isfile(target):
            return self._send(404, b"not here", "text/plain")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def log_message(self, fmt, *args):
        if "/api/" not in (args[0] if args else ""):
            return                                       # the poll is not news
        sys.stderr.write("  %s\n" % (fmt % args))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="serve the live desk")
    ap.add_argument("--config", default=None)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    cfg = _config.load(args.config)
    conn = _db.connect(cfg.db_file)
    h = health(conn, cfg.detector.rate_window_min * 60)
    n = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    cal = _callers.build(conn)
    conn.close()

    srv = ThreadingHTTPServer((args.host, args.port), Desk)
    srv.cfg = cfg
    print("TRACE desk  http://%s:%d" % (args.host, args.port), file=sys.stderr)
    print("  database %s - %d posts held" % (cfg.db_file, n), file=sys.stderr)
    print("  callers  %d on the list of %d counted accounts"
          % (sum(1 for c in cal if c["listed"]), len(cal)), file=sys.stderr)
    print("  collector %s" % ("running" if h["running"] else
                              "NOT running - start it in another terminal:\n"
                              "      python3 -m trace.collector"), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
