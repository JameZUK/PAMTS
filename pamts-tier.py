#!/usr/bin/env python3
"""PAMTS tiering and backup.

Two kinds of job, declared per pair in the config. The distinction is the whole
point of this script, so understand it before changing anything:

  BACKUP  The fast copy is authoritative and the other side is a replica, so
          deletions MUST propagate (rsync --delete). Use this for content that lives
          permanently on fast storage and is merely mirrored.

  TIER    The fast copy is a cache that is *expected* to empty as content ages out,
          and the slow side is the permanent home. rsync --remove-source-files moves
          content off the fast tier; --delete must NEVER be used here, because the
          source going empty is normal and --delete would erase the whole library.

Declaring the mode per job makes the fatal mistake unrepresentable: you cannot
accidentally apply delete-semantics to a cache, because caches are TIER jobs, and the
config loader refuses a tier job that specifies max_delete.

Safety, for BACKUP jobs specifically (these delete real data):
  * the destination must be a mountpoint of an expected filesystem type -- otherwise
    an unmounted mountpoint means writing into the local root filesystem
  * the source must exist and be non-empty -- an unmounted or empty source with
    --delete would erase the replica
  * max_delete is a circuit breaker: a pre-flight dry run COUNTS the deletions first,
    and if there are more than expected the job fails loudly having deleted nothing
  * excluded paths are never deleted on the destination (no --delete-excluded), so
    content that exists only on the replica behind an exclude is protected

For TIER jobs, eviction ranks by when something was last PLAYED, read from the media
player -- never by atime. See docs/DESIGN.md for why that distinction matters.

Usage:
    pamts-tier.py --dry-run             # report, change nothing
    pamts-tier.py                       # do it
    pamts-tier.py --only tier           # or: backup
    pamts-tier.py --job <name>          # a single named job
    pamts-tier.py --config /path/to/pamts.toml
"""

import argparse
import collections
import errno
import fcntl
import logging
import os
import subprocess
import sys
import time

import pamts
import pamts_players

# Set False only by the test suite; production must always require a real mount.
REQUIRE_DEST_MOUNT = True

# Sentinel: do_tier fetches its own ranking data unless supplied (tests).
_FETCH = object()


def human(n):
    return pamts.human(n)


# ------------------------------------------------------------------ filesystem
def containing_mount(path):
    """(mountpoint, fstype) of the filesystem `path` lives on."""
    p = os.path.abspath(path)
    while not os.path.ismount(p) and p != "/":
        p = os.path.dirname(p)
    fstype = None
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                try:
                    sep = parts.index("-")
                except ValueError:
                    continue
                if parts[4] == p:
                    fstype = parts[sep + 1]
    except OSError:
        pass
    return p, fstype


def dest_is_acceptable(path):
    """Is `path` on a filesystem we are willing to write a replica/tier to?

    This guard exists because an UNMOUNTED mountpoint is just an empty directory. A
    job that writes there fills the root filesystem, and with --remove-source-files it
    then deletes the originals. Checking "is it a mountpoint" is NOT enough on its
    own, because / is always a mountpoint -- the filesystem TYPE is what tells you the
    remote storage is actually attached.
    """
    allowed = [str(x).lower() for x in pamts.TIER["dest_fstypes"]]
    mp, fstype = containing_mount(path)
    if "*" in allowed:
        return True, mp, fstype
    return (fstype or "").lower() in allowed, mp, fstype


