"""PAMTS shared library: configuration, path mapping, state, helpers.

Imported by pamts-tier.py and pamts-promote.py. Both must agree on how a media
player's path maps onto your two storage tiers, and on the limits that govern
pinning -- if they disagreed, the guarantees described in docs/DESIGN.md would
quietly stop holding. That agreement is why this file exists.

Configuration is loaded once and assigned to module-level globals via configure().
That is deliberately simple: the scripts read plain module constants, and the test
suites override those constants directly.
"""

import fcntl
import json
import logging
import os
import sys
import time

try:
    import tomllib                      # Python 3.11+
except ModuleNotFoundError:              # pragma: no cover
    tomllib = None

GB = 1024 ** 3
MB = 1024 ** 2

# --------------------------------------------------------------------- defaults
# Every one of these can be overridden in the config file. The defaults are
# deliberately conservative: they favour leaving content where it is over moving it.
DEFAULTS = {
    "paths": {
        # Under /var/lib, not /run: flock is held on an inode, so a lock file that gets
        # deleted and recreated between runs hands two processes a "lock" each. /run is
        # where stale files get tidied by hand.
        "lock_file": "/var/lib/pamts/pamts.lock",
        "state_file": "/var/lib/pamts/state.json",
        "tier_log": "/var/log/pamts-tier.log",
        "promote_log": "/var/log/pamts-promote.log",
        # Append-only record of transfers and tier utilisation, for the dashboard's
        # history and graphs. Separate from the observer's database on purpose -- see
        # pamts_events. Set to "" to disable recording entirely.
        "events_db": "/var/lib/pamts/events.db",
    },
    "tier": {
        "budget_gb": 400,
        "settle_seconds": 3600,
        "new_grace_days": 14,
        "promote_protect_days": 7,
        "next_up_max_items": 40,
        "next_up_max_gb": 80,
        "pin_depth_max": 4,
        # A series you have never started is a GUESS. The next episode of one you are
        # part-way through is a near-certainty. So unstarted series rank strictly below
        # every in-progress one and are capped separately -- otherwise a series added
        # today outranks one watched yesterday and speculation eats the budget.
        "next_up_include_unstarted": True,
        "next_up_unstarted_days": 30,     # only if added this recently: a series sitting
                                          # untouched for months is not imminent
        "next_up_unstarted_max": 3,       # at most this many unstarted series
        # Filesystem types a replica/slow destination may legitimately live on.
        # The guard exists because writing to an UNMOUNTED mountpoint silently fills
        # the local root filesystem -- and with --remove-source-files, then deletes
        # the originals. Set this to the filesystem type your slow tier really is.
        # Use ["*"] to disable the check entirely (NOT recommended).
        "dest_fstypes": ["nfs", "nfs4"],
        # Metadata written beside the media -- .nfo, artwork, subtitles, playlists.
        # These are EXCLUDED from a directory's mtime, because ranking uses mtime
        # whenever there is no play record and a metadata rewrite is not a sign the
        # content is fresh. Lidarr's XbmcMetadata consumer rewrote 6,436 album.nfo
        # files nightly on this estate, which stamped every album with today's date and
        # sorted never-played albums to the BACK of the eviction queue -- the opposite
        # of what was wanted. A directory holding nothing but sidecars falls back to
        # their mtime, so this can never report a mtime of zero.
        "sidecar_suffixes": [".nfo", ".jpg", ".jpeg", ".png", ".webp", ".tbn",
                             ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt",
                             ".cue", ".lrc", ".m3u", ".m3u8", ".txt", ".sfv",
                             ".md5", ".bif", ".theme"],
        "inprogress_suffixes": [".part", ".tmp", ".!qB", ".partial", ".crdownload"],
    },
    "promote": {
        "headroom_gb": 60,
        "min_free_gb": 100,
        "headroom_fraction": 0.30,
        "min_event_bytes_gb": 6,
        "min_event_items": 2,
        "max_items": 24,
        "max_bytes_gb": 60,
        "time_safety": 0.40,
        "default_rate_mbps": 80,
        "rate_alpha": 0.3,
        "poll_seconds": 60,
    },
    "player": {
        "kind": "plex",
        "url": "",
        "token_file": "",
        "verify_tls": True,
    },
    "history": {
        # PAMTS records what it observes playing, building its own play history.
        #
        # This exists because not every player exposes play history over its API --
        # Lyrion/LMS, for instance, keeps play counts in a private database and its
        # JSON-RPC API reports none of it. Such a player can still drive promotion
        # (it reports what is playing), and with this it can also drive ranking,
        # after a warm-up period.
        #
        # It is scan-immune by construction: a library scan never appears as a
        # playing session, so it can never write a record here.
        "observe": True,
        "max_entries": 200000,      # cap, oldest dropped first
        "max_age_days": 1825,       # ~5 years; older records are pruned
    },
}

