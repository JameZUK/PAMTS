# Configuration

PAMTS reads one TOML file, `/etc/pamts/pamts.toml` by default. Override with `--config`
or the `PAMTS_CONFIG` environment variable. A fully commented starting point is in
[`examples/pamts.toml`](../examples/pamts.toml).

Sizes are binary GB (1 GB = 1024³). Times are seconds or days as named.

The loader validates strictly and refuses to start on anything ambiguous — a tiering
system that has misunderstood its own configuration moves data to the wrong place.

---

## `[[players]]`

The only section that knows about a specific media server. Repeatable — and for music you
usually want more than one, because several things commonly serve the same files. History
is **merged** across them, taking the most recent play, so an album played in one app is
not treated as untouched by the others.

A singular `[player]` is accepted as shorthand for one entry. Using both is an error.

| key | applies to | notes |
|---|---|---|
| `kind` | all | `plex`, `lms`, `navidrome` |
| `name` | all | optional label; defaults to `kind`. **Required** if you configure two of the same kind |
| `url` | all | *required*, e.g. `http://127.0.0.1:32400` |
| `token_file` | plex, navidrome | file containing the token/password, mode `600` |
| `username` | navidrome, lms | Navidrome: required. LMS: only if HTTP auth is on |
| `password` | lms | only if HTTP auth is on |
| `music_folder` | navidrome | **required** — Subsonic reports paths relative to it |
| `history_db` | lms, navidrome | *optional* read-only path to the server's database — see below |

Secrets go in their **own file**, not in this config, and not in version control.

### Per-adapter caveats

**LMS exposes no play history.** Play counts live in a private database and its API
reports none of them. It drives promotion fine; ranking comes from observed plays (see
`[history]`), which start empty and fill in. An LMS-only setup therefore has no ranking
data on day one, and PAMTS refuses to evict rather than guessing.

**Navidrome annotations are per-user.** A fresh service account reports no play history
however much the library has been listened to. Without `history_db`, point it at the
account that actually listens. PAMTS warns if it sees items but no plays.

**The observer is not a media server.** `kind = "observer"` reads the access observer
daemon (see [OBSERVER.md](OBSERVER.md)), which watches what the file server actually
serves. It reports demand for every client with no credentials, which is the only way to
cover a server whose history is unreachable — but it gives *file-level* demand and never
per-user history, and it only knows files that have been read, so its library view is
partial by nature. Options: `url` (default `http://127.0.0.1:8621`), `timeout`,
`demand_labels` (default `["PLAY", "FETCH"]`), `history_since`.

### `history_db`

Neither music server's API can answer "what has *anyone* played" — LMS reports no play
data at all, and Subsonic annotations are per-user. Setting `history_db` to the server's
own database closes that gap:

| server | database | gives you |
|---|---|---|
| LMS | `persist.db` | play history at all — its API has none |
| Navidrome | `navidrome.db` | **every user's** history, not just the caller's |

Read-only, opened `immutable`, so a live server is never disturbed and no lock is taken.
It needs filesystem access to the database and depends on a schema the upstream project
may change; if that happens, history is skipped with a clear error rather than being
silently wrong. Navidrome also needs `music_folder` set, since the database stores paths
relative to it.

`history_db` does two jobs, not one:

1. **Ranking history** — including history that predates PAMTS, and (for Navidrome) every
   user's rather than just the calling account's.
2. **A promotion trigger** — PAMTS also asks the database "what was played since I last
   looked", which covers every user with no privileged account, and catches plays that
   started and finished between polls. A per-player watermark is kept in the state file;
   on first sight of a player it starts from "now", so the first run cannot mistake your
   whole listening history for a burst of activity.

For a multi-user music library this is the whole answer: point `history_db` at the
server's database and every user is covered automatically, for both ranking and
promotion, without giving PAMTS an elevated account.

## `[history]`

PAMTS records what it observes playing, building its own play history, and merges it with
whatever the players report.

| key | default | notes |
|---|---|---|
| `observe` | `true` | record observed plays |
| `max_entries` | `200000` | cap; oldest records dropped first |
| `max_age_days` | `1825` | ~5 years; older records pruned |

This is what makes a history-less adapter usable for ranking. It is scan-immune by
construction — a library scan is never a playing session — so it cannot be polluted the
way `atime` can. Harmless to leave on when every player reports history; it simply agrees
with them.

