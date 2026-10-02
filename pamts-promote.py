#!/usr/bin/env python3
"""PAMTS promotion: move content back onto fast storage when it is PLAYED.

The counterpart to pamts-tier.py. Tiering moves content off fast storage as it ages;
this moves it back -- but only in response to actual playback.

Why playback and nothing else
-----------------------------
A library scan reads every file in the library. Any trigger based on the filesystem --
atime, inotify, or intercepting reads through a union filesystem -- cannot tell a
scanner's read from someone watching something, so a nightly scan would promote the
entire library. The signal is therefore the player's own notion of "playing", which a
scan does not produce.

Why it pays
-----------
It does nothing for the item being played: that is already streaming from slow storage
by the time we see it. It is the NEXT item that benefits -- and slow disks get to spin
back down instead of being woken again twenty minutes later. It is also nearly free,
because during playback the slow tier is already spun up.

Two modes
---------
  (default)   poll the player for what is playing and promote the run that follows.
  --next-up   make sure each series' next unwatched item is on fast storage. Run this
              after tiering, from the same nightly window. It is the other half of
              letting a whole-season download age onto slow storage: the season goes,
              the episode you are about to watch stays.

Promotion COPIES; it never moves. Slow storage remains the permanent home. A useful
consequence: evicting promoted content later is nearly free, because rsync finds the
destination already identical and only has to remove the source.

Usage:
    pamts-promote.py --dry-run
    pamts-promote.py
    pamts-promote.py --next-up
    pamts-promote.py --dry-run --simulate-recent --simulate-count 6
"""

import argparse
import fcntl
import logging
import os
import subprocess
import sys
import time

import pamts
import pamts_events
import pamts_players
from pamts_players import Caps


def human(n):
    return pamts.human(n)


def tier_footprint(paths):
    total = 0
    for p in paths:
        for dirpath, _d, files in os.walk(p, followlinks=False):
            for fn in files:
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if not os.path.islink(fp):
                    total += st.st_size
    return total


class Headroom:
    """Promotable bytes remaining, PER BUDGET POOL.

    Promotion used to measure one footprint against one budget. Once a medium has its
    own `budget_gb` that is wrong in both directions: a full video pool would block
    music promotion, and an empty music pool would appear to licence video promotion.

    It was wrong in practice, not just in theory. With music tiered into its own 800 GB
    pool the combined fast-tier footprint reached 1.3 TB against the global 400 GB video
    budget, so the single `budget_left` went negative and promotion refused every item
    on every pass -- silently disabling the feature, while logging a cheerful
    "promotable headroom 0B".

    Pools are derived from the tier jobs exactly as eviction groups them: a job with its
    own budget_gb is its own pool, everything else shares the global one.
    """

    def __init__(self, jobs, shared_gb, reserve_bytes):
        self.pools = {}
        for j in jobs:
            if j.get("mode") != "tier":
                continue
            own = j.get("budget_gb")
            key = j["name"] if own else "__shared__"
            pool = self.pools.setdefault(
                key, {"budget": int(float(own or shared_gb) * pamts.GB),
                      "sources": [], "footprint": 0})
            pool["sources"].append(os.path.abspath(j["source"]).rstrip("/"))
        for key, pool in self.pools.items():
            live = [p for p in pool["sources"] if os.path.isdir(p)]
            pool["footprint"] = tier_footprint(live)
            # Promotion may fill the pool to its BUDGET. The reserve is not subtracted
            # here, and that is the whole point of it.
            #
            # Eviction evicts down to budget - reserve. If promotion then also stopped
            # at budget - reserve, the two would converge on the same line and the
            # reserve would be set aside and never usable. Measured in production the
            # morning after it was introduced: "shared 347.4G of 400.0G (0B left);
            # music-organised 739.7G of 800.0G (271.4M left)" -- 60 GB reserved in each
            # pool, none of it available, and not one album promotable.
            #
            # The reserve is the working space BETWEEN the two lines: eviction keeps
            # the floor at budget - reserve, promotion may climb to budget, and a
            # promoted item is protected for promote_protect_days so the next eviction
            # takes staler content rather than undoing the promotion.
            pool["left"] = pool["budget"] - pool["footprint"]
            pool["reserve"] = reserve_bytes

    def pool_of(self, fast_path):
        """Longest matching tier source wins, so nested sources resolve correctly."""
        best, best_len = None, -1
        for key, pool in self.pools.items():
            for src in pool["sources"]:
                if fast_path == src or fast_path.startswith(src + "/"):
                    if len(src) > best_len:
                        best, best_len = key, len(src)
        return best

    def left(self, fast_path):
        key = self.pool_of(fast_path)
        return self.pools[key]["left"] if key else 0

    def charge(self, fast_path, nbytes):
        key = self.pool_of(fast_path)
        if key:
            self.pools[key]["left"] -= nbytes

    def any_left(self):
        return any(p["left"] > 0 for p in self.pools.values())

    def describe(self):
        return "; ".join(
            "%s %s of %s (%s promotable)" % (
                "shared" if k == "__shared__" else k,
                human(p["footprint"]), human(p["budget"]), human(max(0, p["left"])))
            for k, p in sorted(self.pools.items()))