# Populated by configure(). Scripts read these; tests override them.
PATHS = dict(DEFAULTS["paths"])
TIER = dict(DEFAULTS["tier"])
PROMOTE = dict(DEFAULTS["promote"])
PROMOTE_RULES = []        # see load_and_configure / promote_rule
HISTORY = dict(DEFAULTS["history"])
PLAYERS = []        # one config dict per configured player
ROOTS = {}          # player path -> (fast tier path, slow tier path)
JOBS = []           # tier and backup jobs


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path):
    """Read and validate a config file. Returns the parsed dict."""
    if tomllib is None:
        raise ConfigError("Python 3.11+ is required (tomllib is missing)")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    return raw


def configure(raw):
    """Apply a parsed config to this module's globals, validating as we go.

    Validation is strict and loud. A tiering system that has misunderstood its own
    configuration will move data to the wrong place, so it is far better to refuse to
    start than to guess.
    """
    global PATHS, TIER, PROMOTE, PROMOTE_RULES, HISTORY, PLAYERS, ROOTS, JOBS
    PATHS = _merge(DEFAULTS["paths"], raw.get("paths"))
    TIER = _merge(DEFAULTS["tier"], raw.get("tier"))
    PROMOTE = _merge(DEFAULTS["promote"], raw.get("promote"))
    HISTORY = _merge(DEFAULTS["history"], raw.get("history"))

    # Several players may serve the same files -- a music library is commonly served
    # by two or three things at once. Play history is MERGED across them, because an
    # album played in one app must not look untouched to the others; ranking on a
    # single player's view would evict music that was played and simply not seen.
    #
    # [[players]] is the general form. A singular [player] is accepted as shorthand
    # for one entry.
    plist = raw.get("players")
    if plist and raw.get("player"):
        raise ConfigError("use either [player] or [[players]], not both")
    if not plist:
        plist = [raw.get("player") or {}]
    PLAYERS = []
    seen = set()
    for i, p in enumerate(plist):
        merged = _merge(DEFAULTS["player"], p)
        kind = str(merged.get("kind", "")).lower()
        if not merged.get("url"):
            raise ConfigError(f"player #{i + 1} ({kind or '?'}) has no 'url'")
        name = merged.get("name") or kind
        if name in seen:
            raise ConfigError(f"duplicate player name {name!r}; set a distinct 'name'")
        seen.add(name)
        merged["name"] = name
        PLAYERS.append(merged)
    if not PLAYERS:
        raise ConfigError("at least one player must be configured")

    roots = raw.get("roots") or []
    if not roots:
        raise ConfigError("at least one [[roots]] entry is required: PAMTS cannot map "
                          "a player's file path onto your tiers without it")
    ROOTS = {}
    for i, r in enumerate(roots):
        for key in ("player_path", "fast", "slow"):
            if not r.get(key):
                raise ConfigError(f"[[roots]] #{i + 1} is missing '{key}'")
        for key in ("player_path", "fast", "slow"):
            if not str(r[key]).startswith("/"):
                raise ConfigError(f"[[roots]] #{i + 1} '{key}' must be an absolute "
                                  f"path, got {r[key]!r}")
        if r["fast"] == r["slow"]:
            raise ConfigError(f"[[roots]] #{i + 1} has fast == slow ({r['fast']}); "
                              "the two tiers must be different locations")
        ROOTS[str(r["player_path"]).rstrip("/")] = (str(r["fast"]).rstrip("/"),
                                                    str(r["slow"]).rstrip("/"))

    JOBS = []
    names = set()
    for i, j in enumerate(raw.get("jobs") or []):
        name, mode = j.get("name"), j.get("mode")
        if not name:
            raise ConfigError(f"[[jobs]] #{i + 1} is missing 'name'")
        if name in names:
            raise ConfigError(f"duplicate job name {name!r}")
        names.add(name)
        if mode not in ("tier", "backup"):
            raise ConfigError(f"job {name!r}: mode must be 'tier' or 'backup', "
                              f"got {mode!r}")
        src, dst = j.get("source"), j.get("dest")
        if not src or not dst:
            raise ConfigError(f"job {name!r} needs both 'source' and 'dest'")
        if mode == "backup":
            # A backup job runs rsync --delete. Without a ceiling, one bad run can
            # empty the replica. Refuse to run at all rather than accept the risk.
            if not j.get("max_delete"):
                raise ConfigError(
                    f"backup job {name!r} has no 'max_delete'. Backup jobs propagate "
                    "deletions, so a circuit breaker is mandatory -- set it to a little "
                    "above the largest number of deletions you would consider normal.")
            if (j.get("depth") or j.get("grace") is not None
                    or j.get("budget_gb") is not None
                    or j.get("play_weight_days") is not None):
                raise ConfigError(
                    f"backup job {name!r} must not set 'depth'/'grace'/'budget_gb'/"
                    "'play_weight_days' (those are tier-only settings)")
        else:
            if j.get("max_delete"):
                raise ConfigError(
                    f"tier job {name!r} sets 'max_delete'. Tier jobs must NEVER use "
                    "--delete: the fast tier emptying is normal and expected, and "
                    "--delete would erase the permanent copy. Remove it.")
            # A tier job may carve out its own allowance. Omitting it shares the global
            # [tier] budget_gb -- which is what movies and TV do, deliberately, so a
            # quiet month of films lends its space to a heavy month of television.
            # Music does NOT want that: 4 TB of albums would swamp a 400 GB video pool.
            if j.get("budget_gb") is not None:
                try:
                    bg = float(j["budget_gb"])
                except (TypeError, ValueError):
                    raise ConfigError(f"tier job {name!r}: budget_gb must be a number")
                if bg <= 0:
                    raise ConfigError(
                        f"tier job {name!r}: budget_gb must be greater than 0. Omit it "
                        "to share the global [tier] budget_gb instead.")
                j = dict(j, budget_gb=bg)
            # How much apparent recency a DOUBLING of the play count is worth, in days.
            # 0 (the default) means rank on recency alone, which is right for TV.
            pw = j.get("play_weight_days")
            if pw is not None:
                try:
                    pw = float(pw)
                except (TypeError, ValueError):
                    raise ConfigError(
                        f"tier job {name!r}: play_weight_days must be a number")
                if pw < 0:
                    raise ConfigError(
                        f"tier job {name!r}: play_weight_days must be >= 0 "
                        "(0 disables play-count weighting for this job)")
                j = dict(j, play_weight_days=pw)
        JOBS.append(dict(j))
    if not JOBS:
        raise ConfigError("no [[jobs]] configured; there is nothing for PAMTS to do")

    # ------------------------------------------------------------ promotion rules
    # What to promote differs by medium, and the differences are not small. A film has
    # nothing after it, so the only useful promotion is the film itself. An episode has
    # a run behind it worth pulling. A track has an album, which is many small files
    # rather than a few large ones. One global cap cannot serve all three.
    #
    # A kind with no rule keeps the previous behaviour exactly: lookahead only, bounded
    # by the adapter's own Caps.
    PROMOTE_RULES = []
    seen_match = set()
    for i, r in enumerate(PROMOTE.get("rules") or []):
        where = f"[[promote.rules]] #{i + 1}"
        if not isinstance(r, dict):
            raise ConfigError(f"{where} must be a table")
        m = str(r.get("match") or "").strip()
        if not m:
            raise ConfigError(f"{where} is missing 'match' -- the kind of thing being "
                              "played: 'movie', 'episode', 'track', or '*' for any")
        if m in seen_match:
            raise ConfigError(f"duplicate {where} match {m!r}")
        seen_match.add(m)
        unknown = set(r) - {"match", "current", "lookahead", "max_bytes_gb"}
        if unknown:
            raise ConfigError(f"{where} has unknown key(s): {', '.join(sorted(unknown))}")

        cur = r.get("current") or {}
        if not isinstance(cur, dict):
            raise ConfigError(f"{where} 'current' must be a table, e.g. "
                              "current = {{ after_seconds = 120 }}")
        after = cur.get("after_seconds")
        # Absent means "do not promote what is already playing" -- the old behaviour.
        after = -1.0 if after is None else float(after)
        if after < 0 and cur.get("after_seconds") is not None:
            raise ConfigError(f"{where} current.after_seconds must be >= 0; omit "
                              "'current' entirely to leave the playing item alone")

        look = r.get("lookahead") or {}
        if not isinstance(look, dict):
            raise ConfigError(f"{where} 'lookahead' must be a table, e.g. "
                              "lookahead = {{ items = 3 }}")
        items = look.get("items")
        if items is not None:
            items = int(items)
            if items < 0:
                raise ConfigError(f"{where} lookahead.items must be >= 0 "
                                  "(0 disables lookahead for this kind)")
        gb = r.get("max_bytes_gb")
        if gb is not None:
            gb = float(gb)
            if gb <= 0:
                raise ConfigError(f"{where} max_bytes_gb must be greater than 0")

        PROMOTE_RULES.append({
            "match": m,
            "current_after": after,
            "look_items": items,
            "max_bytes": None if gb is None else int(gb * GB),
        })

    if not 0 < float(PROMOTE["headroom_fraction"]) <= 1:
        raise ConfigError("[promote] headroom_fraction must be between 0 and 1")
    if not 0 < float(PROMOTE["time_safety"]) <= 1:
        raise ConfigError("[promote] time_safety must be between 0 and 1")
    if int(TIER["pin_depth_max"]) < 1:
        raise ConfigError("[tier] pin_depth_max must be at least 1")
    for key in ("sidecar_suffixes", "inprogress_suffixes"):
        val = TIER[key]
        if not isinstance(val, (list, tuple)):
            raise ConfigError(f"[tier] {key} must be a list of suffixes")
        for suf in val:
            if not isinstance(suf, str) or not suf.startswith("."):
                raise ConfigError(f"[tier] {key} entries must be strings starting with "
                                  f"'.', got {suf!r}")
    return True


