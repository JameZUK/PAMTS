# Quick Start

About ten minutes, in this order. Do not skip step 3 — without it your media player will
see files move and churn its library.

Throughout, replace `/srv/fast` and `/srv/slow` with your own paths.

---

## 0. Check the prerequisites

```sh
python3 --version        # need 3.11 or newer (for tomllib)
rsync --version          # need rsync
```

You also need:
- a media player with an API (Plex is implemented; see [PLAYERS.md](PLAYERS.md))
- two directory trees on different storage: one small and fast, one large and slow
- root, or a user that can read/write both trees

## 1. Install

```sh
git clone https://github.com/JameZUK/PAMTS.git
cd PAMTS
sudo install -m 755 pamts-tier.py pamts-promote.py /usr/local/bin/
sudo install -m 644 pamts.py pamts_players.py /usr/local/bin/
sudo mkdir -p /etc/pamts /var/lib/pamts
```

> The two libraries go next to the scripts so `import pamts` resolves. If you prefer
> them elsewhere, put that directory on `PYTHONPATH` in the systemd units.

## 2. Get a player token

**Plex.** The simplest source is the server's own `Preferences.xml`:

```sh
sudo grep -o 'PlexOnlineToken="[^"]*"' \
  "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"
```

(Path varies by install — Docker images usually put it under `/config`.) Alternatively
follow Plex's own [Finding an authentication
token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
article.

Write it to a root-only file. **Never** put the token in `pamts.toml`, and never commit
it:

```sh
printf '%s' 'YOUR_TOKEN_HERE' | sudo tee /etc/pamts/player-token >/dev/null
sudo chmod 600 /etc/pamts/player-token
```

## 3. Put a union filesystem in front of your tiers

**This is the step that makes tiering invisible.** PAMTS moves files between
`/srv/fast/tv` and `/srv/slow/tv`. If your player points at both, it sees files appear
and disappear on every run. A union filesystem presents them as a single directory, so a
move between tiers changes nothing the player can see.

