"""PAMTS shared library: configuration, path mapping, state, helpers.

Imported by pamts-tier.py and pamts-promote.py. Both must agree on how a media
player's path maps onto your two storage tiers, and on the limits that govern
pinning -- if they disagreed, the guarantees described in docs/DESIGN.md would
quietly stop holding. That agreement is why this file exists.

Configuration is loaded once and assigned to module-level globals via configure().
That is deliberately simple: the scripts read plain module constants, and the test
suites override those constants directly.
"""

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
        "lock_file": "/run/pamts.lock",
        "state_file": "/var/lib/pamts/state.json",
        "tier_log": "/var/log/pamts-tier.log",
        "promote_log": "/var/log/pamts-promote.log",
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
    global PATHS, TIER, PROMOTE, HISTORY, PLAYERS, ROOTS, JOBS
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
            if j.get("depth") or j.get("grace") is not None:
                raise ConfigError(f"backup job {name!r} must not set 'depth'/'grace' "
                                  "(those are tier-only settings)")
        else:
            if j.get("max_delete"):
                raise ConfigError(
                    f"tier job {name!r} sets 'max_delete'. Tier jobs must NEVER use "
                    "--delete: the fast tier emptying is normal and expected, and "
                    "--delete would erase the permanent copy. Remove it.")
        JOBS.append(dict(j))
    if not JOBS:
        raise ConfigError("no [[jobs]] configured; there is nothing for PAMTS to do")

    if not 0 < float(PROMOTE["headroom_fraction"]) <= 1:
        raise ConfigError("[promote] headroom_fraction must be between 0 and 1")
    if not 0 < float(PROMOTE["time_safety"]) <= 1:
        raise ConfigError("[promote] time_safety must be between 0 and 1")
    if int(TIER["pin_depth_max"]) < 1:
        raise ConfigError("[tier] pin_depth_max must be at least 1")
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
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass        # a missing log directory must not stop the run
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - " + ("[DRY-RUN] " if dry_run else "") + "%(message)s",
        handlers=handlers, force=True)


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
def pin_depth(headroom_bytes, budget_bytes):
    """How many items ahead to pin, scaled by free headroom.

    Nearly-empty tier -> keep a longer run local; there is no reason not to.
    Nearly-full tier  -> one item per series, which is the actual guarantee.
    """
    dmax = int(TIER["pin_depth_max"])
    if budget_bytes <= 0:
        return 1
    frac = max(0.0, min(1.0, headroom_bytes / budget_bytes))
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