def load_and_configure(path):
    configure(load(path))


def default_config_path():
    return os.environ.get("PAMTS_CONFIG", "/etc/pamts/pamts.toml")


# ------------------------------------------------------------------- formatting
def human(n):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def setup_logging(log_file, dry_run):
    """Configure logging. Timestamps are UTC, always.

    This estate spans hosts in two zones: the cache and Plex run UTC, the music and
    download servers run Europe/London. Logging in local time meant correlating a
    tiering run against a player's scan required knowing which host wrote which line
    and whether BST was in effect. Stamping UTC everywhere removes the question, and
    the Z suffix says so rather than leaving a reader to assume.
    """
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass        # a missing log directory must not stop the run
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - " + ("[DRY-RUN] " if dry_run else "") + "%(message)s",
        handlers=handlers, force=True)
    # converter is a class attribute on Formatter, so set it on every handler's
    # formatter rather than trusting basicConfig to have made only one.
    for h in logging.getLogger().handlers:
        if h.formatter:
            h.formatter.converter = time.gmtime
            h.formatter.default_time_format = "%Y-%m-%d %H:%M:%S"
            h.formatter.default_msec_format = "%s.%03dZ"


# ----------------------------------------------------------------- path mapping
def split_root(path):
    """A player's file path -> (relative, fast tier root, slow tier root), or None.

    Longest prefix wins, so overlapping roots can coexist. That matters in practice:
    if you migrate a library from separate per-tier paths to a single unified path,
    listing both lets PAMTS keep working through the transition and afterwards.
    """
    if not path:
        return None
    for root in sorted(ROOTS, key=len, reverse=True):
        base = root.rstrip("/")
        if path.startswith(base + "/"):
            fast, slow = ROOTS[root]
            return path[len(base) + 1:], fast, slow
    return None


