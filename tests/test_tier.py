#!/usr/bin/env python3
"""Tests for pamts-tier.py. Synthetic trees only; touches nothing real and never
contacts a media player.

The backup tests matter most: they guard rsync --delete against wiping a replica.

Run:  python3 tests/test_tier.py
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pamts                                                    # noqa: E402

spec = importlib.util.spec_from_file_location("pamts_tier", ROOT / "pamts-tier.py")
tier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tier)

GB = 1024 ** 3
fails = []
captured = []          # every rsync argv the module builds


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


_real = tier.run_rsync


def spy(cmd, dry_run):
    captured.append(list(cmd))
    return _real(cmd, dry_run)


tier.run_rsync = spy

if shutil.which("rsync") is None:
    print("SKIP: rsync is not installed. PAMTS drives rsync for every transfer, so the\n"
          "      suite cannot verify behaviour without it. Install rsync and re-run.")
    sys.exit(77)

# Production must require a real mountpoint of an expected type; temp dirs are neither,
# so the suite turns the check off after asserting it defaults to on.
assert tier.REQUIRE_DEST_MOUNT is True, "REQUIRE_DEST_MOUNT must default to True"
tier.REQUIRE_DEST_MOUNT = False


def mkfile(p, size=1024, mtime_days=0, atime_days=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.truncate(size)
    a = time.time() - 86400 * (atime_days if atime_days is not None else mtime_days)
    os.utime(p, (a, time.time() - 86400 * mtime_days))


def fresh():
    """A clean temp root, with pamts configured to point inside it."""
    captured.clear()
    root = pathlib.Path(tempfile.mkdtemp(prefix="pamts-test-"))
    pamts.PATHS = dict(pamts.DEFAULTS["paths"])
    pamts.PATHS["lock_file"] = str(root / "lock")
    pamts.PATHS["state_file"] = str(root / "state.json")
    pamts.PATHS["tier_log"] = str(root / "tier.log")
    pamts.TIER = dict(pamts.DEFAULTS["tier"])
    pamts.PROMOTE = dict(pamts.DEFAULTS["promote"])
    pamts.setup_logging(pamts.PATHS["tier_log"], False)
    return root


def job(mode, source, dest, **kw):
    d = {"name": kw.pop("name", mode), "mode": mode,
         "source": str(source), "dest": str(dest)}
    d.update(kw)
    return d


# --------------------------------------------------------------- config validation
print("=== CONFIG: the loader refuses configurations that could destroy data")
base = {"player": {"kind": "plex", "url": "http://x:32400", "token_file": "/t"},
        "roots": [{"player_path": "/p/tv", "fast": "/f/tv", "slow": "/s/tv"}]}


def cfg_err(raw):
    try:
        pamts.configure(raw)
        return None
    except pamts.ConfigError as e:
        return str(e)


check("a backup job with no max_delete is refused",
      cfg_err({**base, "jobs": [{"name": "b", "mode": "backup",
                                 "source": "/a", "dest": "/b"}]}) is not None)
check("a tier job WITH max_delete is refused",
      cfg_err({**base, "jobs": [{"name": "t", "mode": "tier", "source": "/a",
                                 "dest": "/b", "max_delete": 5}]}) is not None)
check("an unknown mode is refused",
      cfg_err({**base, "jobs": [{"name": "x", "mode": "sync",
                                 "source": "/a", "dest": "/b"}]}) is not None)
check("duplicate job names are refused",
      cfg_err({**base, "jobs": [
          {"name": "d", "mode": "tier", "source": "/a", "dest": "/b"},
          {"name": "d", "mode": "tier", "source": "/c", "dest": "/e"}]}) is not None)
check("no roots at all is refused",
      cfg_err({"player": base["player"],
               "jobs": [{"name": "t", "mode": "tier",
                         "source": "/a", "dest": "/b"}]}) is not None)
check("a root whose fast == slow is refused",
      cfg_err({**base, "roots": [{"player_path": "/p", "fast": "/same", "slow": "/same"}],
               "jobs": [{"name": "t", "mode": "tier",
                         "source": "/a", "dest": "/b"}]}) is not None)
check("a relative root path is refused",
      cfg_err({**base, "roots": [{"player_path": "p/tv", "fast": "/f", "slow": "/s"}],
               "jobs": [{"name": "t", "mode": "tier",
                         "source": "/a", "dest": "/b"}]}) is not None)
check("no jobs at all is refused",
      cfg_err({**base, "jobs": []}) is not None)
check("a valid config is accepted",
      cfg_err({**base, "jobs": [{"name": "t", "mode": "tier", "source": "/a",
                                 "dest": "/b", "depth": 2, "grace": False}]}) is None)

# ------------------------------------------------------------------ backup guards
print("=== BACKUP: refuses an EMPTY source (would erase the replica)")
root = fresh()
src, dst = root / "src", root / "dst"
src.mkdir()
mkfile(dst / "precious.jpg", 4096)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=10), False)
check("returns failure", ok is False)
check("no rsync ran at all", not captured, f"ran {captured}")
check("replica file still present", (dst / "precious.jpg").exists())
shutil.rmtree(root)

print("=== BACKUP: refuses a source that does not exist")
root = fresh()
src, dst = root / "nope", root / "dst"
mkfile(dst / "precious.jpg", 4096)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=10), False)
check("returns failure", ok is False)
check("no rsync ran", not captured)
check("replica intact", (dst / "precious.jpg").exists())
shutil.rmtree(root)

print("=== BACKUP: refuses when a configured exclude file has gone missing")
root = fresh()
src, dst = root / "src", root / "dst"
mkfile(src / "a.bin", 2048)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=10,
                        exclude_from=str(root / "gone.lst")), False)
check("returns failure", ok is False)
check("no rsync ran", not captured)
shutil.rmtree(root)

print("=== BACKUP: max_delete stops a mass deletion and deletes NOTHING")
root = fresh()
src, dst = root / "src", root / "dst"
mkfile(src / "keep.bin", 1024)
for i in range(12):
    mkfile(dst / f"doomed{i}.bin", 1024)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=3), False)
check("returns failure (loud, not silent)", ok is False)
check("only the pre-flight dry run executed",
      all("--dry-run" in c for c in captured), f"{captured}")
check("all 12 replica files survive - nothing deleted",
      sum(1 for p in dst.iterdir() if p.name.startswith("doomed")) == 12)
shutil.rmtree(root)

print("=== BACKUP: the pre-flight probe must not inherit --max-delete")
root = fresh()
src, dst = root / "src", root / "dst"
mkfile(src / "keep.bin", 1024)
for i in range(12):
    mkfile(dst / f"doomed{i}.bin", 1024)
tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=3), False)
probe = [c for c in captured if "--dry-run" in c]
check("a probe ran", len(probe) == 1, f"{captured}")
check("probe does NOT carry --max-delete",
      not any("--max-delete" in a for a in probe[0]), f"{probe[0]}")
shutil.rmtree(root)

print("=== BACKUP: a normal run propagates deletions (the intended behaviour)")
root = fresh()
src, dst = root / "src", root / "dst"
mkfile(src / "current.bin", 1024)
mkfile(dst / "current.bin", 1024)
mkfile(dst / "removed.bin", 1024)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=10), False)
check("returns success", ok is True)
check("file deleted from source is removed from replica", not (dst / "removed.bin").exists())
check("current file retained", (dst / "current.bin").exists())
shutil.rmtree(root)

print("=== BACKUP: excluded content on the replica is PROTECTED from deletion")
root = fresh()
src, dst = root / "src", root / "dst"
mkfile(src / "a.bin", 1024)
mkfile(dst / "replica-only" / "keep.bin", 1024)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=10,
                        exclude=["replica-only/"]), False)
check("returns success", ok is True)
check("excluded replica-only tree survives --delete",
      (dst / "replica-only" / "keep.bin").exists())
shutil.rmtree(root)

print("=== BACKUP: dry run changes nothing")
root = fresh()
src, dst = root / "src", root / "dst"
mkfile(src / "a.bin", 1024)
mkfile(dst / "doomed.bin", 1024)
ok = tier.do_backup(job("backup", str(src) + "/", str(dst) + "/", max_delete=10), True)
check("returns success", ok is True)
check("replica file NOT deleted", (dst / "doomed.bin").exists())
shutil.rmtree(root)

# ---------------------------------------------------------------------- tier mode
print("=== TIER: rsync is NEVER given --delete")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Old" / "e.mkv", 4 * 1024 * 1024, mtime_days=90)
(slow / "tv").mkdir(parents=True)
jobs = [job("tier", fast / "tv", slow / "tv", depth=1)]
tier.do_tier(jobs, budget=1, dry_run=False, views={})
moves = [c for c in captured if "--remove-source-files" in c]
check("a tier rsync ran", len(moves) >= 1)
check("NO tier rsync contains --delete",
      all("--delete" not in " ".join(c) for c in moves), f"{moves}")
check("content reached slow storage", (slow / "tv" / "Old" / "e.mkv").exists())
check("emptied source shell removed", not (fast / "tv" / "Old").exists())
shutil.rmtree(root)

print("=== TIER: under budget means the destination is never touched")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "New" / "e.mkv", 1024, mtime_days=1)
(slow / "tv").mkdir(parents=True)
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=1)],
             budget=100 * GB, dry_run=False, views={})
check("no rsync ran at all", not captured, f"ran {captured}")
check("content stayed on fast storage", (fast / "tv" / "New" / "e.mkv").exists())
shutil.rmtree(root)

print("=== TIER: refuses to evict when the destination fstype is unexpected")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Old" / "e.mkv", 4 * 1024 * 1024, mtime_days=90)
(slow / "tv").mkdir(parents=True)
tier.REQUIRE_DEST_MOUNT = True
ok = tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=1)],
                  budget=1, dry_run=False, views={})
tier.REQUIRE_DEST_MOUNT = False
check("returns failure", ok is False)
check("no rsync ran", not captured)
check("content is STILL on fast storage", (fast / "tv" / "Old" / "e.mkv").exists())
check("nothing written to the fake destination", not any((slow / "tv").iterdir()))
shutil.rmtree(root)

print("=== TIER: dest_fstypes = ['*'] disables the guard (documented escape hatch)")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Old" / "e.mkv", 4 * 1024 * 1024, mtime_days=90)
(slow / "tv").mkdir(parents=True)
pamts.TIER["dest_fstypes"] = ["*"]
tier.REQUIRE_DEST_MOUNT = True
ok = tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=1)],
                  budget=1, dry_run=False, views={})
tier.REQUIRE_DEST_MOUNT = False
check("eviction proceeds with the wildcard", ok is True)
check("content moved", (slow / "tv" / "Old" / "e.mkv").exists())
shutil.rmtree(root)

# ------------------------------------------------------------------- the ranking
print("=== RANKING: last-played beats mtime (the whole point of not using atime)")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "WatchedRecently" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=200)
mkfile(fast / "tv" / "AddedRecently" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=2)
(slow / "tv").mkdir(parents=True)
views = {
    str(fast / "tv" / "WatchedRecently" / "S01" / "e.mkv"): time.time() - 86400,
    str(fast / "tv" / "AddedRecently" / "S01" / "e.mkv"): time.time() - 100 * 86400,
}
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2)],
             budget=5 * 1024 * 1024, dry_run=False, views=views)
check("the recently PLAYED item stayed, despite the oldest mtime",
      (fast / "tv" / "WatchedRecently" / "S01" / "e.mkv").exists())
check("the recently ADDED but long-unplayed item was evicted",
      (slow / "tv" / "AddedRecently" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

print("=== RANKING: a fresh atime does NOT protect anything (atime is ignored)")
root = fresh()
fast, slow = root / "fast", root / "slow"
# atime_days=0 simulates a library scan having just read it; mtime is ancient.
mkfile(fast / "tv" / "ScannedJustNow" / "S01" / "e.mkv", 4 * 1024 * 1024,
       mtime_days=300, atime_days=0)
(slow / "tv").mkdir(parents=True)
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={})
check("a just-read (scanned) file is still evicted",
      (slow / "tv" / "ScannedJustNow" / "S01" / "e.mkv").exists())
check("scan_dir does not report atime at all",
      "atime" not in tier.scan_dir(str(fast / "tv")))
shutil.rmtree(root)

print("=== SAFETY: no play data means REFUSE to evict, never fall back to atime")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Show" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=300)
(slow / "tv").mkdir(parents=True)


class DeadPlayer:
    kind = "dead"

    def __init__(self, cfg):
        pass

    def library_items(self):
        return None


_orig_build = tier.pamts_players.build
tier.pamts_players.build = lambda cfg: DeadPlayer(cfg)
ok = tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2)],
                  budget=1, dry_run=False)          # no views= -> it fetches
tier.pamts_players.build = _orig_build
check("returns failure", ok is False)
check("no rsync ran", not captured, f"ran {captured}")
check("content stayed on fast storage", (fast / "tv" / "Show" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

# ------------------------------------------------------------------ season flow
print("=== SEASON FLOW: depth 2 evicts a SEASON, not the whole series")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Show" / "Season 1" / "e01.mkv", 4 * 1024 * 1024, mtime_days=200)
mkfile(fast / "tv" / "Show" / "Season 3" / "e01.mkv", 4 * 1024 * 1024, mtime_days=200)
(slow / "tv").mkdir(parents=True)
views = {
    str(fast / "tv" / "Show" / "Season 1" / "e01.mkv"): time.time() - 90 * 86400,
    str(fast / "tv" / "Show" / "Season 3" / "e01.mkv"): time.time() - 86400,
}
j = job("tier", fast / "tv", slow / "tv", depth=2)
cands = tier.tier_candidates([j], views)
check("candidates are seasons, not the series", len(cands) == 2,
      f"{[c['rel'] for c in cands]}")
check("candidate rel paths are Show/Season N",
      sorted(c["rel"] for c in cands) == [os.path.join("Show", "Season 1"),
                                          os.path.join("Show", "Season 3")],
      f"{[c['rel'] for c in cands]}")
tier.do_tier([j], budget=5 * 1024 * 1024, dry_run=False, views=views)
check("the stale season was evicted", (slow / "tv" / "Show" / "Season 1" / "e01.mkv").exists())
check("the season being watched stayed", (fast / "tv" / "Show" / "Season 3" / "e01.mkv").exists())
check("it landed in the right series folder", (slow / "tv" / "Show" / "Season 1").is_dir())
shutil.rmtree(root)

print("=== SEASON FLOW: a series stored WITHOUT season folders is still evictable")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "FlatShow" / "e01.mkv", 4 * 1024 * 1024, mtime_days=200)
(slow / "tv").mkdir(parents=True)
cands = tier.tier_candidates([job("tier", fast / "tv", slow / "tv", depth=2)], {})
check("the series dir itself becomes the candidate",
      [c["rel"] for c in cands] == ["FlatShow"], f"{[c['rel'] for c in cands]}")
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={})
check("and it is evicted", (slow / "tv" / "FlatShow" / "e01.mkv").exists())
shutil.rmtree(root)

# ------------------------------------------------------------------------- pins
print("=== PIN: a whole-season download is swept EXCEPT the next-to-watch item")
root = fresh()
fast, slow = root / "fast", root / "slow"
for i in (1, 2, 3, 4):
    mkfile(fast / "tv" / "NewSeries" / "Season 1" / f"e{i:02d}.mkv",
           4 * 1024 * 1024, mtime_days=1)
(slow / "tv").mkdir(parents=True)
pin = str(fast / "tv" / "NewSeries" / "Season 1" / "e01.mkv")
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={}, pins={pin: {"size": 4 * 1024 * 1024}})
check("the pinned next-to-watch item STAYED on fast storage",
      (fast / "tv" / "NewSeries" / "Season 1" / "e01.mkv").exists())
check("the pinned item was NOT copied to slow storage either",
      not (slow / "tv" / "NewSeries" / "Season 1" / "e01.mkv").exists())
check("the other three went to slow storage",
      all((slow / "tv" / "NewSeries" / "Season 1" / f"e{i:02d}.mkv").exists()
          for i in (2, 3, 4)))
check("and were removed from fast storage",
      not any((fast / "tv" / "NewSeries" / "Season 1" / f"e{i:02d}.mkv").exists()
              for i in (2, 3, 4)))
shutil.rmtree(root)

print("=== PIN: a candidate whose every file is pinned is not evicted at all")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Show" / "Season 1" / "e01.mkv", 4 * 1024 * 1024, mtime_days=400)
(slow / "tv").mkdir(parents=True)
pin = str(fast / "tv" / "Show" / "Season 1" / "e01.mkv")
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={}, pins={pin: {"size": 4 * 1024 * 1024}})
check("no rsync ran for a fully-pinned candidate", not captured, f"ran {captured}")
check("content stayed", (fast / "tv" / "Show" / "Season 1" / "e01.mkv").exists())
shutil.rmtree(root)

print("=== PIN: evictable size excludes pinned bytes (budget arithmetic)")
root = fresh()
fast, slow = root / "fast", root / "slow"
for i in (1, 2):
    mkfile(fast / "tv" / "Show" / "Season 1" / f"e{i:02d}.mkv", 4 * 1024 * 1024, mtime_days=9)
(slow / "tv").mkdir(parents=True)
pin = str(fast / "tv" / "Show" / "Season 1" / "e01.mkv")
cands = tier.tier_candidates([job("tier", fast / "tv", slow / "tv", depth=2)], {},
                             {pin: {"size": 4 * 1024 * 1024}})
check("size counts both", cands[0]["size"] == 8 * 1024 * 1024, str(cands[0]["size"]))
check("evictable counts only the unpinned one",
      cands[0]["evictable"] == 4 * 1024 * 1024, str(cands[0]["evictable"]))
check("pinned file listed relative to the candidate",
      cands[0]["pinned_rels"] == ["e01.mkv"], str(cands[0]["pinned_rels"]))
shutil.rmtree(root)

# ------------------------------------------------------------------------ grace
print("=== GRACE: a new unwatched item is not evicted before it can be watched")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "movies" / "JustAdded" / "f.mkv", 4 * 1024 * 1024, mtime_days=3)
mkfile(fast / "movies" / "WatchedToday" / "f.mkv", 4 * 1024 * 1024, mtime_days=400)
(slow / "movies").mkdir(parents=True)
views = {str(fast / "movies" / "WatchedToday" / "f.mkv"): time.time() - 3600}
tier.do_tier([job("tier", fast / "movies", slow / "movies", depth=1, grace=True)],
             budget=5 * 1024 * 1024, dry_run=False, views=views)
check("the new unwatched item stayed", (fast / "movies" / "JustAdded" / "f.mkv").exists())
check("the long-ago-watched item was evicted",
      (slow / "movies" / "WatchedToday" / "f.mkv").exists())
shutil.rmtree(root)

print("=== GRACE: expires, so an old unwatched item is still evicted")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "movies" / "Stale" / "f.mkv", 4 * 1024 * 1024,
       mtime_days=int(pamts.TIER["new_grace_days"]) + 5)
(slow / "movies").mkdir(parents=True)
tier.do_tier([job("tier", fast / "movies", slow / "movies", depth=1, grace=True)],
             budget=1, dry_run=False, views={})
check("an unwatched item past the grace window is evicted",
      (slow / "movies" / "Stale" / "f.mkv").exists())
shutil.rmtree(root)

print("=== GRACE: a preference, not a guarantee - it yields when nothing else can go")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "movies" / "NewA" / "f.mkv", 4 * 1024 * 1024, mtime_days=6)
mkfile(fast / "movies" / "NewB" / "f.mkv", 4 * 1024 * 1024, mtime_days=1)
(slow / "movies").mkdir(parents=True)
tier.do_tier([job("tier", fast / "movies", slow / "movies", depth=1, grace=True)],
             budget=5 * 1024 * 1024, dry_run=False, views={})
check("the OLDEST in-grace item was evicted as a last resort",
      (slow / "movies" / "NewA" / "f.mkv").exists())
check("the newest in-grace item was kept", (fast / "movies" / "NewB" / "f.mkv").exists())
shutil.rmtree(root)

print("=== GRACE: grace=false means a fresh unwatched item IS evictable")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Fresh" / "Season 1" / "e01.mkv", 4 * 1024 * 1024, mtime_days=1)
(slow / "tv").mkdir(parents=True)
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={}, pins={})
check("evicted with grace off", (slow / "tv" / "Fresh" / "Season 1" / "e01.mkv").exists())
shutil.rmtree(root)

# ------------------------------------------------------------------- protection
print("=== PROTECTION: promoted content is not evicted straight back")
root = fresh()
fast, slow = root / "fast", root / "slow"
# The promoted series is the OLDEST, so without protection it would go first.
mkfile(fast / "tv" / "Promoted" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=100)
mkfile(fast / "tv" / "Other" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=90)
(slow / "tv").mkdir(parents=True)
import json as _json
with open(pamts.PATHS["state_file"], "w") as f:
    _json.dump({"promotions": {str(fast / "tv" / "Promoted" / "S01"):
                               {"at": time.time() - 86400}}}, f)
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=5 * 1024 * 1024, dry_run=False, views={})
check("the promoted item stayed", (fast / "tv" / "Promoted" / "S01" / "e.mkv").exists())
check("the unprotected older item went instead",
      (slow / "tv" / "Other" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

print("=== PROTECTION: expires, so promoted content is not pinned forever")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Promoted" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=100)
(slow / "tv").mkdir(parents=True)
with open(pamts.PATHS["state_file"], "w") as f:
    _json.dump({"promotions": {str(fast / "tv" / "Promoted" / "S01"): {
        "at": time.time() - (int(pamts.TIER["promote_protect_days"]) + 1) * 86400}}}, f)
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={})
check("expired protection no longer prevents eviction",
      (slow / "tv" / "Promoted" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

print("=== PROTECTION: unusable state must not break eviction")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "Show" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=90)
(slow / "tv").mkdir(parents=True)
pamts.PATHS["state_file"] = str(root / "missing.json")
check("missing state -> nothing protected", pamts.protected_now(time.time()) == {})
bad = root / "corrupt.json"
bad.write_text("{not json")
pamts.PATHS["state_file"] = str(bad)
check("corrupt state -> nothing protected", pamts.protected_now(time.time()) == {})
tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
             budget=1, dry_run=False, views={})
check("eviction still works", (slow / "tv" / "Show" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

print("=== PROTECTION matches across granularities")
check("exact match", pamts.is_protected("/f/tv/Show/S01", {"/f/tv/Show/S01": {}}) is not None)
check("record deeper than candidate still protects",
      pamts.is_protected("/f/tv/Show", {"/f/tv/Show/S01": {}}) is not None)
check("record shallower than candidate still protects",
      pamts.is_protected("/f/tv/Show/S01", {"/f/tv/Show": {}}) is not None)
check("a different series is NOT protected",
      pamts.is_protected("/f/tv/Other", {"/f/tv/Show": {}}) is None)
check("a name prefix is not treated as a path prefix",
      pamts.is_protected("/f/tv/ShowTwo", {"/f/tv/Show": {}}) is None)

# ----------------------------------------------------------------- next-up pins
print("=== NEXT-UP: which item gets pinned, and how deep")


def ep(show, season, episode, viewed=0, size=1024, added=0):
    return {"kind": "episode", "show": show, "show_key": show, "season": season,
            "episode": episode, "title": f"e{episode}",
            "rel": f"{show}/S{season}/e{episode}",
            "fast": f"/f/tv/{show}/S{season}/e{episode}",
            "slow": f"/s/tv/{show}/S{season}/e{episode}",
            "size": size, "last_viewed": viewed, "added": added}


items = [ep("A", 1, 1, viewed=1000), ep("A", 1, 2, viewed=2000),
         ep("A", 1, 3), ep("A", 1, 4), ep("A", 1, 5),
         ep("B", 1, 1, added=9000), ep("B", 1, 2, added=9000),
         ep("C", 1, 1, viewed=500)]
pins = pamts.next_up(items, 99, 10 ** 9, per_show=1)
check("A pins its first UNWATCHED item, not its first item", "/f/tv/A/S1/e3" in pins,
      str(sorted(pins)))
check("B pins e1 of a never-started season", "/f/tv/B/S1/e1" in pins, str(sorted(pins)))
check("C, fully watched, gets no pin", not any("/C/" in k for k in pins), str(sorted(pins)))
check("one pin per series at depth 1", len(pins) == 2, str(sorted(pins)))
deep = pamts.next_up(items, 99, 10 ** 9, per_show=3)
check("depth 3 pins a longer run for A",
      {"/f/tv/A/S1/e3", "/f/tv/A/S1/e4", "/f/tv/A/S1/e5"} <= set(deep), str(sorted(deep)))
check("and does not invent items B does not have", len(deep) == 5, str(sorted(deep)))

print("=== NEXT-UP: caps and round-robin fairness")
capped = pamts.next_up(items, 2, 10 ** 9, per_show=3)
check("max_items honoured", len(capped) == 2, str(sorted(capped)))
check("every series gets its FIRST unwatched item before any gets a second",
      {"/f/tv/A/S1/e3", "/f/tv/B/S1/e1"} == set(capped), str(sorted(capped)))
check("max_bytes honoured", len(pamts.next_up(items, 99, 1024, per_show=3)) == 1)

print("=== NEXT-UP: pin depth scales with headroom")
B = 400 * GB
pamts.TIER = dict(pamts.DEFAULTS["tier"])
check("empty tier pins the maximum depth",
      pamts.pin_depth(B, B) == int(pamts.TIER["pin_depth_max"]), str(pamts.pin_depth(B, B)))
check("full tier pins exactly one", pamts.pin_depth(0, B) == 1)
check("half-full is in between", 1 < pamts.pin_depth(B // 2, B) < int(pamts.TIER["pin_depth_max"]))
check("negative headroom still pins one", pamts.pin_depth(-5 * GB, B) == 1)

print()
print(f"{'ALL TESTS PASSED' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
