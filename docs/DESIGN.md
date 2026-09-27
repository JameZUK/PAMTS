# Design

Why PAMTS works the way it does. Most of these decisions exist because the obvious
alternative was tried and was wrong.

## Playback is the only trigger

The obvious way to decide what belongs on fast storage is "what was read recently".
Every mechanism for that is unusable:

- **`atime`** — with `relatime` (the default nearly everywhere) a read updates `atime`
  whenever the stored value is more than 24 hours stale, which is true of essentially
  every file in a tiering candidate set.
- **`inotify`** — reports opens and reads with no idea who is doing it.
- **A FUSE layer counting reads** — same problem, one level down.

The problem is not precision, it is **semantics**. A media server scans its library —
often several times a day, and Plex's default includes a periodic deep scan that reads
whole files to generate preview thumbnails. At the filesystem level, a scanner reading
every file in your library is *identical* to you watching something. So:

- an atime-based ranking is reset to "everything was just accessed" by every scan, and
  eviction then picks close to arbitrarily
- a read-based promotion trigger fires for every file in the library during a scan, and
  promotes the lot

Both failure modes are silent. Nothing errors; the system simply stops doing anything
useful, and you find out months later when your fast tier is full of things you have
never watched.

PAMTS therefore asks the **media player** what has been *played*. A view record is
written when a human watches something and is untouched by scans. `atime` appears
nowhere in this codebase, and `scan_dir()` deliberately does not even collect it.

### The corollary: refuse rather than guess

If the player cannot be reached, eviction **refuses to run** and says so loudly. It does
not fall back to `atime` or to "oldest file".

That is the right trade because the failure is asymmetric. Being over budget for another
day costs nothing — nothing is lost, nothing breaks, content simply stays on fast
storage. Evicting on a corrupted ranking moves the wrong things, which costs you spin-ups
later and has to be undone. When the signal is untrustworthy, doing nothing is strictly
better.

Never-played content has no view record, so it falls back to **mtime** — "when did this
arrive". That is also scan-immune, because a scan reads and does not write, and for
something never watched "how long has it occupied fast storage for nothing" is exactly
the right question.

## Promotion copies; it never moves

Slow storage is the permanent home. Promotion leaves it intact and puts a second copy on
fast storage.

This costs some slow-tier space that is, strictly, redundant. It buys:

- **a cheap reversal** — evicting promoted content later finds the destination already
  identical, so rsync transfers nothing and only removes the source
- **no window of risk** — at no point does exactly one copy exist mid-transfer
- **simplicity** — the slow tier is always complete, so "is this safe to drop from fast
  storage" never needs a second thought

## Next-to-watch pinning

A whole-season download lands on fast storage all at once. Two naive policies both fail:

- *keep it all* (a grace window over the whole season) — twenty-four episodes occupy the
  budget when only one is going to be watched soon
- *evict by age* — the season is swept to slow storage and the episode you press play on
  tomorrow is not local

So PAMTS pins the **next unwatched item of each series**: never evicted, and fetched back
if missing. The rest of the season is free to age off. You keep one episode local instead
of twenty-four, and the guarantee you actually care about still holds.

"Next" is the lowest `(season, episode)` with no view record, counting only items the
player has a file for — so gaps in a season do not stall the pin.

Pinning has two halves that **must** agree: tiering must not evict a pin, and promotion
must fetch a pin that is absent. The caps and the depth rule therefore live in one place
(`pamts.py`), not in each script. Two copies of those numbers would drift, and the
guarantee would break silently.

It is capped. A large library has hundreds of series, and pinning one item of each could
run to terabytes. Series are prioritised by recent activity — most recently played, or
for one never started, most recently added — and allocation is **round-robin**, so every
active series gets its *first* unwatched item before any series gets a second.

## Season granularity

Eviction acts on directories at a configurable `depth`:

- `depth = 1` — each item is its own directory (films)
- `depth = 2` — `Show/Season`, so a **season** is the unit

Depth 2 matters more than it sounds. At depth 1 a series is one all-or-nothing blob:
watching season 4 keeps seasons 1–3 on fast storage too. At depth 2 you work through a
series and the seasons behind you age off naturally.

A directory shallower than `depth` with no subdirectories is treated as its own
candidate, so a series stored without season folders still works.

## Sizing by space and time