def dirs_at_depth(src, depth):
    """Directories `depth` levels below src -- the units eviction acts on.

    depth=1 suits a flat collection where each item is its own directory (films).
    depth=2 suits Show/Season layouts, making a SEASON the unit: watching through a
    series lets earlier seasons age off the fast tier while the current one stays.

    A directory shallower than `depth` with no subdirectories is returned as itself,
    so a series stored without season folders is still handled.
    """
    level = [src]
    for _ in range(max(1, int(depth))):
        nxt = []
        for p in level:
            try:
                subs = [e.path for e in os.scandir(p) if e.is_dir(follow_symlinks=False)]
            except OSError:
                subs = []
            if subs:
                nxt.extend(subs)
            elif p != src:
                nxt.append(p)
        level = nxt
    return sorted(level)


def is_protected(candidate, records):
    """Is `candidate` covered by any protection record, at any granularity?

    Prefix-aware in both directions, so a record written per-season still protects a
    per-show candidate and vice versa. A granularity mismatch must fail safe
    (over-protect); under-protecting would let a just-promoted item be evicted
    immediately, and the two halves of the system would fight.
    """
    c = candidate.rstrip("/")
    for r in records:
        r2 = r.rstrip("/")
        if c == r2 or c.startswith(r2 + "/") or r2.startswith(c + "/"):
            return r
    return None