Caveats: a 60-second poll can miss a very short item, and "seen playing" is not quite
"played to completion". Both are acceptable for tiering, where the question is whether a
human was there.

## `[paths]`

| key | default |
|---|---|
| `lock_file` | `/run/pamts.lock` |
| `state_file` | `/var/lib/pamts/state.json` |
| `tier_log` | `/var/log/pamts-tier.log` |
| `promote_log` | `/var/log/pamts-promote.log` |

`lock_file` is shared by tiering and promotion so they can never run at once. Do not give
them separate locks.

`state_file` holds promotion protection records and the measured throughput estimate.
Losing it is harmless — protections lapse and throughput is re-measured.

## `[[roots]]` — how player paths map to your tiers

**Required, and the thing most likely to be wrong.** This is how PAMTS knows that the
file the player calls `/media/library/tv/Show/S01E01.mkv` lives at
`/srv/fast/tv/Show/S01E01.mkv` locally.

```toml
[[roots]]
player_path = "/media/library/tv"    # exactly what the player reports
fast = "/srv/fast/tv"
slow = "/srv/slow/tv"
```

All three must be absolute, and `fast` must differ from `slow`.

The relative path below the root must be **identical** on both tiers. PAMTS never
renames anything, so this holds naturally if you let it do the moving.

**Longest prefix wins**, so overlapping roots may coexist. That is genuinely useful when
migrating: if your player used to point at the two tiers separately and now points at a
merged path, list all three and PAMTS keeps working during and after the change.

```toml
[[roots]]                            # the merged path (what the player uses now)
player_path = "/media/library/tv"
fast = "/srv/fast/tv"
slow = "/srv/slow/tv"

[[roots]]                            # a legacy path, still reported for some items
player_path = "/mnt/tv-fast"
fast = "/srv/fast/tv"
slow = "/srv/slow/tv"
```

If you see `not under a configured root` in the logs, a `[[roots]]` entry does not match
what the player actually reports. Check the exact string with
`pamts-promote.py --dry-run --simulate-recent`.

## `[[jobs]]` — what to manage

| key | modes | notes |
|---|---|---|
| `name` | both | unique; selectable with `--job` |
| `mode` | both | `"tier"` or `"backup"` |
| `source` | both | the fast-tier path |
| `dest` | both | the slow-tier / replica path |
| `depth` | tier | eviction granularity, default `1` |
| `grace` | tier | honour the grace window, default `true` |
| `max_delete` | backup | **required**; refused on tier jobs |
| `exclude` | backup | list of rsync patterns |
| `exclude_from` | backup | file of rsync patterns |

### `mode`

