#!/usr/bin/env python3
"""Prove constraint 1 is still true: nothing here can sign or broadcast.

The README says "no signer, no private key, ever" and `trace/rpc.py` enforces it
with an allow-list. An allow-list is only worth what the surrounding code is
worth, though - a year of commits is plenty of time for somebody to add a second
HTTP call that skips it, in good faith, at midnight.

So this runs on every push. It reads every source file in the project and fails
if it finds a way to move funds: a write-side JSON-RPC method, a signing library,
or a private key being read out of the environment.

Two rules keep it from crying wolf:

  * `trace/rpc.py` may name the write methods, because refusing them is its job.
  * A line ending in `# no-signer-ok` is exempt, and had better say why.

Run: python scripts/no_signer.py     (or `make guard`)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Anything that could put a transaction on a chain, or hold the means to.
FORBIDDEN = {
    # write-side JSON-RPC
    "eth_sendTransaction": "broadcasts a transaction",
    "eth_sendRawTransaction": "broadcasts a signed transaction",
    "eth_sign": "signs data with a node-held key",
    "eth_signTransaction": "signs a transaction",
    "personal_sign": "signs data with a node-held key",
    "personal_sendTransaction": "broadcasts a transaction",
    "personal_unlockAccount": "unlocks a node-held key",
    "eth_requestAccounts": "asks a wallet for signing access",
    "wallet_requestPermissions": "asks a wallet for signing access",
    # signing libraries and key material
    "from eth_account": "a signing library",
    "import eth_account": "a signing library",
    "LocalAccount": "a signing library",
    "signTypedData": "signs data",
    "sendTransaction": "broadcasts a transaction",
    "PRIVATE_KEY": "private key material",
    "privateKey": "private key material",
    "mnemonic": "seed phrase material",
    "window.ethereum": "an injected wallet, i.e. a signer",
}

# The file whose whole job is to name these in order to refuse them.
ALLOWED_TO_NAME = {"trace/rpc.py", "scripts/no_signer.py", "web/book.html"}

SKIP_DIRS = {".git", "venv", ".venv", "__pycache__", "node_modules", "assets"}
SUFFIXES = {".py", ".html", ".js", ".toml", ".sql", ".sh"}


def sources():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        yield p


def main() -> int:
    hits = []
    scanned = 0
    for path in sources():
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if line.rstrip().endswith("# no-signer-ok"):
                continue
            for needle, why in FORBIDDEN.items():
                if needle not in line:
                    continue
                # web/book.html and rpc.py name these to refuse them; that is
                # only acceptable where the surrounding line is a refusal.
                if rel in ALLOWED_TO_NAME:
                    continue
                hits.append((rel, n, needle, why, line.strip()[:88]))

    if hits:
        print("no_signer: FAILED - this project must never be able to sign or "
              "broadcast.\n", file=sys.stderr)
        for rel, n, needle, why, line in hits:
            print(f"  {rel}:{n}\n    {needle}  ({why})\n    {line}\n",
                  file=sys.stderr)
        print("If a hit is genuinely a refusal, end the line with "
              "'# no-signer-ok' and say why.", file=sys.stderr)
        return 1

    print(f"no_signer: ok - {scanned} files scanned, no way to sign or "
          f"broadcast found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