# ------------------------------------------------------- next-to-watch pinning
def promote_rule(kind):
    """The [[promote.rules]] entry governing `kind`, or None if none applies.

    An exact match on the kind wins over a '*' catch-all, whatever order they appear
    in, so a general rule can sit alongside specific ones without shadowing them.
    """
    star = None
    for r in PROMOTE_RULES:
        if r["match"] == kind:
            return r
        if r["match"] == "*" and star is None:
            star = r
    return star


def track_playing(state, paths, now):
    """Remember when each currently-playing path was FIRST seen.

    Players report what is playing, not for how long, and no adapter exposes an
    elapsed time PAMTS can rely on. So it measures from the first poll that saw the
    item. Anything that has stopped is forgotten, so resuming it later starts the
    clock again instead of inheriting a stale "playing for days".
    """
    prev = state.get("playing") or {}
    state["playing"] = {p: prev.get(p, now) for p in paths}
    return state["playing"]


def playing_for(state, path, now):
    """Seconds `path` has been playing, per track_playing. 0 if this is the first poll."""
    first = (state.get("playing") or {}).get(path)
    return 0.0 if first is None else max(0.0, now - float(first))


def pin_depth(footprint_bytes, budget_bytes):
    """How many items ahead to pin, scaled by how full the fast tier is.

    Nearly-empty tier -> keep a longer run local; there is no reason not to.
    Nearly-full tier  -> one item per series, which is the actual guarantee.

    Takes the FOOTPRINT, not a headroom, deliberately. An earlier signature took
    "headroom", and the two callers disagreed about what that meant: eviction passed
    `budget - footprint` while promotion passed its own anti-thrash headroom
    (`budget - reserve - footprint`). They therefore derived different depths from the
    same state -- the exact divergence the shared module exists to prevent, masked only
    because the byte cap usually binds first. A footprint has one meaning.
    """
    dmax = int(TIER["pin_depth_max"])
    if budget_bytes <= 0:
        return 1
    headroom = budget_bytes - max(0, footprint_bytes)
    frac = max(0.0, min(1.0, headroom / budget_bytes))
    return max(1, int(round(1 + frac * (dmax - 1))))


