# Putting it on a server

The collector is a thing that has to be awake at four in the morning, because
that is when a coin launches. A laptop is the wrong place for it: it sleeps, it
travels, it gets closed. This is how to move it to a machine that does not.

Nothing here is TRACE-specific cleverness. It is a small Python process, one
sqlite file, and systemd.

---

## What you are paying for

| | |
|---|---|
| the box | about €4 a month. A collector that polls ten tokens every five minutes uses a rounding error of one CPU |
| the feed | measured on a real day: **$0.02/hour, about $0.53/day** at ten tokens. This is the number that matters |
| the chain | nothing. Robinhood Chain's public RPC is free and discovery only reads it |

The bill scales with **posts read**, not with visitors. One collector serves
everyone at flat cost, which is the whole reason the attention half is
server-side and the wallet half runs in the visitor's browser.

---

## The short version

On a fresh Ubuntu 24.04 box, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/marlowxbt/trace/main/deploy/install.sh | sudo bash
sudo nano /etc/trace.env            # put the key in
sudo systemctl enable --now trace-collector
journalctl -u trace-collector -f
```

That is it. The script is safe to run twice - running it again is how you
update.

---

## What the script actually does

1. **A user of its own.** `trace`, system account, no shell, no home to log
   into. The collector never runs as root.
2. **`/opt/trace/app`** - the checkout. **`/opt/trace/data`** - the database.
   They are separate on purpose: `git pull` can never touch the one thing on
   the machine that cannot be rebuilt.
3. **`/opt/trace/venv`** - the dependencies. One of them, at runtime.
4. **`/etc/trace.env`**, mode 0600, root-owned - the key, and the only place it
   exists on the machine. systemd hands it to the process, so `ps` never shows
   it and neither does the shell history.
5. **Three services.** The collector, the desk, and a daily backup.
6. **A firewall** allowing ssh and nothing else.

---

## The key

It goes in `/etc/trace.env` and nowhere else.

```
TRACE_X_BEARER=your-key-here
```

Not in `config.toml`, which is world-readable. Not on a command line, where
every other user on the box can read it out of `ps`. Not in the repository -
`config.toml` and `*.db` are gitignored, and there is a CI job that fails a
push carrying anything that can sign or spend.

If a key has ever appeared in a screenshot, a chat, or a terminal somebody
else could see, rotate it. It costs nothing and takes a minute.

---

## Watching it

```bash
systemctl status trace-collector          # is it alive
journalctl -u trace-collector -f          # what it is doing right now
journalctl -u trace-collector --since '1 hour ago' | grep watchlist
```

Every fifteen minutes it prints one of two lines:

```
20:45:00  watchlist: +3 -3
20:45:00  watchlist: re-checked, nothing moved (10 held)
```

The second one matters as much as the first. A watchlist that has not rotated
in five hours looks exactly like a refresh that never ran, and the difference
is money: attention on this chain falls about thirtyfold within two hours of
launch, so polling this morning's coins is paying to watch a funeral.

---

## The desk

It binds `127.0.0.1` and stays there. There is no login on that page because
there is nothing to log in to - it reads one sqlite file and hands it to
whoever is already on the machine. Exposing it would mean putting a page full
of posts you paid for, and handles, on the open internet behind a password
somebody has to remember.

Reach it from your laptop instead:

```bash
ssh -N -L 8080:127.0.0.1:8080 root@your-server
# then open http://127.0.0.1:8080
```

The tunnel is the authentication. Close the terminal and the desk is
unreachable again.

---

## Backups

A timer runs at 03:17 UTC, takes a proper `sqlite3 .backup` (not `cp` - the
collector is writing, and a file copied mid-write is a corrupt file), gzips it
and keeps seven.

```bash
ls -la /opt/trace/data/backups/
systemctl list-timers trace-backup
```

To pull one down:

```bash
scp root@your-server:/opt/trace/data/backups/trace-20260909.db.gz .
```

Every post in that database was paid for once. The feed will not sell the same
history back to you cheaply, which is what makes it the only irreplaceable
thing on the machine.

---

## Updating

```bash
sudo bash /opt/trace/app/deploy/install.sh      # pulls, reinstalls, restarts
```

or by hand:

```bash
sudo -u trace git -C /opt/trace/app pull
sudo systemctl restart trace-collector trace-desk
```

The database is outside the checkout, so an update never touches it.

---

## When it goes wrong

**Restarting every twenty seconds.** `journalctl -u trace-collector -n 50`.
Usually the key: a placeholder still in `/etc/trace.env`, or one that has been
rotated. The collector refuses to start on a bad key rather than burning
through requests finding out.

**`watchlist refresh failed`.** The chain RPC is unreachable from the server.
The collector keeps polling the list it already has rather than stopping, which
is right, but it is now watching a list that ages. Check with
`curl -s -X POST https://rpc.mainnet.chain.robinhood.com -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber"}'`.

**The bill climbing.** `daily_budget_usd` in `config.toml` stops polling dead
when it is hit - it does not quietly degrade. Raise it deliberately, having
looked at what the last day actually cost.

**Disk.** The database grew to 323 KB in five hours of light traffic. If it
ever becomes a problem you have a much better problem than disk.
