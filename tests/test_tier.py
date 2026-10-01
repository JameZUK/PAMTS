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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pamts                                                    # noqa: E402
from _harness import check, summary, skip                       # noqa: E402

spec = importlib.util.spec_from_file_location("pamts_tier", ROOT / "pamts-tier.py")
tier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tier)

GB = 1024 ** 3
captured = []          # every rsync argv the module builds




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

# Eviction evicts down to (budget - [promote] headroom_gb) so that promotion has room
# to work -- see the RESERVE section below. Nearly every test here uses a budget of a
# few megabytes to exercise RANKING: which item is chosen, not how many. Against the
# production default of 60 GB the target would clamp to zero and every one of them
# would evict its whole fixture, testing nothing. So the reserve is neutralised here
# and exercised explicitly, with its own value, in exactly one place.
#
# It has to be set in DEFAULTS as well as in the live dict: load_and_configure REBINDS
# pamts.PROMOTE to a fresh _merge(DEFAULTS["promote"], ...), so the config tests further
# down would otherwise restore 60 GB for every test after them -- which is exactly what
# happened, and what the "leave 60.0G for promotion" line in the output gave away.
pamts.DEFAULTS["promote"]["headroom_gb"] = 0
pamts.PROMOTE["headroom_gb"] = 0


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


# ----------------------------------------------------------------------- locking
print("=== LOCKING: a real run waits; a poller does not; dry runs coexist")
import fcntl as _f                                                      # noqa: E402
_lockroot = pathlib.Path(tempfile.mkdtemp(prefix="pamts-lock-"))
_lp = str(_lockroot / "lock")

first = pamts.acquire_lock(_lp)
check("an uncontended exclusive lock succeeds", first is not None)
check("a second exclusive attempt with no wait fails fast",
      pamts.acquire_lock(_lp, wait_seconds=0) is None)
_t0 = time.time()
check("and a short wait still fails, without hanging",
      pamts.acquire_lock(_lp, wait_seconds=3) is None)
check("it actually waited rather than returning instantly",
      2.0 < time.time() - _t0 < 12.0, f"{time.time() - _t0:.1f}s")
check("a SHARED attempt is also blocked by an exclusive holder",
      pamts.acquire_lock(_lp, shared=True, wait_seconds=0) is None)
first.close()

# Two dry runs must be able to inspect at the same time.
sh1 = pamts.acquire_lock(_lp, shared=True)
sh2 = pamts.acquire_lock(_lp, shared=True)
check("two SHARED locks coexist (two dry runs can inspect together)",
      sh1 is not None and sh2 is not None)
check("but an exclusive run is blocked while a dry run holds it",
      pamts.acquire_lock(_lp, wait_seconds=0) is None)
sh1.close()
sh2.close()
check("and succeeds once they release", pamts.acquire_lock(_lp) is not None)

# The real scenario: a holder that releases part-way through the wait.
import subprocess as _sp                                               # noqa: E402
_holder = _sp.Popen([sys.executable, "-c",
                     f"import fcntl,time;f=open({_lp!r},'w');"
                     "fcntl.flock(f,fcntl.LOCK_EX);time.sleep(4)"])
time.sleep(1.5)
_t0 = time.time()
_got = pamts.acquire_lock(_lp, wait_seconds=30)
_el = time.time() - _t0
_holder.wait()
check("a waiting run acquires the lock once the holder releases",
      _got is not None, f"waited {_el:.1f}s")
check("and it waited for it rather than failing", _el > 1.0, f"{_el:.1f}s")
if _got:
    _got.close()
shutil.rmtree(_lockroot, ignore_errors=True)

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
    """A player that is configured but cannot answer."""
    kind = name = "dead"
    provides_history = True
    provides_sessions = True

    def __init__(self, cfg=None):
        pass

    def available(self):
        return True

    def library_items(self):
        return None


_orig_all = tier.pamts_players.build_all
tier.pamts_players.build_all = lambda cfgs: [DeadPlayer()]
pamts.PATHS["state_file"] = str(root / "no-observed.json")     # no observed plays either
ok = tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2)],
                  budget=1, dry_run=False)          # no views= -> it fetches