How much to promote is not a constant, because a fixed number is wrong at both ends. Six
episodes is 8 GB of 1080p television and 40 GB of 4K. Two limits apply and the smaller
wins:

**Space.** A fraction of remaining headroom. A nearly-empty tier can take a whole season;
a nearly-full one takes a couple of episodes. Pin depth scales the same way.

**Time.** What can realistically be copied **before the episode being watched ends**,
from the player's reported duration and position. Copying beyond that point does not help
the viewer, and it competes with the stream slow storage is already serving — hence a
safety factor assuming PAMTS gets only part of the available throughput.

Throughput is **measured** from real transfers and smoothed (EWMA), starting from a
conservative default until there is data. A guessed constant would be wrong on every
system.

Floors survive both limits, so the immediately-next item is always attempted. Being
slightly late with it still beats not fetching it at all.

## Anti-thrash

Promotion and eviction pull in opposite directions, so unchecked they oscillate.

- Promotion keeps a **headroom** reserve below the budget, so a promotion cannot push the
  tier over budget and trigger its own eviction on the next run.
- Promotion **records** what it promoted; eviction refuses to evict anything promoted
  within `promote_protect_days`.

That window is measured in **days**, not hours, and this is worth stressing. If someone
watches one episode every two or three days, a protection window of hours means the
promoted episode is evicted long before they come back to it, and the work is repeated
every night. Match the window to how people actually watch.

Records are keyed by full path and matched prefix-aware in both directions, so a
granularity mismatch between the two halves **over**-protects. Under-protecting would let
a just-promoted item be evicted immediately, which is the exact oscillation this is
preventing.

## Two job modes, and why mixing them up is impossible

This is the most dangerous part of the tool, so the design makes the mistake
unrepresentable rather than merely documented.

|  | `tier` | `backup` |
|---|---|---|
| fast copy is | a cache | authoritative |
| slow copy is | the permanent home | a replica |
| source emptying is | **normal and expected** | a red flag |
| rsync uses | `--remove-source-files` | `--delete` |
| `max_delete` | **refused** | **required** |

The asymmetry is the point. For a `tier` job, the fast copy emptying out is the system
working correctly — so `--delete` would look at an empty cache, conclude everything was
deleted, and erase the permanent library. For a `backup` job, deletions genuinely must
propagate, or the replica grows forever.

The config loader enforces both directions: a tier job that sets `max_delete` is
rejected, and a backup job that omits it is rejected.

### `max_delete` is a circuit breaker, not a limiter

rsync's own `--max-delete=N` stops *after* deleting N files. As a safety mechanism that
is nearly useless: it deletes N things and then complains.

PAMTS runs a pre-flight pass that **counts** what `--delete` would remove, and refuses
the entire job if the count exceeds the limit — having deleted nothing. The probe must
not inherit `--max-delete`, or it would abort at the limit and never report the true
figure. There is a test for exactly that.

### The destination filesystem check

An unmounted mountpoint is an ordinary empty directory. A job that writes there fills the
root filesystem, and because tiering uses `--remove-source-files` it then deletes the
only other copy.

Checking "is it a mountpoint" does **not** work: `/` is always a mountpoint, so that test
returns true for everything. PAMTS checks the filesystem **type** matches what you
configured, which is what actually proves the storage is attached.

## Films are not promoted

A film is watched once. By the time a session is visible it is already streaming from
slow storage, and there is no "next film" to prefetch. Reactive promotion for films is
pure overhead.

Only *predictive* promotion — a watchlist, "continue watching", recently-added — could
help, and that spends fast storage on content that may never be played. PAMTS declines
rather than pretending, and films still get tiered and still get the grace window.

This is also why the locality rule belongs in the **player adapter** and not the engine:
"what is likely to be played next" is a different question for an episode, a track and a
film, and only the adapter knows which it is holding.

## What PAMTS deliberately does not do

- **It does not present the unified path.** That is a union filesystem's job, and
  mergerfs already does it well. Reimplementing it would mean sitting in the read path
  for all your media.
- **It does not rename or reorganise anything.** Relative paths are identical on both
  tiers. If you stop using PAMTS, what is left is two ordinary directory trees.
- **It does not manage your library.** It never writes to the media player.
- **It does not transcode, verify or checksum.** rsync does the transfer; that is enough.