def scan_dir(path, views=None, pins=None):
    """Summarise a tree: size, newest mtime, in-progress marker, newest play, pins.

    atime is deliberately NOT collected -- see docs/DESIGN.md. `views` maps a fast-tier
    file path to its last-played epoch. `pins` is the set of fast-tier paths that must
    stay put; their bytes are reported separately because evicting this directory will
    not free them (they are excluded from the transfer), so budget arithmetic must not
    count them.

    Symlinks count as zero and are never followed, so a link cannot inflate a
    directory's size or drag in another tree's timestamps.

    `mtime` is the newest CONTENT file's mtime: metadata written beside the media
    ([tier] sidecar_suffixes) is ignored, because ranking falls back to mtime whenever
    there is no play record, and a nightly .nfo rewrite would otherwise make every
    never-played item look brand new. `mtime_any` keeps the unfiltered value for
    diagnostics. A directory holding only sidecars falls back to their mtime rather
    than reporting zero.
    """
    views = views or {}
    pins = pins or {}
    suffixes = tuple(pamts.TIER["inprogress_suffixes"])
    sidecars = tuple(s.lower() for s in pamts.TIER["sidecar_suffixes"])
    out = {"size": 0, "mtime": 0, "mtime_any": 0, "inprogress": False, "last_view": 0,
           "pinned_bytes": 0, "pinned_rels": []}
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if os.path.islink(fp):
                continue
            out["size"] += st.st_size
            out["mtime_any"] = max(out["mtime_any"], st.st_mtime)
            if not fn.lower().endswith(sidecars):
                out["mtime"] = max(out["mtime"], st.st_mtime)
            out["last_view"] = max(out["last_view"], views.get(fp, 0))
            if fp in pins:
                out["pinned_bytes"] += st.st_size
                out["pinned_rels"].append(os.path.relpath(fp, path))
            if fn.endswith(suffixes):
                out["inprogress"] = True
    if not out["mtime"]:
        out["mtime"] = out["mtime_any"]
    return out


def cleanup_empty_dirs(root, dry_run):
    """rsync --remove-source-files removes files but never directories, so a moved
    tree leaves empty shells behind. Left alone they accumulate and, worse, can be
    re-selected as eviction candidates forever."""
    removed = 0
    for dirpath, dirnames, files in os.walk(root, topdown=False):
        if dirpath == root or files or dirnames:
            continue
        try:
            if not dry_run:
                os.rmdir(dirpath)
            removed += 1
        except OSError:
            pass
    return removed