def effective_caps(caps, headroom, remaining_s, rate_bps):
    """Scale an adapter's ceiling down by the space AND the time actually available.

    Space: generous when the fast tier is nearly empty, conservative as it fills.
    Time:  only what can realistically be copied before the current item ends. Copying
           past that point does not help the viewer and competes with the stream slow
           storage is already serving, hence the safety factor.

    Floors survive both limits, so the immediately-next item is always attempted:
    being slightly late with it still beats not fetching it at all.
    """
    P = pamts.PROMOTE
    min_bytes = int(P["min_event_bytes_gb"]) * pamts.GB
    by_space = max(min_bytes, min(headroom * float(P["headroom_fraction"]),
                                  caps.max_bytes))
    limits = [caps.max_bytes, by_space]
    note = []
    if remaining_s > 0:
        by_time = remaining_s * rate_bps * float(P["time_safety"])
        limits.append(max(min_bytes, by_time))
        note.append(f"{human(by_time)} fits in {remaining_s / 60:.0f} min at "
                    f"{human(rate_bps)}/s x{P['time_safety']}")
    allowance = int(min(limits))
    # Items scale with the byte allowance, so a nearly-full tier does not instead
    # promote a long run of small files.
    frac = 1.0 if caps.max_bytes <= 0 else allowance / caps.max_bytes
    items = max(int(P["min_event_items"]), int(round(caps.max_items * frac)))
    if note:
        logging.info(f"  budget for this event: {human(allowance)}, "
                     f"{min(items, caps.max_items)} item(s) ({'; '.join(note)})")
    return Caps(max_items=min(items, caps.max_items), max_bytes=allowance)


def rule_caps(base, rule, want_current):
    """Apply a [[promote.rules]] override to an adapter's ceilings.

    `lookahead.items` counts the items that FOLLOW. The currently-playing item, when
    the rule lets it through, is allowed on top of that, so `items = 0` still promotes
    the film itself rather than nothing at all.
    """
    if not rule:
        return base
    items = base.max_items if rule["look_items"] is None else rule["look_items"]
    if want_current:
        items += 1
    nbytes = base.max_bytes if rule["max_bytes"] is None else rule["max_bytes"]
    return pamts_players.Caps(max_items=max(1, items), max_bytes=nbytes)


def filter_candidates(cands, caps, budget_left):
    """Drop what is already local or unavailable, then apply the caps.

    `budget_left` is either a byte count or a Headroom, which charges each item against
    the budget pool that actually owns it.
    """
    suffixes = tuple(pamts.TIER["inprogress_suffixes"])
    chosen, used = [], 0
    for c in sorted(cands, key=lambda x: x.order):
        m = pamts.split_root(c.path)
        if not m:
            logging.warning(f"    {c.label}: not under a configured root ({c.path})")
            continue
        rel, fast_root, slow_root = m
        fpath, spath = os.path.join(fast_root, rel), os.path.join(slow_root, rel)
        if rel.endswith(suffixes):
            continue
        if os.path.exists(fpath):
            continue                          # already on fast storage
        if not os.path.exists(spath):
            logging.info(f"    {c.label}: not on slow storage either - skipping")
            continue
        try:
            size = os.path.getsize(spath)
        except OSError:
            size = c.size
        if len(chosen) >= caps.max_items:
            logging.info(f"    {c.label}: hit the {caps.max_items}-item cap - stopping")
            break
        if used + size > caps.max_bytes:
            logging.info(f"    {c.label}: would exceed the {human(caps.max_bytes)} "
                         "per-event cap - stopping")
            break
        avail = (budget_left.left(fpath) if isinstance(budget_left, Headroom)
                 else budget_left - used)
        if size > avail:
            logging.info(f"    {c.label}: no budget headroom left in its pool "
                         f"({human(max(0, avail))}) - stopping")
            break
        if isinstance(budget_left, Headroom):
            budget_left.charge(fpath, size)
        # Protection is recorded against the CONTAINING directory, which is the unit
        # tiering evicts. pamts.is_protected also matches prefixes in both directions,
        # so a granularity mismatch over-protects rather than leaving this exposed.
        chosen.append({"rel": rel, "src": spath, "dst": fpath, "size": size,
                       "label": c.label, "group_dir": os.path.dirname(fpath),
                       "pool": (budget_left.pool_of(fpath)
                                if isinstance(budget_left, Headroom) else None),
                       "reason": "playing now" if c.order < 0 else "lookahead"})
        used += size
    return chosen