`tier` moves content to `dest` as it ages, and **never** uses `--delete`. `backup`
mirrors and propagates deletions. See [DESIGN.md](DESIGN.md#two-job-modes-and-why-mixing-them-up-is-impossible) —
this distinction is the most safety-critical thing in the tool, and the loader refuses to
let the two be confused.

### `depth`

`1` where each item is its own directory (films). `2` for `Show/Season` layouts, making a
**season** the unit of eviction so that working through a series lets earlier seasons age
off while the current one stays. A directory shallower than `depth` with no
subdirectories becomes its own candidate, so a series without season folders still works.

### `grace`

`true` — never-played content is not evicted for `new_grace_days`. Right for films.

`false` — right for series, because next-to-watch pinning already guarantees the episode
you are about to watch. With `grace = true` on series, a whole-season download is held on
fast storage *in its entirety* for a fortnight, which wastes the budget on twenty-three
episodes you will not watch this week.

### `max_delete`

A **circuit breaker**. PAMTS counts what `--delete` would remove before deleting
anything, and refuses the whole job if the count is higher, having deleted nothing.

Set it a little above the largest number of deletions you would consider normal for that
pair. If a run reports a number far larger than you expect, that almost always means the
source is not fully mounted — which is precisely what this catches.

### Excludes

Patterns are passed to rsync. Excluded paths are **never deleted** on the destination
(PAMTS does not use `--delete-excluded`), so content that exists only on the replica
behind an exclude is protected. A configured `exclude_from` file that has gone missing
aborts the job, because running without it would delete what the excludes protect.

### `budget_gb` (tier jobs only)

A tier job may carve out its own allowance instead of sharing `[tier] budget_gb`:

```toml
[[jobs]]
name = "music"
mode = "tier"
source = "/media/media-cache/music/Organised"
dest   = "/media/media-slow/Music/Organised"
depth  = 2                  # Artist/Album -- an album is the unit
budget_gb = 800
```

Jobs that **omit** it share the global number, and that sharing is deliberate: a quiet
month of films lends its space to a heavy month of television, because both are watched
from the same sofa in the same evenings.

Media of very different sizes must **not** share. Four terabytes of albums in the same
pool as a 400 GB video allowance means one combined footprint measured against one
ceiling — so whichever medium happens to be larger evicts the other's entire working
set, every night. Give any such medium its own `budget_gb`.

Each pool is evicted independently, but play history is gathered **once** per run and
shared, so adding a pool does not multiply the cost of sweeping your players.

Setting it on a `backup` job is refused: a backup has no budget, it mirrors.

## `[tier]`

| key | default | notes |
|---|---|---|
| `budget_gb` | `400` | how much fast storage PAMTS may fill with tiered content |
| `settle_seconds` | `3600` | never evict something written this recently |
| `new_grace_days` | `14` | never-played content is kept this long (where `grace = true`) |
| `promote_protect_days` | `7` | promoted content is not evicted for this long |
| `next_up_max_items` | `40` | ceiling on pinned items |
| `next_up_max_gb` | `80` | ceiling on pinned bytes |
| `pin_depth_max` | `4` | items ahead to pin when space is plentiful |
| `dest_fstypes` | `["nfs","nfs4"]` | **safety guard — read below** |
| `inprogress_suffixes` | `.part .tmp .!qB .partial .crdownload` | a directory containing one is never evicted |

### `budget_gb`

The single most important number. Content above it is evicted, least-recently-played
first. Leave room for pinning (`next_up_max_gb`) and promotion headroom
(`[promote] headroom_gb`) inside it — if those two add up to most of the budget there is
nothing left for ordinary tiered content.

### `dest_fstypes`

The filesystem types the **slow** destination may be on. This is a real safety guard.

An unmounted mountpoint is just an empty directory. A job writing there fills your root
filesystem, and because tiering uses `--remove-source-files` it then deletes the only
other copy. Checking "is it a mountpoint" does not help — `/` is always one — so PAMTS
checks the filesystem *type*.

Set it to what your slow tier actually is: `["nfs","nfs4"]`, `["cifs"]`, `["zfs"]`,
`["ext4"]`, `["btrfs"]`… Find it with:

```sh
findmnt -no FSTYPE --target /srv/slow/tv
```

`["*"]` disables the check. Do not, unless you have a specific reason and accept the
consequence above.

### `promote_protect_days`

Days, not hours. If you watch one episode every two or three days, a window of hours
means the promoted episode is evicted before you return to it and the work repeats
nightly. Match it to how you actually watch.

### Pinning caps

`next_up_max_items` / `next_up_max_gb` cap how much of the budget pinning may consume.
A large library has hundreds of series and pinning one item of each could run to
terabytes. Series are prioritised by recent activity, and allocation is round-robin so
every active series gets its first unwatched item before any gets a second.

`pin_depth_max` is how many items ahead to pin when the tier is nearly empty; it scales
down to 1 as the tier fills. Raise it if your fast tier is comfortably large.

## `[promote]`

| key | default | notes |
|---|---|---|
| `headroom_gb` | `60` | stop promoting this far below budget (anti-thrash) |
| `min_free_gb` | `100` | refuse to promote below this much **real** free space |
| `headroom_fraction` | `0.30` | share of remaining headroom one event may use |
| `min_event_bytes_gb` | `6` | floor, so the next item is always attempted |
| `min_event_items` | `2` | item floor |
| `max_items` | `24` | ceiling per event |
| `max_bytes_gb` | `60` | byte ceiling per event |
| `time_safety` | `0.40` | assumed share of throughput while a stream is being served |
| `default_rate_mbps` | `80` | starting throughput guess, MB/s |
| `rate_alpha` | `0.3` | EWMA weight for measured throughput |

### `headroom_gb`

Reserved so a promotion cannot push the tier over budget and cause its own eviction on
the next run. Roughly one promotion event's worth is a sensible floor.

### `min_free_gb`

`budget_gb` is a policy number; it does not know what *else* shares that filesystem. This
is a hard stop on real free space. Set it above whatever your other workloads might need.

### `max_items` / `max_bytes_gb`

**Ceilings, not targets.** PAMTS scales down from them by space and time. Both exist
because either alone is meaningless: six episodes is 8 GB of 1080p television and 40 GB of
4K, so an item count alone tells you nothing about cost, and a byte cap alone would
promote an absurd number of small items.

### `time_safety`

While PAMTS promotes, slow storage is concurrently serving the stream you are watching.
This is the share of measured throughput PAMTS assumes it can use. Lower it if promotion
ever disturbs playback; raise it if your slow tier has headroom to spare.

### `default_rate_mbps` / `rate_alpha`

The starting guess, used only until real transfers have been measured. PAMTS then smooths
measurements into a running estimate. Leave `rate_alpha` alone unless your throughput is
unusually variable.

## `[[promote.rules]]` — what to promote, per medium

Without any rules, promotion fetches only the items that **follow** what is playing,
bounded by the adapter's own ceilings. That suits an episode and does nothing at all for
a film, which has nothing after it — the log says `no locality group for this item`.

A rule is matched on the kind of thing playing: `movie`, `episode`, `track`, or `*` for
anything. An exact match always wins over `*`, whatever order they appear in. A kind with
no matching rule keeps the old lookahead-only behaviour exactly.

```toml
# A film: nothing follows it, so the only thing worth promoting is the film itself.
[[promote.rules]]
match = "movie"
current  = { after_seconds = 120 }
lookahead = { items = 0 }
max_bytes_gb = 40

# An episode: pull the run behind it, and the episode itself once it is clearly being
# watched rather than sampled.
[[promote.rules]]
match = "episode"
current  = { after_seconds = 300 }
lookahead = { items = 3 }
max_bytes_gb = 24

# A track: the rest of the album. Many small files rather than a few large ones.
[[promote.rules]]
match = "track"
current  = { after_seconds = 30 }
lookahead = { items = 20 }
max_bytes_gb = 2
```

| key | meaning |
|---|---|
| `match` | the kind playing: `movie`, `episode`, `track`, or `*` |
| `current.after_seconds` | promote the item **being played** once it has been playing this long. Omit `current` entirely to leave it alone. |
| `lookahead.items` | how many **following** items to fetch. `0` disables lookahead. |
| `max_bytes_gb` | byte ceiling for the whole event, current item included. Omit to keep the adapter's. |

`lookahead.items` counts followers only; the current item is allowed on top, so
`items = 0` still promotes the film itself rather than nothing.

### What promoting the current item does and does not buy you

It does **not** speed up the stream in flight. This was tested rather than assumed: the
NFSv4 client holds the file open for the whole duration, so nfsd never ages out its
handle and mergerfs never re-resolves which branch to read from. The playback already
running finishes from slow storage whichever copy appears underneath it. Bursty readers
with long idle gaps behave no differently — the open state, not the read pattern, is what
pins the branch.

What it buys is every **fresh open** of that file: a seek, a resume the next evening, a
re-watch, a second viewer. Those are common enough for a film to make it worth the space.

`current.after_seconds` is what stops a browse from being expensive. The clock starts
when PAMTS first *sees* the item playing — no adapter reports a reliable elapsed time —
so with a 60-second poll a 120-second gate is satisfied on the third pass. If playback
stops, the clock is forgotten, so resuming the same file a week later starts it again
rather than promoting instantly.

---

## Command-line reference

```
pamts-tier.py [--config F] [--dry-run] [--only tier|backup] [--job NAME]... [--budget-gb N]
pamts-promote.py [--config F] [--dry-run] [--next-up]
                 [--simulate-recent [--simulate-count N]] [--budget-gb N]
```

| flag | notes |
|---|---|
| `--dry-run` | change nothing; honoured all the way down to rsync |
| `--only` | restrict to one mode |
| `--job` | run only named jobs; repeatable |
| `--next-up` | fetch pinned items that are not on fast storage (nightly) |
| `--simulate-recent` | replay recently *played* items as if playing; requires `--dry-run` |
| `--budget-gb` | override the **shared** `[tier] budget_gb` for one run; jobs with their own `budget_gb` keep it |

`--simulate-recent` is the most useful validation tool here: it exercises the real
selection logic against your real library without waiting for someone to press play, and
the log shows exactly why selection stopped where it did.
