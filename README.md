# PAMTS — Pluggable Automatic Media Tiering System

Keep the media you are about to watch on fast storage, and everything else on slow
storage — automatically, driven by what you actually play.

If you have a small, fast pool (SSD/NVMe, or just a quieter disk) and a large, slow one
(a spinning array that idles down), PAMTS moves content between them so that:

- what you are about to watch is already on fast storage
- the slow array stays asleep during a binge instead of waking for every episode
- your fast pool holds the *next* episode, not all twenty-four of them

**[→ Quick Start](docs/QUICKSTART.md)** — working setup in about ten minutes.

---

## The idea in one picture

```
                      your media player sees ONE path
                    /media/library/tv/Show/S01E02.mkv
                                   |
                        [ union filesystem ]
                          /              \
                /srv/fast/tv            /srv/slow/tv
                (small, fast)           (large, slow, spins down)
                     ^                        |
                     |    PAMTS promotion     |   PAMTS tiering
                     +------ on playback -----+   as content ages
```

PAMTS does not touch your library layout, and it never renames anything. It moves files
between two directory trees and lets a union filesystem present them as one.

## Two things it is important to understand

**1. You need a union filesystem (or equivalent) in front of the two tiers.**

PAMTS moves files between tiers. If your media player points at both tiers directly, it
will see files appear and disappear on every run and churn its library. Put something
like [mergerfs](https://github.com/trapexit/mergerfs) in front, point the player at the
merged path only, and moves become invisible. The Quick Start covers this.

**2. Playback is the only trigger. Deliberately.**

It is tempting to promote content when a file is *read* — via `atime`, `inotify`, or a
FUSE layer. Do not. A library scan reads every file in your library, and at that level a
scanner's read is indistinguishable from you watching something. The result is that one
nightly scan promotes your entire library and flattens every ranking.

PAMTS therefore asks the media player what has been *played*. Scans do not write view
records, so they are inert. This is why `atime` appears nowhere in this codebase, and
why eviction **refuses to run** if it cannot get play data rather than falling back to
it. See [DESIGN.md](docs/DESIGN.md).

## What it does

**Tiering (fast → slow).** Content over your budget is moved to slow storage,
least-recently-*played* first. Never-played content falls back to when it was added.
Configurable granularity: a *season* rather than a whole series, so working through a
show lets earlier seasons age off while the current one stays.

**Next-to-watch pinning.** The next unwatched item of each series is pinned to fast
storage: never evicted, and fetched back if missing. This is what lets a whole-season
download age onto slow storage while still guaranteeing the episode you are about to
watch is local. Pin one episode instead of keeping twenty-four.

**Promotion (slow → fast), on playback.** When you start an episode, PAMTS copies the
ones that follow — crossing into the next season when the current one runs out. It does
nothing for the episode you are watching (already streaming) and everything for the next
one, which is the point: the slow array gets to go back to sleep.

**Sized by space *and* time.** How much to promote is not a fixed number:

- *space* — a fraction of remaining headroom, so a nearly-empty tier takes a whole
  season while a nearly-full one takes a couple of episodes
- *time* — what can realistically be copied **before the episode you are watching
  ends**, from the player's reported duration and position. Throughput is measured from
  real transfers and smoothed, not guessed.

**Anti-thrash, enforced on both sides.** Promotion keeps headroom below the budget so it
cannot trigger its own eviction, and records what it promoted; tiering refuses to evict
anything promoted within a configurable window (default 7 days — if you watch one
episode every few days, hours of protection achieves nothing).

**Backup mode, kept strictly separate.** The same tool mirrors content that lives
permanently on fast storage, with `rsync --delete` so deletions propagate. That is a
genuinely dangerous mode, so it is declared per job and the config loader refuses to let
the two be confused — see below.

## Films are not promoted, on purpose

A film is watched once, and by the time a session is visible it is already streaming
from slow storage. There is nothing to prefetch. Only *predictive* promotion (a
watchlist, "continue watching") could help, and that spends your fast pool on content
that may never be played. PAMTS leaves it alone rather than pretending.

Films still get tiered, and still get the grace window for new unwatched arrivals.

## Safety

This tool deletes and moves files, so the guards are the most important part of it.

- **Two job modes, and mixing them up is unrepresentable.** A `tier` job *moves* content
  and never uses `--delete`, because the fast copy emptying is normal and expected. A
  `backup` job propagates deletions and *requires* a `max_delete` circuit breaker. The
  config loader refuses a tier job that sets `max_delete`, and refuses a backup job that
  does not.
- **`max_delete` is a circuit breaker, not a limiter.** rsync's own `--max-delete` stops
  *after* N deletions — so on its own it deletes N things and then complains. PAMTS runs
  a pre-flight pass that **counts** the deletions first and refuses the whole job if
  there are more than expected, having deleted nothing.
- **The destination filesystem type is checked.** An unmounted mountpoint is just an
  empty directory: a job writing there fills your root filesystem, and because tiering
  uses `--remove-source-files` it then deletes the only other copy. Checking "is it a
  mountpoint" is not enough — `/` is always a mountpoint — so PAMTS checks the
  filesystem *type* matches what you configured.
- **An empty or missing source aborts a backup job**, because with `--delete` that would
  erase the replica.
- **Excluded paths are never deleted** on the destination, so content that exists only
  on the replica behind an exclude is safe.
- **Promotion copies, never moves.** Slow storage stays the permanent home. A pleasant
  side effect: evicting promoted content later is nearly free, because rsync finds the
  destination already identical and only removes the source.
- **One lock.** Tiering and promotion share it, so they can never run at once and cannot
  race on the budget.
- **`--dry-run` everywhere**, and it is honoured all the way down to rsync.

## Pluggable players

Only one component knows about a specific media server. A player adapter answers:

| question | used by |
|---|---|
| what has been played, across the library | tiering (ranking, pinning) |
| what is playing right now | promotion (the trigger) |
| what is likely to be played **next** | promotion (what to copy) |

That third question is why adapters exist rather than a hard-coded API client: the
locality rule differs per medium. An episode implies the rest of the season; a track
implies the rest of the album; a film implies nothing.

| adapter | play history | now playing | locality |
|---|---|---|---|
| **Plex** | ✅ | ✅ | season, crossing into the next |
| **LMS** / Lyrion | ✅ via the bundled plugin | ✅ | album |
| **Navidrome** | ✅ but **per-user** | ✅ | album |
| **Observer** | ✅ every client, no credentials | ✅ | directory, across both tiers |

**Several players at once.** `[[players]]` takes any number, and history is merged across
them taking the most recent play. This is the normal case for music, where two or three
things serve the same files: ranking on a single app's view would evict an album that was
played in one of the others and simply not seen.

**PAMTS also records what it sees playing**, building its own history. That is what makes
an adapter with no history API — LMS — usable for ranking, and it is scan-immune by
construction, because a scan is never a playing session.

For music, neither server's API answers *"what has anyone played"* out of the box — LMS
exposes no play data at all, and Subsonic annotations are per-user. The right fix is a
plugin that runs inside the server and publishes the data through its own API, and
**PAMTS ships one for LMS** ([`plugins/lms/`](plugins/lms/)): a small Perl plugin adding a
CLI query, so history arrives over the endpoint the adapter already uses — 35,034 records
in under a second, with no filesystem access or database coupling. Install it and the
adapter finds it automatically.

Where no plugin exists, an optional read-only `history_db` covers the same ground, and for
a multi-user Navidrome it also supplies the play *trigger*. Details and trade-offs in
[PLAYERS.md](docs/PLAYERS.md).

### Or stop asking the servers entirely

A player API tells you what *one player* knows. The filesystem sees every consumer
equally. Where your media is served over NFS from one host, the optional
**[access observer](docs/OBSERVER.md)** taps the file server itself and works out from the
read pattern alone whether something was **played**, **scanned**, or **copied** — no
credentials, no per-user enumeration, and no adapter per service. It is the only thing
here that covers a server whose history is unreachable, such as a multi-user Navidrome.

It classifies by how a file is read: a paced, progressive read is playback; a sip of the
header and the tail is a scan; the whole file at wire speed is a copy. Calibrated on real
traffic, a 26 Mbps stream and 21 scans separated cleanly, the scans sitting at 1–2%
coverage across exactly two regions.

What it cannot do is per-user history — `all_squash` erases user identity at the server.
It gives *file-level demand*, which is the right signal for tiering and no substitute for
scrobbles.

Needs `libbpf` on the file server and a privileged container if containerised; the
collector is built elsewhere, so no compiler is required on the server itself.

Adding a player is one class and one registry line — see [PLAYERS.md](docs/PLAYERS.md),
which also covers two gotchas worth reading before you configure music: LMS exposes no
play history at all, and Navidrome's annotations are per-user so a fresh service account
reports nothing.

## Dashboard

```sh
pamts-web.py --config /etc/pamts/pamts.toml --listen 127.0.0.1:8622
```

Current state and history, in one page, standard library only and no build step.
Shows the fast tier against its budget, what is being read right now with each
verdict, recent promotions, and the classified session log — which is the useful
part, because it shows what was *rejected* as well as what counted.

Adding a data source is one class and one registry line, and **the page renders it
without being changed**: an unrecognised source is shown as raw JSON rather than
hidden. See [DASHBOARD.md](docs/DASHBOARD.md).

No authentication; it binds localhost by default. See the doc before exposing it.

## Requirements

- Python **3.11+** (for `tomllib`)
- `rsync`
- A media player with an API that reports play history and current sessions
- A union filesystem in front of your tiers (mergerfs or similar) — see above
- Linux (uses `flock`, `/proc/self/mountinfo`, `ionice`)

No third-party Python packages.

For the optional [access observer](docs/OBSERVER.md), on the **file server** only:

- `libbpf.so.1` (usually already present; the loader is pure ctypes)
- `CAP_BPF` + `CAP_PERFMON`, or `CAP_SYS_ADMIN` — a privileged container has these
- an NFS server using the kernel's `nfsd`

and on whichever machine builds the collector object, `clang` and `bpftool`. Neither is
needed on the file server, and no `tracefs` mount or restart is required.

## Documentation

| | |
|---|---|
| [Quick Start](docs/QUICKSTART.md) | get it running, in order |
| [Configuration](docs/CONFIGURATION.md) | every option, and how to tune it |
| [Design](docs/DESIGN.md) | why playback-only, why copy-not-move, why not atime |
| [Players](docs/PLAYERS.md) | write an adapter for another media server |
| [Observer](docs/OBSERVER.md) | the eBPF access tap: what it sees, and the traps |
| [Dashboard](docs/DASHBOARD.md) | the web view, and how to add a data source |

## Testing

```sh
python3 tests/test_tier.py            # eviction, backup guards
python3 tests/test_promote.py         # promotion, sizing
python3 tests/test_observer.py        # access signatures and classification
python3 tests/test_observer_adapter.py
```

330 checks. They use temporary directories and a fake player — they never contact a real
media server, never touch real storage, and never load anything into the kernel. `rsync`
is required by the first two; they skip with a clear message if it is missing.

If you change ranking, eviction or the guards, run these first. Several of them exist
because the behaviour they check was once wrong.

## Status

Working, and in use. Treat the `backup` mode with the caution it deserves: run
`--dry-run` first, check the deletion counts it reports, and set `max_delete` to
something you have actually thought about.

## Licence

[MIT](LICENSE). It moves and deletes your files — the warranty disclaimer is not
boilerplate here, so read the safety notes above and dry-run first.
