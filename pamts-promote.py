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


def filter_candidates(cands, caps, budget_left):
    """Drop what is already local or unavailable, then apply the caps."""
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
        if used + size > budget_left:
            logging.info(f"    {c.label}: no budget headroom left "
                         f"({human(max(0, budget_left - used))}) - stopping")
            break
        # Protection is recorded against the CONTAINING directory, which is the unit
        # tiering evicts. pamts.is_protected also matches prefixes in both directions,
        # so a granularity mismatch over-protects rather than leaving this exposed.
        chosen.append({"rel": rel, "src": spath, "dst": fpath, "size": size,
                       "label": c.label, "group_dir": os.path.dirname(fpath)})
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
        elif stats is not None:
            dt = max(0.001, time.time() - t0)
            stats["bytes"] = stats.get("bytes", 0) + it["size"]
            stats["seconds"] = stats.get("seconds", 0.0) + dt
            logging.info(f"      {human(it['size'] / dt)}/s")
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


def do_next_up(players, budget, budget_left, state, stats, now, dry_run):
    """Make sure each series' next unwatched item is on fast storage.

    Run nightly, after eviction. Tiering deliberately lets a whole-season download age
    onto slow storage keeping only the pinned items; this is the other half of that
    bargain -- it fetches any pin that is not actually local.

    Not speculative in the way film prefetch would be: "the next unwatched episode of
    a series you are part-way through" is the strongest predictor there is.
    """
    items, hist_ok = pamts_players.sweep(players)
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
    depth = pamts.pin_depth(budget_left, budget)
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
                        "group_dir": os.path.dirname(fast_path)})
    if not missing:
        logging.info("[next-up] every pinned item is already on fast storage")
        return 0

    fit, used = [], 0
    for m in missing:
        if used + m["size"] > budget_left:
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
    if args.simulate_recent and not args.dry_run:
        logging.error("--simulate-recent is a validation aid and requires --dry-run")
        return 2

    P = pamts.PROMOTE
    budget_gb = args.budget_gb if args.budget_gb is not None else pamts.TIER["budget_gb"]

    # Shared lock: if tiering holds it, it is moving content the other way and the
    # budget arithmetic would be meaningless.
    lock = open(pamts.PATHS["lock_file"], "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.info("another PAMTS job holds the lock - skipping this pass")
        return 0

    now = time.time()
    fast_roots = sorted({f for f, _s in pamts.ROOTS.values()})
    live = [p for p in fast_roots if os.path.isdir(p)]
    footprint = tier_footprint(live)
    budget = budget_gb * pamts.GB
    budget_left = budget - int(P["headroom_gb"]) * pamts.GB - footprint
    logging.info(f"fast tier holds {human(footprint)}; budget {human(budget)}; "
                 f"promotable headroom {human(max(0, budget_left))}")

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

    if budget_left <= 0:
        logging.info("no headroom below budget - refusing to promote (anti-thrash)")
        return 0

    state = pamts.load_state()
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
        rc = do_next_up(players, budget, budget_left, state, stats, now, args.dry_run)
        record_rate(state, stats)
        pamts.prune_observed(state, now)
        pamts.save_state(state, args.dry_run)
        return rc

    # Ask every player what is playing, keeping each event paired with the adapter that
    # produced it -- locality is adapter-specific, so the answer must come from the same
    # one. Several players may be serving different things at the same time.
    pairs, rc = [], 0
    for pl in players:
        if not pl.provides_sessions:
            continue
        if not pl.available():
            rc = 1
            continue
        try:
            evs = (pl.simulate_recent(args.simulate_count) if args.simulate_recent
                   else pl.now_playing())
        except Exception as e:
            logging.error(f"[{pl.name}] cannot query: {e}")
            rc = 1
            continue
        if not evs:
            logging.info(f"[{pl.name}] nothing playing")
        pairs.extend((pl, ev) for ev in evs)

    if not pairs:
        pamts.prune_observed(state, now)
        pamts.save_state(state, args.dry_run)
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

    promoted = 0
    for pl, ev in pairs:
        logging.info(f"[{pl.name}] playing {ev.kind}: {ev.label}")
        try:
            cands = pl.locality_group(ev)
        except Exception as e:
            logging.error(f"  cannot resolve locality group: {e}")
            rc = 1
            continue
        if not cands:
            logging.info("  no locality group for this item - nothing to promote")
            continue
        caps = effective_caps(pl.caps, budget_left - promoted, ev.remaining_s, rate)
        items = filter_candidates(cands, caps, budget_left - promoted)
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
    pamts.save_state(state, args.dry_run)
    logging.info(f"promoted {human(promoted)} this pass")
    return rc


if __name__ == "__main__":
    sys.exit(main())