def copy_items(items, dry_run, stats=None):
    """Copy each item onto fast storage. `stats` accumulates bytes/seconds so the
    throughput estimate can self-tune from real transfers."""
    ok = True
    for it in items:
        logging.info(f"    promoting {it['label']} {human(it['size'])}  {it['rel']}")
        if dry_run:
            continue
        t0 = time.time()
        try:
            os.makedirs(os.path.dirname(it["dst"]), exist_ok=True)
        except OSError as e:
            logging.error(f"    cannot create {os.path.dirname(it['dst'])}: {e}")
            ok = False
            continue
        # COPY. Never --remove-source-files: slow storage is the permanent home.
        # Default temp-then-rename (not --inplace) so a partial file is never served.
        cmd = ["ionice", "-c3", "rsync", "-a", "--timeout=600",
               "--partial-dir=.rsync-partial", it["src"], it["dst"]]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            logging.error(f"    rsync failed rc={p.returncode}: {p.stderr.strip()[:200]}")
            ok = False
        else:
            dt = max(0.001, time.time() - t0)
            if stats is not None:
                stats["bytes"] = stats.get("bytes", 0) + it["size"]
                stats["seconds"] = stats.get("seconds", 0.0) + dt
                logging.info(f"      {human(it['size'] / dt)}/s")
            pamts_events.record_transfer(
                "promote", it["rel"], it["size"], dt, pool=it.get("pool"),
                label=it.get("label"), reason=it.get("reason"))
    return ok


def record_rate(state, stats):
    """Fold a measured transfer into the throughput EWMA."""
    if stats.get("seconds", 0) <= 0 or stats.get("bytes", 0) <= 0:
        return
    measured = stats["bytes"] / stats["seconds"]
    alpha = float(pamts.PROMOTE["rate_alpha"])
    prev = state.get("rate_bps")
    state["rate_bps"] = measured if not prev else (alpha * measured + (1 - alpha) * prev)
    logging.info(f"throughput this pass {human(measured)}/s; "
                 f"estimate now {human(state['rate_bps'])}/s")