def next_up(items, max_items, max_bytes, per_show=1, include_unstarted=None,
            unstarted_days=None, unstarted_max=None, now=None):
    """{fast path: info} -- the next unwatched item of each series.

    Why this exists: a whole-season download lands on the fast tier at once. Keeping
    all of it there wastes the budget, because only the next episode will be watched
    soon. So the season is allowed to age onto slow storage EXCEPT the next unwatched
    episode(s), which are pinned -- never evicted, and fetched back if missing.

    "Next" is the lowest (season, episode) with no view record, counting only items
    the player has a file for, so gaps in a season do not stall the pin.

    Capped, because a large library has hundreds of series and pinning one episode of
    every one could run to terabytes. Allocation is round-robin, so every series
    considered gets its FIRST pending episode before any gets a second.

    IN-PROGRESS BEATS UNSTARTED, ALWAYS
    -----------------------------------
    The next episode of a series you are part-way through is a near-certainty. Episode
    one of a series you have never started is a guess -- the same speculation this
    project declines to make for films.

    An earlier version ranked both on one scale: last-played for series with history,
    date-added for those without. Those are different kinds of timestamp, so a series
    added today outranked one watched yesterday, took the top of the list, and consumed
    the largest share of the budget on content nobody had touched. Now the two are
    separate classes: every in-progress series is allocated first and without a cap,
    and unstarted ones get only what survives -- at most `unstarted_max` of them, and
    only if added within `unstarted_days`. A series sitting unwatched for months is not
    imminent and is skipped entirely.
    """
    if include_unstarted is None:
        include_unstarted = bool(TIER.get("next_up_include_unstarted", True))
    if unstarted_days is None:
        unstarted_days = int(TIER.get("next_up_unstarted_days", 30))
    if unstarted_max is None:
        unstarted_max = int(TIER.get("next_up_unstarted_max", 3))
    if now is None:
        now = time.time()
    by_show = {}
    for it in items:
        if it.get("kind") != "episode" or it.get("show_key") is None:
            continue
        by_show.setdefault(it["show_key"], []).append(it)

    started, unstarted = [], []
    for _key, eps in by_show.items():
        pending = sorted(
            (e for e in eps if not e["last_viewed"]
             and e["season"] is not None and e["episode"] is not None),
            key=lambda e: (e["season"], e["episode"]))
        if not pending:
            continue
        run = pending[:max(1, per_show)]
        watched = [e["last_viewed"] for e in eps if e["last_viewed"]]
        if watched:
            started.append((max(watched), run))
            continue
        if not include_unstarted:
            continue
        added = max((e.get("added") or 0 for e in eps), default=0)
        if unstarted_days and added and (now - added) > unstarted_days * 86400:
            continue                    # untouched for months; not imminent
        unstarted.append((added, run))

    started.sort(key=lambda t: t[0], reverse=True)
    unstarted.sort(key=lambda t: t[0], reverse=True)

    pins, used = {}, 0

    def take(groups, is_unstarted, show_cap=None):
        nonlocal used
        shows = set()
        for depth in range(max(1, per_show)):
            for _prio, run in groups:
                if depth >= len(run):
                    continue
                e = run[depth]
                if len(pins) >= max_items:
                    return
                key = e.get("show") or e["rel"].split("/", 1)[0]
                if show_cap is not None and key not in shows and len(shows) >= show_cap:
                    continue
                if used + (e["size"] or 0) > max_bytes:
                    continue
                pins[e["fast"]] = {
                    "show": e.get("show"), "season": e.get("season"),
                    "episode": e.get("episode"), "slow": e["slow"],
                    "size": e["size"], "rel": e["rel"], "depth": depth,
                    "unstarted": is_unstarted,
                }
                used += e["size"] or 0
                shows.add(key)

    # In-progress first and uncapped: these are the pins worth guaranteeing.
    take(started, False)
    # Only what survives may go to a few freshly added series.
    take(unstarted, True, show_cap=max(0, unstarted_max))
    return pins