tier.pamts_players.build_all = _orig_all
check("returns failure", ok is False)
check("no rsync ran", not captured, f"ran {captured}")
check("content stayed on fast storage", (fast / "tv" / "Show" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

print("=== MULTI-PLAYER: history is merged across players, newest play wins")
root = fresh()


class HistPlayer:
    """Reports a fixed library with play times."""
    provides_history = True
    provides_sessions = False

    def __init__(self, name, items):
        self.name = self.kind = name
        self._items = items

    def available(self):
        return True

    def library_items(self):
        return self._items


def it_(fast_p, viewed, show="Alb", ep=1, size=100):
    return {"kind": "track", "show": show, "show_key": show, "season": 1, "episode": ep,
            "title": "t", "rel": "x", "fast": fast_p, "slow": "/s/x", "size": size,
            "last_viewed": viewed, "added": 0}


a = HistPlayer("appA", [it_("/f/one", 1000), it_("/f/two", 5000)])
b = HistPlayer("appB", [it_("/f/one", 9000), it_("/f/three", 100)])
items, ok = tier.pamts_players.sweep([a, b])
byfast = {i["fast"]: i["last_viewed"] for i in items}
check("at least one player supplied history", ok is True)
check("all distinct items are present", set(byfast) == {"/f/one", "/f/two", "/f/three"},
      str(sorted(byfast)))
check("the NEWEST play across players wins", byfast["/f/one"] == 9000, str(byfast))
check("items are de-duplicated by fast path, not duplicated per player",
      len(items) == 3, str(len(items)))

print("=== MULTI-PLAYER: a player with no history API is skipped, not fatal")


class SessionOnly(HistPlayer):
    provides_history = False


items, ok = tier.pamts_players.sweep([SessionOnly("lmslike", [])])
check("no history provider means ok is False", ok is False)
check("and no items", items == [], str(items))
items, ok = tier.pamts_players.sweep([SessionOnly("lmslike", []), a])
check("a mixed set still reports ok from the provider that worked", ok is True)
check("and yields that provider's items", len(items) == 2, str(len(items)))

print("=== OBSERVED PLAYS: PAMTS's own history, and it can rank on its own")
root = fresh()
fast, slow = root / "fast", root / "slow"
mkfile(fast / "tv" / "SeenPlaying" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=200)
mkfile(fast / "tv" / "NeverSeen" / "S01" / "e.mkv", 4 * 1024 * 1024, mtime_days=200)
(slow / "tv").mkdir(parents=True)
st = {}
n = pamts.observe_plays(st, [str(fast / "tv" / "SeenPlaying" / "S01" / "e.mkv")],
                        time.time() - 3600)
check("a play is recorded", n == 1 and len(st["observed"]) == 1)
pamts.save_state({"promotions": {}, "observed": st["observed"]}, dry_run=False)
check("observed history is readable back",
      len(pamts.observed_history()) == 1, str(pamts.observed_history()))
# Only observed history exists -- no player provides any. Eviction must still work and
# must prefer the item it has never seen played.
tier.pamts_players.build_all = lambda cfgs: [SessionOnly("lmslike", [])]
ok = tier.do_tier([job("tier", fast / "tv", slow / "tv", depth=2, grace=False)],
                  budget=5 * 1024 * 1024, dry_run=False)
tier.pamts_players.build_all = _orig_all
check("eviction proceeds on observed plays alone", ok is True)
check("the item seen playing stayed",
      (fast / "tv" / "SeenPlaying" / "S01" / "e.mkv").exists())
check("the item never seen playing was evicted",
      (slow / "tv" / "NeverSeen" / "S01" / "e.mkv").exists())
shutil.rmtree(root)

print("=== OBSERVED PLAYS: bounded, and never moved backwards")
st = {"observed": {"/f/a": 5000}}
pamts.observe_plays(st, ["/f/a"], 1000)
check("an older sighting does not overwrite a newer one", st["observed"]["/f/a"] == 5000)
pamts.observe_plays(st, ["/f/a"], 9000)
check("a newer sighting does", st["observed"]["/f/a"] == 9000)
now = time.time()
st = {"observed": {"/f/new": now - 100,
                   "/f/ancient": now - (int(pamts.HISTORY["max_age_days"]) + 10) * 86400}}
check("ancient records are pruned", pamts.prune_observed(st, now) == 1)
check("recent ones are kept", list(st["observed"]) == ["/f/new"], str(st["observed"]))
pamts.HISTORY["max_entries"] = 3
st = {"observed": {f"/f/{i}": now - i for i in range(10)}}
pamts.prune_observed(st, now)
check("the entry cap is enforced", len(st["observed"]) == 3, str(len(st["observed"])))
check("and it keeps the NEWEST entries",
      set(st["observed"]) == {"/f/0", "/f/1", "/f/2"}, str(sorted(st["observed"])))
pamts.HISTORY = dict(pamts.DEFAULTS["history"])
check("observe=false disables recording",
      (pamts.HISTORY.update({"observe": False}) or
       pamts.observe_plays({}, ["/f/x"], now) == 0))
pamts.HISTORY = dict(pamts.DEFAULTS["history"])

print("=== MULTI-PLAYER CONFIG")
mp = {"roots": [{"player_path": "/p", "fast": "/f", "slow": "/s"}],
      "jobs": [{"name": "t", "mode": "tier", "source": "/a", "dest": "/b"}]}
check("[[players]] with several entries is accepted",
      cfg_err({**mp, "players": [
          {"kind": "plex", "url": "http://a", "token_file": "/t"},
          {"kind": "lms", "url": "http://b"}]}) is None)
check("and both are registered", len(pamts.PLAYERS) == 2, str(len(pamts.PLAYERS)))
check("using [player] and [[players]] together is refused",
      cfg_err({**mp, "player": {"kind": "plex", "url": "http://a", "token_file": "/t"},
               "players": [{"kind": "lms", "url": "http://b"}]}) is not None)
check("a player with no url is refused",
      cfg_err({**mp, "players": [{"kind": "lms"}]}) is not None)
check("duplicate player names are refused",
      cfg_err({**mp, "players": [{"kind": "lms", "url": "http://a"},
                                 {"kind": "lms", "url": "http://b"}]}) is not None)
check("distinct names make duplicates of one kind fine",
      cfg_err({**mp, "players": [{"name": "lms1", "kind": "lms", "url": "http://a"},
                                 {"name": "lms2", "kind": "lms", "url": "http://b"}]})
      is None)
check("singular [player] still works as shorthand",
      cfg_err({**mp, "player": {"kind": "plex", "url": "http://a",
                                "token_file": "/t"}}) is None
      and len(pamts.PLAYERS) == 1)

print("=== ADAPTERS: registry and declared capabilities")
reg = tier.pamts_players.ADAPTERS
check("plex is registered", "plex" in reg)
check("lms is registered", "lms" in reg)
check("navidrome is registered", "navidrome" in reg)
check("LMS declares NO history (its API exposes none)",
      reg["lms"].provides_history is False)
check("LMS does provide sessions", reg["lms"].provides_sessions is True)
check("LMS library_items returns [] not None (a definite 'nothing', not a failure)",
      reg["lms"]({"url": "http://x"}).library_items() == [])
check("Navidrome declares history", reg["navidrome"].provides_history is True)
check("Plex declares both", reg["plex"].provides_history is True
      and reg["plex"].provides_sessions is True)
check("navidrome without music_folder reports unavailable",
      reg["navidrome"]({"url": "http://x", "username": "u",
                        "token_file": "/nonexistent"}).available() is False)
check("LMS decodes percent-encoded file:// urls",
      reg["lms"]._path_from_url("file:///srv/fast/music/A%20B/01%20x.flac")
      == "/srv/fast/music/A B/01 x.flac")
check("LMS ignores remote streams",
      reg["lms"]._path_from_url("podcast://https://example.com/a.mp3") is None)

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
         # `added` must be RECENT: an unstarted series untouched for months is
         # deliberately not pinned now, so a 1970 sentinel would be excluded.
         ep("B", 1, 1, added=time.time()), ep("B", 1, 2, added=time.time()),
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

print("=== NEXT-UP: an unstarted series must NEVER outrank one in progress")
NOW = time.time()


def ep2(show, season, episode, viewed=0, size=1024, added=0):
    return {"kind": "episode", "show": show, "show_key": show, "season": season,
            "episode": episode, "title": f"e{episode}",
            "rel": f"{show}/S{season}/e{episode}",
            "fast": f"/f/tv/{show}/S{season}/e{episode}",
            "slow": f"/s/tv/{show}/S{season}/e{episode}",
            "size": size, "last_viewed": viewed, "added": added}


# The exact bug: "Fresh" was ADDED today and never watched; "Binge" was WATCHED
# yesterday. Ranking both on one timestamp scale put Fresh first.
mixed = [
    ep2("Binge", 1, 1, viewed=NOW - 86400, added=NOW - 400 * 86400),
    ep2("Binge", 1, 2, added=NOW - 400 * 86400),
    ep2("Fresh", 1, 1, added=NOW),
    ep2("Fresh", 1, 2, added=NOW),
]
one = pamts.next_up(mixed, max_items=1, max_bytes=10 ** 9, per_show=1, now=NOW)
check("one slot goes to the series in PROGRESS, not the one added today",
      list(one) == ["/f/tv/Binge/S1/e2"], str(list(one)))

both = pamts.next_up(mixed, max_items=9, max_bytes=10 ** 9, per_show=1, now=NOW)
check("with room for both, both are pinned",
      "/f/tv/Binge/S1/e2" in both and "/f/tv/Fresh/S1/e1" in both, str(sorted(both)))
check("each pin records which class it came from",
      both["/f/tv/Binge/S1/e2"]["unstarted"] is False
      and both["/f/tv/Fresh/S1/e1"]["unstarted"] is True,
      str({k: v["unstarted"] for k, v in both.items()}))

print("=== NEXT-UP: unstarted series are capped separately")
many_new = [ep2(f"New{i}", 1, 1, added=NOW - i) for i in range(6)]
many_new += [ep2(f"New{i}", 1, 2, added=NOW - i) for i in range(6)]
capped2 = pamts.next_up(many_new, max_items=99, max_bytes=10 ** 9,
                        per_show=1, unstarted_max=2, now=NOW)
check("unstarted_max is honoured", len(capped2) == 2, str(sorted(capped2)))
check("and every pin is flagged unstarted",
      all(v["unstarted"] for v in capped2.values()), str(capped2))
check("unstarted_max=0 pins none of them",
      pamts.next_up(many_new, max_items=99, max_bytes=10 ** 9, per_show=1,
                    unstarted_max=0, now=NOW) == {})
check("include_unstarted=False pins none of them",
      pamts.next_up(many_new, max_items=99, max_bytes=10 ** 9, per_show=1,
                    include_unstarted=False, now=NOW) == {})

print("=== NEXT-UP: a series left unwatched for months is not imminent")
stale = [ep2("Stale", 1, 1, added=NOW - 200 * 86400),
         ep2("Stale", 1, 2, added=NOW - 200 * 86400)]
check("added 200 days ago and never started -> not pinned",
      pamts.next_up(stale, max_items=9, max_bytes=10 ** 9, per_show=1,
                    unstarted_days=30, now=NOW) == {})
check("unstarted_days=0 disables the age limit",
      len(pamts.next_up(stale, max_items=9, max_bytes=10 ** 9, per_show=1,
                        unstarted_days=0, now=NOW)) == 1)

print("=== NEXT-UP: in-progress series never lose budget to unstarted ones")
budget_test = []
for i in (1, 2):
    budget_test += [ep2(f"Prog{i}", 1, 1, viewed=NOW - i * 86400),
                    ep2(f"Prog{i}", 1, 2)]
for i in (1, 2, 3):
    budget_test += [ep2(f"New{i}", 1, 1, added=NOW)]
three = pamts.next_up(budget_test, max_items=3, max_bytes=10 ** 9, per_show=1, now=NOW)
prog = [k for k, v in three.items() if not v["unstarted"]]
check("both in-progress series got their pin first", len(prog) == 2, str(sorted(three)))
check("and the remaining slot went to an unstarted one", len(three) == 3, str(sorted(three)))

print("=== NEXT-UP: round-robin still holds WITHIN the in-progress class")
rr = []
for i in (1, 2):
    rr += [ep2(f"P{i}", 1, 1, viewed=NOW - i * 86400),
           ep2(f"P{i}", 1, 2), ep2(f"P{i}", 1, 3)]
two = pamts.next_up(rr, max_items=2, max_bytes=10 ** 9, per_show=3, now=NOW)
check("two in-progress series each get their FIRST pending episode before either "
      "gets a second",
      set(two) == {"/f/tv/P1/S1/e2", "/f/tv/P2/S1/e2"}, str(sorted(two)))

print("=== NEXT-UP: caps and round-robin fairness")
capped = pamts.next_up(items, 2, 10 ** 9, per_show=3)
check("max_items honoured", len(capped) == 2, str(sorted(capped)))
# A is in progress, B has never been started. Both slots go to A -- even A's SECOND
# pending episode outranks B's first, which is the whole point of the two classes.
check("an in-progress series takes the budget ahead of an unstarted one",
      {"/f/tv/A/S1/e3", "/f/tv/A/S1/e4"} == set(capped), str(sorted(capped)))
check("max_bytes honoured", len(pamts.next_up(items, 99, 1024, per_show=3)) == 1)

print("=== NEXT-UP: pin depth scales with how full the tier is")
B = 400 * GB
pamts.TIER = dict(pamts.DEFAULTS["tier"])
# pin_depth takes the FOOTPRINT. It used to take a "headroom", and the two callers
# disagreed about which headroom -- so the signature was changed to something with one
# possible meaning.
check("an EMPTY tier pins the maximum depth",
      pamts.pin_depth(0, B) == int(pamts.TIER["pin_depth_max"]), str(pamts.pin_depth(0, B)))
check("a FULL tier pins exactly one", pamts.pin_depth(B, B) == 1, str(pamts.pin_depth(B, B)))
check("half-full is in between",
      1 < pamts.pin_depth(B // 2, B) < int(pamts.TIER["pin_depth_max"]),
      str(pamts.pin_depth(B // 2, B)))
check("over budget still pins one", pamts.pin_depth(B * 2, B) == 1,
      str(pamts.pin_depth(B * 2, B)))

# ------------------------------------------------------------------- sidecar mtime
print("=== SIDECAR mtime: a metadata rewrite must not make content look fresh")
_sroot = pathlib.Path(tempfile.mkdtemp(prefix="pamts-sidecar-"))
# An album last touched 400 days ago, whose album.nfo was rewritten minutes ago --
# exactly what Lidarr's XbmcMetadata consumer did to 6,436 directories nightly.
mkfile(_sroot / "album" / "01.flac", size=4096, mtime_days=400)
mkfile(_sroot / "album" / "album.nfo", size=300, mtime_days=0)
_d = tier.scan_dir(str(_sroot / "album"))
_age_days = (time.time() - _d["mtime"]) / 86400
check("mtime comes from the media file, not the .nfo", _age_days > 399,
      f"mtime is {_age_days:.1f} days old")
check("mtime_any still reports the unfiltered newest file",
      (time.time() - _d["mtime_any"]) / 86400 < 1,
      f"mtime_any is {(time.time() - _d['mtime_any']) / 86400:.2f} days old")
check("size still counts the sidecar", _d["size"] == 4096 + 300, str(_d["size"]))

# Artwork, subtitles and playlists are sidecars too.
for _sc in ("cover.jpg", "folder.png", "sub.srt", "list.m3u", "x.lrc"):
    mkfile(_sroot / "many" / _sc, size=10, mtime_days=0)
mkfile(_sroot / "many" / "track.mp3", size=2048, mtime_days=200)
_d = tier.scan_dir(str(_sroot / "many"))
check("several sidecar types are all ignored",
      (time.time() - _d["mtime"]) / 86400 > 199,
      f"mtime is {(time.time() - _d['mtime']) / 86400:.1f} days old")

# A directory of nothing but sidecars must not report mtime 0: that would sort it to
# the very front of the eviction queue on a fabricated timestamp.
mkfile(_sroot / "only" / "album.nfo", size=100, mtime_days=30)
_d = tier.scan_dir(str(_sroot / "only"))
check("a sidecar-only directory falls back rather than reporting zero",
      _d["mtime"] > 0 and abs(_d["mtime"] - _d["mtime_any"]) < 1,
      f"mtime={_d['mtime']} mtime_any={_d['mtime_any']}")

# And the ranking consequence: the stamped album must still be evicted before an
# album whose CONTENT is genuinely new.
mkfile(_sroot / "tier" / "Old Album" / "01.flac", size=1024 * 1024, mtime_days=400)
mkfile(_sroot / "tier" / "Old Album" / "album.nfo", size=200, mtime_days=0)
mkfile(_sroot / "tier" / "New Album" / "01.flac", size=1024 * 1024, mtime_days=1)
_slow = _sroot / "slowside"
captured.clear()
tier.do_tier([job("tier", _sroot / "tier", _slow, depth=1, grace=False)],
             budget=1024 * 1024, dry_run=False, views={})
_moved = [" ".join(c) for c in captured if "--remove-source-files" in c]
check("the .nfo-stamped old album is the one evicted",
      any("Old Album" in m for m in _moved), str(_moved))
check("the genuinely new album is kept",
      not any("New Album" in m for m in _moved), str(_moved))
shutil.rmtree(_sroot, ignore_errors=True)

print("=== CONFIG: suffix lists are validated")


def _suffix_err(toml_frag):
    d = pathlib.Path(tempfile.mkdtemp(prefix="pamts-suf-"))
    f = d / "pamts.toml"
    f.write_text('[[players]]\nname = "p"\nurl = "http://x"\ntoken_file = "/dev/null"\n'
                 '[[roots]]\nplayer_path = "/p"\nfast = "/f"\nslow = "/s"\n'
                 '[[jobs]]\nname = "t"\nmode = "tier"\nsource = "/a"\ndest = "/b"\n'
                 + toml_frag)
    try:
        pamts.load_and_configure(str(f))
        return None
    except pamts.ConfigError as e:
        return str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


e = _suffix_err('[tier]\nsidecar_suffixes = ["nfo"]\n')
check("a suffix without a leading dot is refused", e is not None and "'.'" in e, repr(e))
e = _suffix_err('[tier]\nsidecar_suffixes = ".nfo"\n')
check("a bare string instead of a list is refused",
      e is not None and "list of suffixes" in e, repr(e))
e = _suffix_err('[tier]\nsidecar_suffixes = [".nfo", ".jpg"]\n')
check("a well-formed list is accepted", e is None, repr(e))


# ------------------------------------------------------------------ dry-run plumbing
print("=== DRY RUN: --dry-run lands after rsync, not after an ionice prefix")
_seen = []


def _capture(cmd, dry_run):
    _seen.append(list(cmd))
    return 0, "", ""


_saved_run = tier.run_rsync
tier.run_rsync = _real          # the genuine implementation, not the spy
try:
    import subprocess as _sp
    _calls = []
    _saved_sp = _sp.run

    def _fake_run(cmd, **kw):
        _calls.append(list(cmd))
        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()
    _sp.run = _fake_run
    tier.run_rsync(["ionice", "-c3", "rsync", "-a", "/src", "/dst"], dry_run=True)
    got = _calls[-1]
    check("the ionice prefix survives", got[:2] == ["ionice", "-c3"], str(got))
    check("--dry-run goes immediately after rsync, where rsync will parse it",
          got[2] == "rsync" and got[3] == "--dry-run", str(got))
    tier.run_rsync(["rsync", "-a", "--delete", "/src", "/dst"], dry_run=True)
    got = _calls[-1]
    check("an unprefixed rsync still gets the flag in position 1",
          got[0] == "rsync" and got[1] == "--dry-run", str(got))
    tier.run_rsync(["ionice", "-c3", "rsync", "-a", "/s", "/d"], dry_run=False)
    check("a real run gets no --dry-run", "--dry-run" not in _calls[-1],
          str(_calls[-1]))
    tier.run_rsync(["rsync", "--dry-run", "-a", "/s", "/d"], dry_run=True)
    check("an already-present --dry-run is not duplicated",
          _calls[-1].count("--dry-run") == 1, str(_calls[-1]))
    # The dangerous case: if the flag cannot be placed, nothing must run at all.
    _before = len(_calls)
    rc, _o, err = tier.run_rsync(["ionice", "-c3", "notrsync", "/s", "/d"],
                                 dry_run=True)
    check("when rsync is not in argv the command is NOT executed",
          len(_calls) == _before, f"{len(_calls) - _before} command(s) ran")
    check("and it reports failure rather than succeeding silently", rc != 0, str(rc))
finally:
    _sp.run = _saved_sp
    tier.run_rsync = _saved_run


# -------------------------------------------------------- the promotion reserve
print("=== RESERVE: eviction leaves room for promotion, instead of filling to budget")
_rroot = pathlib.Path(tempfile.mkdtemp(prefix="pamts-reserve-"))
_rfast, _rslow = _rroot / "fast", _rroot / "slow"
# 10 items of 1 MB each, all last played long ago so all are evictable.
for i in range(10):
    mkfile(_rfast / "tv" / f"Show {i}" / "e01.mkv", size=1024 * 1024, mtime_days=400)
_rviews = {str(_rfast / "tv" / f"Show {i}" / "e01.mkv"): time.time() - 86400 * (400 - i)
           for i in range(10)}

_saved_headroom = dict(pamts.PROMOTE)
try:
    # Budget 6 MB with a 2 MB reserve: eviction must take the tier to 4 MB, not 6 MB,
    # or promotion would subtract a reserve that does not exist and refuse to run.
    pamts.PROMOTE["headroom_gb"] = 2 / 1024.0        # 2 MB, expressed in GB
    captured.clear()
    ok = tier.do_tier([job("tier", _rfast / "tv", _rslow / "tv", depth=1, grace=False)],
                      budget=6 * 1024 * 1024, dry_run=False, views=_rviews)
    check("the run succeeds", ok)
    _left = sum(1 for _ in (_rfast / "tv").iterdir())
    check("eviction stops at budget minus the reserve, not at the budget",
          _left == 4, f"{_left} item(s) left, expected 4 (6 MB budget - 2 MB reserve)")
    _moved = [c for c in captured if "--remove-source-files" in c]
    check("and it moved the other six", len(_moved) == 6, f"{len(_moved)} rsync(s)")

    # Under the reserve-adjusted target, nothing should move at all.
    for i in range(10, 13):
        mkfile(_rfast / "tv2" / f"Show {i}" / "e01.mkv", size=1024 * 1024,
               mtime_days=400)
        _rviews[str(_rfast / "tv2" / f"Show {i}" / "e01.mkv")] = time.time() - 86400
    captured.clear()
    tier.do_tier([job("tier", _rfast / "tv2", _rslow / "tv2", depth=1, grace=False)],
                 budget=6 * 1024 * 1024, dry_run=False, views=_rviews)
    check("a tier already below the reserve-adjusted target is left alone",
          not [c for c in captured if "--remove-source-files" in c],
          str([c[-2:] for c in captured]))

    # A reserve larger than the budget must not ask for a negative footprint.
    pamts.PROMOTE["headroom_gb"] = 99
    captured.clear()
    ok = tier.do_tier([job("tier", _rfast / "tv2", _rslow / "tv2", depth=1,
                           grace=False)],
                      budget=1024 * 1024, dry_run=False, views=_rviews)
    check("a reserve bigger than the budget clamps to zero rather than going negative",
          ok is True, "do_tier returned " + repr(ok))
finally:
    pamts.PROMOTE.clear()
    pamts.PROMOTE.update(_saved_headroom)
    pamts.PROMOTE["headroom_gb"] = 0      # back to neutral for anything after this
shutil.rmtree(_rroot, ignore_errors=True)


# ------------------------------------------------------------- per-job tier budget
print("=== BUDGET GROUPS: a job with its own budget_gb does not share a pool")

g = tier.budget_groups([job("tier", "/a", "/A", name="movies"),
                        job("tier", "/b", "/B", name="tv"),
                        job("tier", "/c", "/C", name="music", budget_gb=800.0)], 400)
check("three jobs collapse to two pools", len(g) == 2, f"got {len(g)}")
check("movies and tv share the global pool",
      g[0][0] == 400.0 and [j["name"] for j in g[0][1]] == ["movies", "tv"], repr(g[0]))
check("the shared pool is flagged as not-own", g[0][2] is False)
check("music gets its own 800 GB pool",
      g[1][0] == 800.0 and [j["name"] for j in g[1][1]] == ["music"], repr(g[1]))
check("the own pool is flagged as own", g[1][2] is True)

g2 = tier.budget_groups([job("tier", "/c", "/C", name="music", budget_gb=800.0)], 400)
check("a lone job with its own budget ignores the shared number",
      len(g2) == 1 and g2[0][0] == 800.0, repr(g2))
g3 = tier.budget_groups([job("tier", "/a", "/A", name="movies")], 123)
check("a job with no budget_gb takes the shared number",
      g3[0][0] == 123.0 and g3[0][2] is False, repr(g3))
g4 = tier.budget_groups([job("tier", "/a", "/A", name="x", budget_gb=50.0),
                         job("tier", "/b", "/B", name="y", budget_gb=50.0)], 400)
check("two jobs naming the same budget share one pool",
      len(g4) == 1 and len(g4[0][1]) == 2, repr(g4))

print("=== BUDGET ISOLATION: a bloated medium cannot evict a frugal one")
_broot = pathlib.Path(tempfile.mkdtemp(prefix="pamts-budget-"))
_bfast, _bslow = _broot / "fast", _broot / "slow"
# 8 MB of film, 8 MB of music, both last played long ago so both are evictable.
for i in range(8):
    mkfile(_bfast / "movies" / f"Film {i}" / "f.mkv", size=1024 * 1024, mtime_days=400)
for i in range(8):
    mkfile(_bfast / "music" / "Artist" / f"Album {i}" / "t.flac",
           size=1024 * 1024, mtime_days=400)
# views maps a fast-tier FILE path -- not a directory -- to its last-played epoch.
# Keying directories here would match nothing and quietly rank on mtime instead,
# which would still evict but would not be the test this claims to be.
_bviews = {}
for i in range(8):
    _bviews[str(_bfast / "movies" / f"Film {i}" / "f.mkv")] = time.time() - 86400 * 400
    _bviews[str(_bfast / "music" / "Artist" / f"Album {i}" / "t.flac")] = (
        time.time() - 86400 * 400)

_probe = tier.tier_candidates([_jmov_probe := job("tier", _bfast / "movies",
                                                  _bslow / "movies", name="probe",
                                                  depth=1)], _bviews)
check("the fixture's views actually register as plays, not as mtime fallback",
      _probe and all(c["last_view"] > 0 for c in _probe),
      f"last_view values: {[c['last_view'] for c in _probe]}")

_jmov = job("tier", _bfast / "movies", _bslow / "movies", name="movies", depth=1)
_jmus = job("tier", _bfast / "music", _bslow / "music", name="music", depth=2,
            budget_gb=9999.0)       # effectively unlimited: must not be touched at all
captured.clear()
for gb, grp, _own in tier.budget_groups([_jmov, _jmus], 0.004):   # ~4 MB shared pool
    tier.do_tier(grp, int(gb * GB), dry_run=False, views=_bviews)

_moved = [c for c in captured if "--remove-source-files" in c]
_moved_music = [c for c in _moved if "/music/" in " ".join(c)]
_moved_movies = [c for c in _moved if "/movies/" in " ".join(c)]
check("the over-budget movie pool evicted something", len(_moved_movies) > 0,
      f"{len(_moved_movies)} rsync(s)")
check("the under-budget music pool evicted NOTHING", len(_moved_music) == 0,
      f"music was evicted by {len(_moved_music)} rsync(s) - the pools are shared")
_music_left = sum(1 for _ in (_bfast / "music" / "Artist").iterdir())
check("all 8 albums are still on the fast tier", _music_left == 8,
      f"{_music_left} of 8 remain")
shutil.rmtree(_broot, ignore_errors=True)

print("=== CONFIG: budget_gb is tier-only and must be positive")


def _cfg_err(toml_text):
    d = pathlib.Path(tempfile.mkdtemp(prefix="pamts-cfg-"))
    f = d / "pamts.toml"
    f.write_text(toml_text)
    try:
        pamts.load_and_configure(str(f))
        return None
    except pamts.ConfigError as e:
        return str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


_base = ('[[players]]\nname = "plex"\nurl = "http://x"\ntoken_file = "/dev/null"\n'
         '[[roots]]\nplayer_path = "/p"\nfast = "/f"\nslow = "/s"\n')
e = _cfg_err(_base + '[[jobs]]\nname = "m"\nmode = "backup"\nsource = "/a"\n'
                     'dest = "/b"\nmax_delete = 10\nbudget_gb = 10\n')
check("a backup job that sets budget_gb is refused", e is not None and "budget_gb" in e,
      repr(e))
e = _cfg_err(_base + '[[jobs]]\nname = "m"\nmode = "tier"\nsource = "/a"\n'
                     'dest = "/b"\nbudget_gb = 0\n')
check("budget_gb = 0 is refused", e is not None and "greater than 0" in e, repr(e))
e = _cfg_err(_base + '[[jobs]]\nname = "m"\nmode = "tier"\nsource = "/a"\n'
                     'dest = "/b"\nbudget_gb = -5\n')
check("a negative budget_gb is refused", e is not None and "greater than 0" in e,
      repr(e))
e = _cfg_err(_base + '[[jobs]]\nname = "m"\nmode = "tier"\nsource = "/a"\n'
                     'dest = "/b"\nbudget_gb = 800\n')
check("a positive budget_gb on a tier job is accepted", e is None, repr(e))
check("and it is stored as a float on the job",
      pamts.JOBS and pamts.JOBS[0].get("budget_gb") == 800.0,
      repr(pamts.JOBS[0] if pamts.JOBS else None))


summary()