[mergerfs](https://github.com/trapexit/mergerfs) is the usual choice:

```sh
sudo apt install mergerfs        # or your distro's package
sudo mkdir -p /media/library/tv /media/library/movies
```

Add to `/etc/fstab` (one line per library):

```
/srv/fast/tv=RW:/srv/slow/tv  /media/library/tv  fuse.mergerfs  allow_other,use_ino,cache.files=partial,dropcacheonclose=true,category.create=ff,moveonenospc=true,minfreespace=50G,fsname=medialib-tv,nofail  0 0
```

The branch modes matter:

- `=RW` on the **fast** branch — new files (downloads) are created here
- `=NC` on the **slow** branch — readable, and existing files stay writable, but it is
  **never chosen for a new file**, so fresh content cannot land on slow storage

```
/srv/fast/tv=RW:/srv/slow/tv=NC  /media/library/tv  fuse.mergerfs  ...
```

`allow_other` needs `user_allow_other` in `/etc/fuse.conf`. In a container you may also
need `/dev/fuse` passed in.

```sh
echo user_allow_other | sudo tee -a /etc/fuse.conf
sudo mount /media/library/tv
ls /media/library/tv          # should show the union of both branches
```

**Verify the union does its job before going further.** Move a small file from one branch
to the other by hand and confirm the merged path is unchanged:

```sh
ls /media/library/tv > /tmp/before
sudo mkdir -p /srv/slow/tv/.pamts-check /srv/fast/tv/.pamts-check
echo hello | sudo tee /srv/fast/tv/.pamts-check/probe >/dev/null
cat /media/library/tv/.pamts-check/probe          # hello
sudo mv /srv/fast/tv/.pamts-check/probe /srv/slow/tv/.pamts-check/probe
cat /media/library/tv/.pamts-check/probe          # still hello -- the move was invisible
sudo rm -rf /srv/fast/tv/.pamts-check /srv/slow/tv/.pamts-check
```

If that second `cat` works, tiering will be transparent to your player.

## 4. Point the player at the merged path only

In your media player, edit each library so its **only** folder is the merged path
(`/media/library/tv`), removing the per-tier paths.

This makes the player rescan and re-match. It is a migration, so:

- **back up the player's database first**
- expect a scan of the whole library afterwards
- check your watched state afterwards

If your player container mounts paths individually, add a bind for the merged path and
make sure the union is mounted **before** the container starts.

## 5. Write the config

```sh
sudo cp examples/pamts.toml /etc/pamts/pamts.toml
sudo chmod 640 /etc/pamts/pamts.toml
sudoedit /etc/pamts/pamts.toml
```

The minimum you must change:

| setting | what to put |
|---|---|
| `[player] url` | your server, e.g. `http://127.0.0.1:32400` |
| `[player] token_file` | `/etc/pamts/player-token` |
| `[[roots]]` | how the player's path maps to your two tiers |
| `[[jobs]]` | one per library |
| `[tier] budget_gb` | how much of the fast tier PAMTS may fill |
| `[tier] dest_fstypes` | the filesystem type your **slow** tier really is |

`dest_fstypes` is a safety guard, not a formality — read its comment in the example
config. If your slow tier is a local disk, set it to e.g. `["ext4"]` or `["zfs"]`; if it
is NFS, `["nfs", "nfs4"]`.

`[[roots]]` is how PAMTS knows that the file your player calls
`/media/library/tv/Show/S01E01.mkv` is `/srv/fast/tv/Show/S01E01.mkv` locally:

```toml
[[roots]]
player_path = "/media/library/tv"
fast = "/srv/fast/tv"
slow = "/srv/slow/tv"
```

## 6. Validate before it touches anything

Everything below is read-only.

```sh
# Does the config parse, and can PAMTS reach the player?
sudo pamts-tier.py --dry-run --only tier
```

You should see a play-data count, a pin count, and either "under budget" or a list of
what it *would* evict. If you see `no play data from the player`, fix that before going
on — PAMTS refuses to evict without it, by design.

```sh
# What would be pinned to fast storage?
sudo pamts-promote.py --dry-run --next-up

# Exercise promotion against your real library without waiting to press play.
# This replays recently PLAYED items as if they were playing.
sudo pamts-promote.py --dry-run --simulate-recent --simulate-count 5
```

Read that last output carefully: it shows exactly which episodes would be copied and why
it stopped where it did (item cap, byte cap, time budget, or headroom).

**If you configured any `backup` jobs, dry-run them separately and look hard at the
deletion counts:**

```sh
sudo pamts-tier.py --dry-run --only backup
```

A number far larger than you expect almost always means a source is not fully mounted.
That is exactly what `max_delete` is there to catch.

## 7. Turn it on

```sh
sudo install -m 644 systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Nightly: evict -> fetch next-to-watch -> replicate, in one window
sudo systemctl enable --now pamts-nightly.timer

# Every 60s: promote what follows whatever is playing
sudo systemctl enable --now pamts-promote.timer
```

Run the nightly job once by hand the first time, and watch it:

```sh
sudo systemctl start pamts-nightly.service
sudo journalctl -u pamts-nightly.service -f
```

## 8. Check it is working

```sh
systemctl list-timers 'pamts-*'
sudo tail -f /var/log/pamts-promote.log
```

Then start an episode and watch the log. Within a minute you should see the session
detected, the time budget calculated from how long is left, and the following episodes
copied:

```
[plex] playing episode: Some Series S01E03 [22 min left]
  budget for this event: 52.8G, 24 item(s) (52.8G fits in 22 min at 100.0M/s x0.4)
  4 item(s), 9.1G
    promoting S01E04 2.3G  Some Series/Season 1/...
```

`promoted 0B this pass` with nothing playing is correct and expected.

## 9. Optional: the access observer

Skip this unless one of these applies:

- a server that serves your media **cannot give you play history** — a multi-user
  Navidrome being the obvious case
- you want one source of truth covering **every** client, including ones with no adapter
  at all

The [access observer](OBSERVER.md) taps the **file server** rather than the media server,
and works out from the read pattern whether each session was a play, a scan or a copy. It
needs `libbpf` on that host and `CAP_BPF`/`CAP_PERFMON`; a privileged container has them.
No compiler and no `tracefs` mount on the server, and no restart.

Build the collector on any machine with `clang` and `bpftool`, using the **target's** BTF:

```sh
ssh fileserver cat /sys/kernel/btf/vmlinux > vmlinux.btf
make -C observer vmlinux.h BTF=$PWD/vmlinux.btf
make -C observer
```

Copy `pamts_observer.py`, `observer/pamts_bpf.py`, `observer/pamts-observerd.py` and
`observer/pamts_nfsd.bpf.o` to the file server, then:

```sh
sudo install -m 644 systemd/pamts-observer.service /etc/systemd/system/
sudo systemctl edit --full pamts-observer.service    # set --root to your export root
sudo systemctl enable --now pamts-observer.service
sudo journalctl -u pamts-observer.service -f
```

**Run it in logging mode for a day or two before wiring anything to it.** Nothing consumes
its output until you add the `[[players]]` stanza, so it costs nothing to watch first — and
you want to see how your own traffic classifies, because the thresholds separating "paced"
from "flat out" depend on your bandwidth and clients. You should see lines like:

```
PLAY   10.0.0.20   407.1MB cov=?      145s 2.8MB/s   reqs=3257  /srv/media/tv/...S02E04.mkv
PLAY   10.0.0.20   136.1MB cov=?       47s 2.9MB/s   reqs=1091  /srv/media/tv/...S02E04.mkv  (refresh)
PROBE  10.0.0.31     0.4MB cov=0.01     0s 1.7MB/s   reqs=5     /srv/media/music/...flac
```

Two lines for one viewing is correct: the first is the mid-flight checkpoint, reported so
promotion does not have to wait for a 45-minute episode to finish, and the second refreshes
it without counting the play twice. `PROBE` is a scan and must never reach history — if
your scans are being labelled `PLAY`, tune before connecting it, not after.

Check what it has concluded:

```sh
curl -s localhost:8621/stats | python3 -m json.tool
curl -s localhost:8621/sessions | python3 -m json.tool     # what is playing now
curl -s 'localhost:8621/history?since=0' | python3 -m json.tool
```

When you are satisfied, add the `observer` stanza from `examples/pamts.toml`.

---

## Tuning from here

Start with the defaults for a week, then read
[CONFIGURATION.md](CONFIGURATION.md). The settings most people want to change first:

- `[tier] budget_gb` — the single most important number
- `[tier] next_up_max_gb` — how much of the budget pinning may use
- `[promote] max_bytes_gb` — the ceiling on one promotion event
- `depth` per job — `2` for `Show/Season` layouts, `1` where each item is a directory
- `grace` per job — `false` for series (pinning covers them), `true` for films

## If something looks wrong

| symptom | look at |
|---|---|
| `no play data from the player` | token file, `[player] url`, player reachable |
| `not under a configured root` | your `[[roots]]` do not match what the player reports |
| `refusing to evict ... not one of [...]` | `[tier] dest_fstypes` vs your slow tier |
| nothing is ever evicted | you may be under `budget_gb`; check the reported footprint |
| everything is pinned | `next_up_max_*` too high relative to `budget_gb` |
| player shows missing media after a move | the union is not set up, or the player still points at a per-tier path (step 3/4) |
| observer logs `<unresolved dev=... ino=...>` | that read was on a dataset outside `--root`; add it or ignore it |
| observer labels your scans `PLAY` | raise `--checkpoint-after`, or see the thresholds in [OBSERVER.md](OBSERVER.md) |
| observer will not load (`bpf_object__load failed`) | missing `CAP_BPF`/`CAP_PERFMON`, an unprivileged container, or a `vmlinux.h` from the wrong kernel |

Every run logs why it kept each item. When in doubt, `--dry-run` and read it.