# ------------------------------------------------------------------------- locking
def acquire_lock(path, shared=False, wait_seconds=0):
    """Take the shared lock file. -> the open file object, or None on failure.

    `wait_seconds` matters. A job that gives up the instant it loses a race silently
    skips its work, and with promotion polling every 60 seconds that race is easy to
    lose: a real nightly deletion pass was skipped exactly this way, logging one ERROR
    line that was mistaken for success because the wrapper carried on. Long-running
    scheduled work should WAIT -- the other holder usually finishes in seconds -- while
    the frequent poller should not, because it will simply try again on its next tick.

    `shared` is for dry runs. They change nothing, so they should neither block nor be
    blocked, and two diagnostics should be able to run at once. Only a real run needs
    exclusivity.
    """
    try:
        f = open(path, "w")
    except OSError as e:
        logging.error(f"cannot open lock file {path}: {e}")
        return None
    mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB

    def holds_the_named_file():
        """Is the inode we just locked still the one this path refers to?

        flock is held on an INODE, not a name. If the file is replaced between our
        open() and our flock(), we end up holding a lock on an orphaned inode while
        the next process locks the new one, and both believe they have it -- which
        would let a promotion run straight into an eviction.

        This closes that window by re-opening and trying again. It CANNOT help once
        the file has simply been deleted and recreated between two runs: the second
        process opens a new inode and has no way to know an older one was ever locked.
        No flock scheme can. The real mitigation for that is to keep the lock file
        somewhere nothing tidies, which is why it lives under /var/lib rather than
        /run -- retiring the predecessor system meant deleting its stale lock by hand,
        and that is exactly the habit that breaks this.
        """
        try:
            return os.fstat(f.fileno()).st_ino == os.stat(path).st_ino
        except OSError:
            return False
    deadline = time.time() + max(0, wait_seconds)
    waited = False
    reopens = 0
    while True:
        try:
            fcntl.flock(f, mode)
            if not holds_the_named_file():
                # Replaced underneath us. Drop the orphaned inode and take the new one.
                logging.warning(f"the lock file {path} was replaced while acquiring it; "
                                "re-taking the lock on the current file")
                f.close()
                reopens += 1
                if reopens > 5:
                    logging.error(f"{path} keeps being replaced - refusing to run "
                                  "rather than race")
                    return None
                try:
                    f = open(path, "w")
                except OSError as e:
                    logging.error(f"cannot reopen lock file {path}: {e}")
                    return None
                continue
            if waited:
                logging.info("lock acquired after waiting")
            return f
        except OSError:
            if time.time() >= deadline:
                if wait_seconds:
                    logging.error(f"could not acquire the lock within {wait_seconds}s - "
                                  "another run is still going")
                return None
            if not waited:
                logging.info(f"another run holds the lock; waiting up to {wait_seconds}s")
                waited = True
            time.sleep(2)


# ------------------------------------------------------------------------ state
def load_state():
    try:
        with open(PATHS["state_file"]) as f:
            s = json.load(f)
            s.setdefault("promotions", {})
            return s
    except (OSError, ValueError):
        return {"promotions": {}}


def save_state(state, dry_run):
    if dry_run:
        return
    try:
        d = os.path.dirname(PATHS["state_file"])
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = PATHS["state_file"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, PATHS["state_file"])
    except OSError as e:
        logging.error(f"could not write state {PATHS['state_file']}: {e}")


def prune_state(state, now):
    """Drop protection records past their window so they stop pinning content."""
    cut = now - int(TIER["promote_protect_days"]) * 86400
    before = len(state.get("promotions", {}))
    state["promotions"] = {k: v for k, v in state.get("promotions", {}).items()
                           if isinstance(v, dict) and v.get("at", 0) >= cut}
    return before - len(state["promotions"])


def observe_plays(state, paths, now):
    """Record fast-tier paths seen playing, so PAMTS accumulates its own history.

    The identity is always the FAST-tier path, whichever tier the file is actually on,
    so a record survives the file moving between tiers.
    """
    if not HISTORY.get("observe", True):
        return 0
    obs = state.setdefault("observed", {})
    added = 0
    for p in paths:
        if p and obs.get(p, 0) < now:
            obs[p] = now
            added += 1
    return added


def prune_observed(state, now):
    """Keep the observed history bounded: drop ancient records, then cap the size."""
    obs = state.get("observed") or {}
    if not obs:
        return 0
    before = len(obs)
    cut = now - int(HISTORY["max_age_days"]) * 86400
    obs = {k: v for k, v in obs.items() if v >= cut}
    cap = int(HISTORY["max_entries"])
    if len(obs) > cap:
        # Newest first, keep the cap. Oldest plays are the least useful to remember.
        obs = dict(sorted(obs.items(), key=lambda kv: kv[1], reverse=True)[:cap])
    state["observed"] = obs
    return before - len(obs)


def observed_history():
    """{fast path: last-seen-playing epoch} from the state file, or {}."""
    try:
        with open(PATHS["state_file"]) as f:
            obs = json.load(f).get("observed", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in obs.items() if isinstance(v, (int, float))}


def protected_now(now):
    """{fast path: promoted_at} for protections still inside their window.

    A missing or unreadable state file means "nothing protected": eviction must still
    work if promotion has never run.
    """
    try:
        with open(PATHS["state_file"]) as f:
            recs = json.load(f).get("promotions", {})
    except (OSError, ValueError):
        return {}
    cut = now - int(TIER["promote_protect_days"]) * 86400
    return {k: v.get("at", 0) for k, v in recs.items()
            if isinstance(v, dict) and v.get("at", 0) >= cut}