def do_next_up(players, budget, budget_left, footprint, state, stats, now, dry_run):
    """Make sure each series' next unwatched item is on fast storage.

    Run nightly, after eviction. Tiering deliberately lets a whole-season download age
    onto slow storage keeping only the pinned items; this is the other half of that
    bargain -- it fetches any pin that is not actually local.

    Not speculative in the way film prefetch would be: "the next unwatched episode of
    a series you are part-way through" is the strongest predictor there is.
    """
    items, hist_ok, _failed = pamts_players.sweep(players)
    if not items:
        logging.error("[next-up] no library data from any player that provides history")
        return 1
    if not hist_ok:
        logging.warning("[next-up] no player supplied play history; pins are derived "
                        "from item metadata only")
    # Fold in observed plays, so an item played in a session-only app is not treated as
    # unwatched and pinned forever.
    observed = pamts.observed_history()
    if observed:
        folded = 0
        for it in items:
            o = observed.get(it["fast"], 0)
            if o > (it.get("last_viewed") or 0):
                it["last_viewed"] = o
                folded += 1
        logging.info(f"[next-up] folded in {folded} observed play(s)")
    # The FOOTPRINT, not the remaining headroom: headroom has the anti-thrash reserve
    # taken out, and using it here made this derive a different depth from the eviction
    # side. Both must compute the identical pin set.
    #
    # Pin depth is still derived from the SHARED pool. Pins are a next-episode notion,
    # so they live entirely in the video pool; deriving the depth from the estate total
    # would let a terabyte of albums decide how many episodes to hold.
    shared = budget_left.pools.get("__shared__") if isinstance(budget_left, Headroom) \
        else None
    if shared:
        depth = pamts.pin_depth(shared["footprint"], shared["budget"])
    else:
        depth = pamts.pin_depth(footprint, budget)
    pins = pamts.next_up(items, int(pamts.TIER["next_up_max_items"]),
                         int(pamts.TIER["next_up_max_gb"]) * pamts.GB, depth)
    logging.info(f"[next-up] pinning {depth} item(s) ahead per series; {len(pins)} "
                 f"pin(s), {human(sum(p['size'] or 0 for p in pins.values()))}")

    missing = []
    for fast_path, info in sorted(pins.items(), key=lambda kv: kv[1].get("depth", 0)):
        if os.path.exists(fast_path):
            continue
        if not os.path.exists(info["slow"]):
            continue                      # on neither tier; nothing to fetch
        label = f"{info['show']} S{info['season']}E{info['episode']}"
        missing.append({"rel": info["rel"], "src": info["slow"], "dst": fast_path,
                        "size": info["size"] or 0, "label": label,
                        "group_dir": os.path.dirname(fast_path),
                        "pool": (budget_left.pool_of(fast_path)
                                 if isinstance(budget_left, Headroom) else None),
                        "reason": "next-up"})
    if not missing:
        logging.info("[next-up] every pinned item is already on fast storage")
        return 0

    fit, used = [], 0
    for m in missing:
        if isinstance(budget_left, Headroom):
            avail = budget_left.left(m["dst"])
            if m["size"] > avail:
                logging.info(f"[next-up] no headroom in {m['label']}'s pool "
                             f"({human(max(0, avail))}) - skipping it")
                continue          # another pool may still have room
            budget_left.charge(m["dst"], m["size"])
        elif used + m["size"] > budget_left:
            logging.info(f"[next-up] no headroom for {m['label']} - stopping")
            break
        fit.append(m)
        used += m["size"]
    logging.info(f"[next-up] fetching {len(fit)} of {len(missing)} missing pin(s), "
                 f"{human(used)}")
    if copy_items(fit, dry_run, stats):
        for m in fit:
            state["promotions"][m["group_dir"]] = {
                "at": now, "source": "next-up", "label": m["label"],
                "last_rel": m["rel"]}
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(description="PAMTS promotion (reverse tiering)")
    ap.add_argument("--config", default=pamts.default_config_path())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--next-up", action="store_true",
                    help="ensure each series' next unwatched item is on fast storage")
    ap.add_argument("--simulate-recent", action="store_true",
                    help="treat recently PLAYED items as if playing (requires --dry-run)")
    ap.add_argument("--simulate-count", type=int, default=1)
    ap.add_argument("--budget-gb", type=float)
    args = ap.parse_args()

    try:
        pamts.load_and_configure(args.config)
    except pamts.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    pamts.setup_logging(pamts.PATHS["promote_log"], args.dry_run)
    # Telemetry only, and fail-safe: if the store cannot be opened, recording becomes a
    # no-op rather than taking the run down with it.
    pamts_events.configure(None if args.dry_run else pamts.PATHS.get("events_db"))
    # Trimming the event store is pamts-tier's job, not this one's: this runs every
    # 60 seconds and a DELETE that matches nothing still opens a write transaction,
    # so pruning here would be 1,440 needless ones a day.
    if args.simulate_recent and not args.dry_run:
        logging.error("--simulate-recent is a validation aid and requires --dry-run")
        return 2

    P = pamts.PROMOTE
    budget_gb = args.budget_gb if args.budget_gb is not None else pamts.TIER["budget_gb"]

    # If tiering holds the lock it is moving content the other way and the budget
    # arithmetic would be meaningless, so do not proceed.
    #
    # The 60-second poll does NOT wait: it will try again on the next tick, and queueing
    # up waiting pollers behind a long eviction would be worse than skipping. --next-up
    # DOES wait, because it runs once in the nightly window and skipping it means the
    # pins simply do not get fetched that night.
    lock = pamts.acquire_lock(pamts.PATHS["lock_file"], shared=args.dry_run,
                              wait_seconds=900 if (args.next_up and not args.dry_run) else 0)
    if lock is None:
        logging.info("another job holds the lock - skipping this pass")
        return 0

    now = time.time()
    fast_roots = sorted({f for f, _s in pamts.ROOTS.values()})
    live = [p for p in fast_roots if os.path.isdir(p)]
    reserve = int(float(P["headroom_gb"]) * pamts.GB)
    hr = Headroom(pamts.JOBS, budget_gb, reserve)
    budget_left = hr
    footprint = sum(pl["footprint"] for pl in hr.pools.values())
    budget = sum(pl["budget"] for pl in hr.pools.values())
    logging.info(f"fast tier holds {human(footprint)} across "
                 f"{len(hr.pools)} budget pool(s): {hr.describe()}")

    # The budget is a policy number; it does not know what else shares the filesystem.
    if live:
        try:
            st = os.statvfs(live[0])
            free = st.f_bavail * st.f_frsize
            logging.info(f"filesystem free: {human(free)}")
            if free < int(P["min_free_gb"]) * pamts.GB:
                logging.warning(f"only {human(free)} free, below the "
                                f"{P['min_free_gb']}G floor - refusing to promote")
                return 0
        except OSError as e:
            logging.warning(f"cannot statvfs {live[0]}: {e}")

    if not hr.any_left():
        logging.info("no headroom below budget in any pool - refusing to promote "
                     "(anti-thrash)")
        return 0

    state = pamts.load_state()
    # Fingerprint as loaded. Every save below passes it, so a pass that changes
    # nothing -- which is almost every one of the 1,440 a day -- does not rewrite a
    # multi-megabyte file to say so.
    state0 = pamts.state_fingerprint(state)
    # Cursors live in their own small file (see pamts.load_watermarks). They advance
    # every pass by definition, so keeping them in state.json meant state.json changed
    # every pass, which is what defeated the no-op skip above.
    marks = pamts.load_watermarks(state)
    state.pop("watermarks", None)
    dropped = pamts.prune_state(state, now)
    if dropped:
        logging.info(f"released {dropped} expired protection record(s)")
    rate = state.get("rate_bps") or int(P["default_rate_mbps"]) * pamts.MB
    logging.info(f"throughput estimate: {human(rate)}/s"
                 f"{' (measured)' if state.get('rate_bps') else ' (default, unmeasured)'}")
    stats = {}

    try:
        players = pamts_players.build_all(pamts.PLAYERS)
    except pamts.ConfigError as e:
        logging.error(str(e))
        return 2

    if args.next_up:
        rc = do_next_up(players, budget, budget_left, footprint, state, stats, now,
                        args.dry_run)
        record_rate(state, stats)
        pamts.prune_observed(state, now)
        pamts.save_watermarks(marks, args.dry_run)
        pamts.save_state(state, args.dry_run, previous=state0)
        return rc

    # Ask every player what is playing, keeping each event paired with the adapter that
    # produced it -- locality is adapter-specific, so the answer must come from the same
    # one. Several players may be serving different things at the same time.
    pairs, rc = [], 0
    for pl in players:
        # available() FIRST, then the capability flag. An adapter may only discover what
        # it can do by asking the server -- Navidrome in history-only mode turns
        # provides_sessions off inside available() -- so reading the flag first sees its
        # optimistic initial value. That ordering made this loop call now_playing() on an
        # adapter with no credentials, which raised KeyError('username') and set rc=1, so
        # pamts-promote.service exited 1 on EVERY one of its 1,440 daily runs. It was
        # invisible because promotion still worked for the players that did answer.
        # sweep() already does this in the right order and has a test saying why.
        if not pl.available():
            rc = 1
            continue
        if not pl.provides_sessions:
            continue
        try:
            evs = (pl.simulate_recent(args.simulate_count) if args.simulate_recent
                   else pl.now_playing())
        except Exception as e:
            logging.error(f"[{pl.name}] cannot query: {e}")
            rc = 1
            continue

        # Plays recorded since we last looked. This is the multi-user trigger: a
        # server's own play records cover every user, whereas a session API may only
        # show the calling account's sessions (and whether it shows others is
        # server- and role-dependent). It also catches plays that happened between
        # runs, which a session poll inevitably misses.
        if not args.simulate_recent:
            mark = marks.get(pl.name)
            if mark is None:
                # First sight of this player: adopt now as the baseline. Without this,
                # the "since" window would be the whole of recorded history and the
                # first run would try to promote the entire library.
                marks[pl.name] = now
                logging.info(f"[{pl.name}] first run - recording a play watermark; "
                             "recent-play detection starts from now")
            else:
                try:
                    recent = pl.recent_plays(mark)
                except Exception as e:
                    logging.error(f"[{pl.name}] recent_plays failed: {e}")
                    recent = []
                    rc = 1
                if recent:
                    logging.info(f"[{pl.name}] {len(recent)} play(s) recorded since "
                                 f"the last pass")
                    # A track can appear in both lists; keep one event per path.
                    known = {e.path for e in evs}
                    evs = list(evs) + [e for e in recent if e.path not in known]
                marks[pl.name] = now

        if not evs:
            logging.info(f"[{pl.name}] nothing playing")
        pairs.extend((pl, ev) for ev in evs)

    if not pairs:
        pamts.prune_observed(state, now)
        pamts.save_watermarks(marks, args.dry_run)
        pamts.save_state(state, args.dry_run, previous=state0)
        return rc
    if args.simulate_recent:
        logging.warning("SIMULATED events - nothing will be promoted")

    # Record what is playing as PAMTS's own play history. This is what makes ranking
    # work for players that expose none (LMS), and it can never be written by a library
    # scan, because a scan is not a playing session.
    if not args.simulate_recent:
        seen = []
        for _pl, ev in pairs:
            m = pamts.split_root(ev.path)
            if m:
                seen.append(os.path.join(m[1], m[0]))
        added = pamts.observe_plays(state, seen, now)
        if added:
            logging.info(f"recorded {added} observed play(s)")
    # The clock the `current.after_seconds` gate reads. Kept for everything playing,
    # including under --simulate-recent, so a dry run reports the same decision a real
    # one would.
    pamts.track_playing(state, [ev.path for _pl, ev in pairs], now)

    promoted = 0
    for pl, ev in pairs:
        logging.info(f"[{pl.name}] playing {ev.kind}: {ev.label}")
        rule = pamts.promote_rule(ev.kind)
        cands = []

        # The item playing RIGHT NOW. An experiment settled that this cannot help the
        # stream in flight: the NFSv4 client holds the file open for its whole
        # duration, so nfsd never ages out its handle and mergerfs never re-resolves
        # which branch to read from -- the copy already running finishes from slow
        # storage whatever we do. It is still worth promoting, because a seek, a
        # resume, or a re-watch all open the file afresh and get the fast copy. The
        # delay is what stops a ten-second skim through a library dragging whole films
        # across.
        want_current = False
        if rule and rule["current_after"] >= 0:
            waited = pamts.playing_for(state, ev.path, now)
            if waited >= rule["current_after"]:
                want_current = True
                cands.append(pamts_players.Candidate(
                    path=ev.path, label=f"{ev.label} (playing now)", order=-1))
            else:
                logging.info(f"  not promoting the playing item yet: {waited:.0f}s of "
                             f"{rule['current_after']:.0f}s watched")

        try:
            group = pl.locality_group(ev)
        except Exception as e:
            logging.error(f"  cannot resolve locality group: {e}")
            rc = 1
            group = []
        if rule and rule["look_items"] == 0 and group:
            logging.info(f"  lookahead is off for {ev.kind}: ignoring "
                         f"{len(group)} following item(s)")
            group = []
        cands.extend(group)

        if not cands:
            logging.info("  nothing to promote for this item")
            continue
        # Scale this event's allowance by the headroom of the pool that owns it, not
        # by the estate total: a near-full video pool must not shrink an album fetch.
        m = pamts.split_root(ev.path)
        ev_fast = os.path.join(m[1], m[0]) if m else None
        ev_head = hr.left(ev_fast) if ev_fast else 0
        caps = effective_caps(rule_caps(pl.caps, rule, want_current),
                              ev_head, ev.remaining_s, rate)
        items = filter_candidates(cands, caps, hr)
        if not items:
            logging.info("  nothing to promote (already local, or capped)")
            continue
        logging.info(f"  {len(items)} item(s), {human(sum(i['size'] for i in items))}")
        if copy_items(items, args.dry_run, stats):
            for it in items:
                state["promotions"][it["group_dir"]] = {
                    "at": now, "source": pl.name, "label": ev.label,
                    "last_rel": it["rel"]}
            promoted += sum(i["size"] for i in items)
        else:
            rc = 1
    record_rate(state, stats)
    pamts.prune_observed(state, now)
    pamts.save_watermarks(marks, args.dry_run)
    pamts.save_state(state, args.dry_run, previous=state0)
    logging.info(f"promoted {human(promoted)} this pass")
    return rc


if __name__ == "__main__":
    sys.exit(main())