def run_rsync(cmd, dry_run):
    cmd = list(cmd)
    if dry_run and "--dry-run" not in cmd:
        # --dry-run belongs after the RSYNC executable, which is not always argv[0]:
        # eviction prefixes the command with `ionice -c3`. Inserting at index 1 put the
        # flag where ionice parses its own options, so ionice exited with
        # "unrecognized option '--dry-run'" and every eviction dry run failed at its
        # first item instead of reporting a plan. Backups were unaffected because their
        # argv starts with rsync, which is why this went unnoticed.
        try:
            i = next(n for n, a in enumerate(cmd)
                     if os.path.basename(str(a)) == "rsync")
        except StopIteration:
            # Do NOT fall through to a real transfer because the flag could not be
            # placed. A dry run that silently copies is far worse than one that fails.
            logging.error("cannot find rsync in %r - refusing to run it at all rather "
                          "than risk a real transfer during a dry run", cmd)
            return 1, "", "rsync not found in argv; refusing to run"
        cmd.insert(i + 1, "--dry-run")
    logging.debug("rsync: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------------- backup
def do_backup(job, dry_run):
    name = job["name"]
    src, dst = job["source"], job["dest"]
    if not src.endswith("/"):
        src += "/"
    if not dst.endswith("/"):
        dst += "/"

    if not os.path.isdir(src):
        logging.error(f"[{name}] source {src} does not exist - refusing to run. With "
                      "--delete this would erase the replica.")
        return False
    size = scan_dir(src)["size"]
    if size == 0:
        logging.error(f"[{name}] source {src} is empty - refusing to run. With "
                      "--delete this would erase the replica. Is it mounted?")
        return False

    if REQUIRE_DEST_MOUNT:
        ok, mp, fstype = dest_is_acceptable(dst)
        if not ok:
            logging.error(f"[{name}] destination {dst} sits on {mp} (fstype "
                          f"{fstype or 'unknown'}), not one of "
                          f"{pamts.TIER['dest_fstypes']} - refusing. Writing to an "
                          "unmounted mountpoint would fill the root filesystem.")
            return False

    base = ["rsync", "-a", "--delete", "--timeout=600", "--human-readable", "--stats"]
    excl = []
    ef = job.get("exclude_from")
    if ef:
        if not os.path.isfile(ef):
            logging.error(f"[{name}] exclude_from {ef} is missing - refusing to run. "
                          "Running without it would delete content the excludes protect.")
            return False
        excl.append(f"--exclude-from={ef}")
    for pattern in job.get("exclude", []):
        excl.append(f"--exclude={pattern}")

    # Pre-flight: COUNT what --delete would remove, before deleting anything.
    # rsync's own --max-delete stops *after* N deletions, so on its own it is a
    # limiter, not a circuit breaker. The probe must not inherit it, or it would abort
    # at the limit and never report the true figure.
    probe = base + excl + ["--dry-run", "--itemize-changes", src, dst]
    rc, out, err = run_rsync(probe, dry_run=False)
    if rc != 0:
        logging.error(f"[{name}] pre-flight rsync failed rc={rc}: {err.strip()[:300]}")
        return False
    deletions = sum(1 for line in out.splitlines() if line.startswith("*deleting"))
    limit = int(job["max_delete"])
    if deletions:
        logging.info(f"[{name}] {deletions} file(s) deleted from the source will be "
                     f"removed from the replica (limit {limit})")
    if deletions > limit:
        logging.error(f"[{name}] REFUSING: {deletions} deletions exceeds the limit of "
                      f"{limit}. Nothing has been deleted. If this is expected, raise "
                      "max_delete for this job; if not, check the source is fully mounted.")
        return False

    logging.info(f"[{name}] backup {human(size)} -> {dst}")
    cmd = base + excl + [f"--max-delete={limit}", src, dst]
    rc, out, err = run_rsync(cmd, dry_run)
    for line in out.splitlines():
        if any(k in line for k in ("Number of deleted files", "Number of regular files "
                                   "transferred", "Total transferred file size")):
            logging.info(f"[{name}]   {line.strip()}")
    if rc != 0:
        logging.error(f"[{name}] rsync failed rc={rc}: {err.strip()[:300]}")
        return False
    return True


# ------------------------------------------------------------------------ tier
def tier_candidates(jobs, views=None, pins=None):
    """Evictable units: one per directory at each job's configured depth."""
    out = []
    for job in jobs:
        src = job["source"]
        if not os.path.isdir(src):
            logging.warning(f"tier source {src} missing - skipping")
            continue
        depth = int(job.get("depth", 1))
        for path in pamts.dirs_at_depth(src, depth):
            d = scan_dir(path, views, pins)
            rel = os.path.relpath(path, src)
            parent = os.path.dirname(rel)
            dst_parent = os.path.join(job["dest"], parent) if parent else job["dest"]
            out.append({"path": path, "dst": dst_parent, "rel": rel,
                        "size": d["size"], "mtime": d["mtime"],
                        "inprogress": d["inprogress"], "last_view": d["last_view"],
                        "pinned_bytes": d["pinned_bytes"],
                        "pinned_rels": d["pinned_rels"],
                        "evictable": d["size"] - d["pinned_bytes"],
                        "grace_allowed": bool(job.get("grace", True))})
    return out


def budget_groups(tier_jobs, shared_gb):
    """Split tier jobs into (budget_gb, [jobs]) pools, in configuration order.

    A job that declares its own budget_gb gets a pool to itself; everything else
    shares `shared_gb`. Without this, adding music -- terabytes of albums -- to the
    same pool as movies and TV would let one medium evict the other's whole working
    set, because eviction only ever sees one combined footprint and one ceiling.
    """
    groups = collections.OrderedDict()
    for j in tier_jobs:
        groups.setdefault(j.get("budget_gb"), []).append(j)
    out = []
    for own_gb, grp in groups.items():
        out.append((float(shared_gb) if own_gb is None else float(own_gb), grp,
                    own_gb is not None))
    return out


def gather_views(jobs):
    """Collect play history once, for every budget group to share.

    This sweeps every configured player, which is the expensive part of a tiering run
    and produces a result that does not depend on which group is being evicted. It
    therefore must not be repeated per group. Returns (views, items, hist_ok), or None
    if there is no usable signal at all, in which case the caller must not evict.
    """
    players = pamts_players.build_all(pamts.PLAYERS)
    items, hist_ok = pamts_players.sweep(players)
    views = {it["fast"]: it["last_viewed"] for it in items if it.get("last_viewed")}

    # PAMTS's own record of what it has seen playing. This is what lets a player
    # with no history API (e.g. LMS) still drive ranking, and it is scan-immune by
    # construction: a scan never appears as a playing session.
    observed = pamts.observed_history()
    newer = 0
    for k, v in observed.items():
        if v > views.get(k, 0):
            views[k] = v
            newer += 1
    if observed:
        logging.info(f"[tier] observed plays: {len(observed)} record(s), "
                     f"{newer} newer than any player reported")

    if not views and not hist_ok:
        # Refuse rather than fall back to atime. Being over budget is not an
        # emergency -- nothing is lost by leaving content on fast storage for
        # another cycle -- whereas evicting on a signal that library scans corrupt
        # causes needless spin-ups later.
        logging.error("[tier] no play data from any player, and no observed plays "
                      "yet - REFUSING to evict. Ranking by atime is not an "
                      "acceptable fallback: a library scan resets it on every file "
                      "it reads.")
        return None
    if not hist_ok:
        logging.warning("[tier] no player supplied play history; ranking on "
                        "PAMTS's observed plays alone. This is expected for "
                        "session-only players, and improves as history accumulates.")
    logging.info(f"[tier] view data: {len(views)} played item(s)")
    return views, items, hist_ok


def do_tier(jobs, budget, dry_run, views=_FETCH, pins=None, items=None, hist_ok=True):
    now = time.time()
    protected = pamts.protected_now(now)
    if protected:
        logging.info(f"[tier] {len(protected)} item(s) protected by a recent promotion")

    if views is _FETCH:
        got = gather_views(jobs)
        if got is None:
            return False
        views, items, hist_ok = got

    # Pins are per group: both the depth and the headroom it is derived from are
    # relative to THIS group's budget and footprint, not the estate's.
    if pins is None and items is not None:
        pre = sum(scan_dir(j["source"], views)["size"]
                  for j in jobs if os.path.isdir(j["source"]))
        depth = pamts.pin_depth(pre, budget)
        pins = pamts.next_up(items, int(pamts.TIER["next_up_max_items"]),
                             int(pamts.TIER["next_up_max_gb"]) * pamts.GB, depth)
        logging.info(f"[tier] pinning {depth} item(s) ahead per series "
                     f"({human(max(0, budget - pre))} headroom): {len(pins)} pin(s), "
                     f"{human(sum(p['size'] or 0 for p in pins.values()))}")
    pins = pins or {}

    removed = sum(cleanup_empty_dirs(j["source"], dry_run)
                  for j in jobs if os.path.isdir(j["source"]))
    if removed:
        logging.info(f"[tier] removed {removed} empty directories (fast tier only)")

    cands = tier_candidates(jobs, views, pins)
    # True footprint, not the sum of candidates: content above the candidate depth
    # still occupies the budget even though it is not individually evictable.
    footprint = sum(scan_dir(j["source"], views, pins)["size"]
                    for j in jobs if os.path.isdir(j["source"]))
    cand_bytes = sum(c["size"] for c in cands)
    if footprint - cand_bytes > 0:
        logging.info(f"[tier] {human(footprint - cand_bytes)} sits above the candidate "
                     "depth and is not individually evictable")
    # Evict to budget MINUS the promotion reserve, not to the budget itself.
    #
    # [promote] headroom_gb is documented as "reserved so a promotion cannot push the
    # tier over budget and cause its own eviction on the next run". But eviction used to
    # stop the moment it reached the budget, which left exactly zero reserve -- so
    # promotion subtracted a reserve that was never there and refused to promote
    # anything. The reserve only existed by luck, in pools whose content happened to
    # fall under budget.
    #
    # Observed: after music was tiered, eviction took it to 798.9G of its 800G budget
    # and promotion reported "0B left" on every pass. An album could never be warmed,
    # which is the entire point of tiering music.
    reserve = int(float(pamts.PROMOTE["headroom_gb"]) * pamts.GB)
    target = max(0, budget - reserve)
    logging.info(f"[tier] fast tier holds {human(footprint)} across {len(cands)} "
                 f"candidate(s); budget {human(budget)}, evicting down to "
                 f"{human(target)} to leave {human(reserve)} for promotion")
    if footprint <= target:
        logging.info("[tier] under budget - nothing to evict, destination not touched")
        return True

    settle = int(pamts.TIER["settle_seconds"])
    grace_days = int(pamts.TIER["new_grace_days"])
    protect_days = int(pamts.TIER["promote_protect_days"])
    for c in cands:
        c["grace"] = False
        if c["size"] == 0:
            c["ok"], c["why"] = False, "empty"
        elif c["evictable"] <= 0:
            c["ok"], c["why"] = False, "every file pinned as next-to-watch"
        elif c["inprogress"]:
            c["ok"], c["why"] = False, "in-progress download marker"
        elif now - c["mtime"] < settle:
            c["ok"], c["why"] = False, f"written {int(now - c['mtime'])}s ago (settling)"
        elif pamts.is_protected(c["path"], protected):
            rec = pamts.is_protected(c["path"], protected)
            age = (now - protected[rec]) / 86400
            c["ok"], c["why"] = False, (f"promoted {age:.1f}d ago, protected for "
                                        f"{protect_days}d")
        elif (c["grace_allowed"] and not c["last_view"]
              and now - c["mtime"] < grace_days * 86400):
            # Never played, and new. Give it a chance to be watched rather than
            # sending it to slow storage first. Downgraded, not excluded.
            c["ok"], c["why"] = False, (f"added {(now - c['mtime']) / 86400:.1f}d ago "
                                        f"and never played - inside the {grace_days}d "
                                        "grace window")
            c["grace"] = True
        else:
            c["ok"], c["why"] = True, ""
    for c in cands:
        if not c["ok"]:
            logging.info(f"[tier] keeping {c['rel']}: {c['why']}")

    # THE ranking. last_view comes from the player and no scan writes it. Never-played
    # content falls back to mtime -- also scan-immune, and for never-watched content
    # "when it arrived" is the right proxy for how long it has occupied fast storage
    # for nothing.
    def staleness(c):
        return c["last_view"] if c["last_view"] else c["mtime"]

    queue = sorted([c for c in cands if c["ok"]], key=staleness)
    # Grace-protected items are a LAST resort, oldest first, so a large back-catalogue
    # cannot hold the tier over budget for the whole grace window.
    grace_queue = sorted([c for c in cands if not c["ok"] and c.get("grace")],
                         key=lambda c: c["mtime"])

    to_move, freed = [], 0
    for c in queue:
        if footprint - freed <= target:
            break
        to_move.append(c)
        freed += c["evictable"]
    if footprint - freed > target and grace_queue:
        logging.warning(f"[tier] still over budget by {human(footprint - freed - target)} "
                        f"after everything eligible - falling back to {len(grace_queue)} "
                        f"item(s) inside the {grace_days}d grace window, oldest first")
        for c in grace_queue:
            if footprint - freed <= target:
                break
            to_move.append(c)
            freed += c["evictable"]
    if not to_move:
        logging.warning(f"[tier] over budget by {human(footprint - target)} but nothing "
                        "is eligible to evict")
        return True

    logging.info(f"[tier] over budget by {human(footprint - target)}; evicting "
                 f"{len(to_move)} item(s), {human(freed)}")

    # Guard EVERY destination before a single byte moves. --remove-source-files deletes
    # the source after a "successful" copy, so writing to an unmounted mountpoint would
    # put content on the root filesystem and then remove the only other copy.
    if REQUIRE_DEST_MOUNT:
        for d in {c["dst"] for c in to_move}:
            ok, mp, fstype = dest_is_acceptable(d)
            if not ok:
                logging.error(f"[tier] {d} sits on {mp} (fstype {fstype or 'unknown'}), "
                              f"not one of {pamts.TIER['dest_fstypes']} - refusing to "
                              "evict anything.")
                return False

    ok_all = True
    for c in to_move:
        when = (f"last played {(now - c['last_view']) / 86400:.0f}d ago"
                if c["last_view"] else
                f"never played, added {(now - c['mtime']) / 86400:.0f}d ago")
        keep = (f", keeping {len(c['pinned_rels'])} pinned item(s)"
                if c["pinned_rels"] else "")
        logging.info(f"[tier]   {c['rel']} {human(c['evictable'])} ({when}){keep}")
        try:
            os.makedirs(c["dst"], exist_ok=True)
        except OSError as e:
            if e.errno != errno.EEXIST:
                logging.error(f"[tier] cannot create {c['dst']}: {e}")
                ok_all = False
                continue
        # No trailing slash on the source: rsync recreates the directory inside dst.
        # No -z: media is already compressed. NEVER --delete here.
        cmd = ["ionice", "-c3", "rsync", "-a", "--remove-source-files", "--timeout=600"]
        # Pinned items stay on the fast tier: not transferred, and because
        # --remove-source-files only removes what it transferred, not deleted either.
        # Patterns are anchored from the transfer root, which is the candidate's own
        # directory name.
        base = os.path.basename(c["path"].rstrip("/"))
        for pr in c["pinned_rels"]:
            cmd.append(f"--exclude=/{base}/{pr}")
        cmd += [c["path"], c["dst"] + "/"]
        rc, out, err = run_rsync(cmd, dry_run)
        if rc != 0:
            logging.error(f"[tier] rsync failed for {c['path']} rc={rc}\n"
                          f"{out.strip()}\n{err.strip()}")
            ok_all = False
            break
        if not dry_run:
            leftover = scan_dir(c["path"])["size"]
            if leftover and leftover != c["pinned_bytes"]:
                logging.warning(f"[tier] {c['path']} still holds {human(leftover)} "
                                f"({human(c['pinned_bytes'])} pinned) - leaving it")
                continue
            if leftover:
                logging.info(f"[tier] {c['rel']}: kept {human(leftover)} of pinned "
                             "item(s) on the fast tier")
                continue
            cleanup_empty_dirs(c["path"], False)
            try:
                os.rmdir(c["path"])
            except OSError as e:
                logging.warning(f"[tier] could not rmdir {c['path']}: {e}")
    return ok_all


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="PAMTS tiering and backup")
    ap.add_argument("--config", default=pamts.default_config_path())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=("tier", "backup"))
    ap.add_argument("--job", action="append", help="run only these named jobs")
    ap.add_argument("--budget-gb", type=float,
                    help="override the SHARED [tier] budget. Jobs that declare their "
                         "own budget_gb keep it.")
    args = ap.parse_args()

    try:
        pamts.load_and_configure(args.config)
    except pamts.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    pamts.setup_logging(pamts.PATHS["tier_log"], args.dry_run)
    budget_gb = args.budget_gb if args.budget_gb is not None else pamts.TIER["budget_gb"]

    # Scheduled work waits rather than abandoning its run: losing a race with the
    # promotion poller used to skip a whole deletion pass. A dry run takes a SHARED
    # lock, so inspecting the system while it runs is possible.
    lock = pamts.acquire_lock(pamts.PATHS["lock_file"], shared=args.dry_run,
                              wait_seconds=0 if args.dry_run else 900)
    if lock is None:
        logging.error("could not acquire the lock - exiting without doing anything")
        return 1
    # If the machine is short of memory, this job is the right thing to kill.
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass

    jobs = list(pamts.JOBS)
    if args.job:
        jobs = [j for j in jobs if j["name"] in args.job]
        if not jobs:
            logging.error(f"no job matches {args.job}")
            return 2
        logging.info(f"RESTRICTED RUN: only {', '.join(j['name'] for j in jobs)}")
    if args.only:
        jobs = [j for j in jobs if j["mode"] == args.only]

    t0 = time.time()
    logging.info("===== PAMTS maintenance starting")
    failures = 0
    tier_jobs = [j for j in jobs if j["mode"] == "tier"]
    if tier_jobs:
        gathered = gather_views(tier_jobs)
        if gathered is None:
            failures += 1
        else:
            views, items, hist_ok = gathered
            for gb, grp, is_own in budget_groups(tier_jobs, budget_gb):
                names = ", ".join(j["name"] for j in grp)
                logging.info(f"[tier] ----- group [{names}]: budget {gb:g} GB "
                             f"({'own' if is_own else 'shared'})")
                if not do_tier(grp, int(gb * pamts.GB), args.dry_run,
                               views=views, pins=None, items=items, hist_ok=hist_ok):
                    failures += 1
    for j in [j for j in jobs if j["mode"] == "backup"]:
        if not do_backup(j, args.dry_run):
            failures += 1
    logging.info(f"===== finished in {time.time() - t0:.0f}s, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
