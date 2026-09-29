#!/usr/bin/env python3
"""Tests for the observer daemon's own pieces: inode->path, device encoding, and
what does and does not reach play history.

The encoding test matters most. Kernel dev_t and Python's st_dev are different
layouts, and comparing them raw fails silently -- every lookup misses while the
daemon looks merely like it has a cold index.

Run:  python3 tests/test_observer_daemon.py
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "observerd", ROOT / "observer" / "pamts-observerd.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (f" -- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


# --- device encoding ---------------------------------------------------------
print("kernel dev_t encoding")
check("kdev packs (major << 20) | minor",
      d.kdev(os.makedev(8, 17)) == (8 << 20) | 17,
      f"{d.kdev(os.makedev(8, 17))}")
check("kdev differs from raw st_dev for a real major",
      d.kdev(os.makedev(8, 17)) != os.makedev(8, 17),
      "if these agree the test is not proving anything")
check("minor-only devices still round-trip", d.kdev(os.makedev(0, 29)) == 29)

# --- inode index -------------------------------------------------------------
print("\ninode -> path index")
tmp = tempfile.mkdtemp()
tree = os.path.join(tmp, "media", "tv", "Show", "Season 1")
os.makedirs(tree)
f1 = os.path.join(tree, "e01.mkv")
with open(f1, "wb") as fh:
    fh.write(b"a" * 1024)
os.makedirs(os.path.join(tmp, "media", "empty"))

idx = d.InodeIndex([os.path.join(tmp, "media")])
n = idx.build()
check("index finds the file", n == 1, f"{n}")
st = os.stat(f1)
check("lookup resolves by (kdev, ino)",
      idx.lookup(d.kdev(st.st_dev), st.st_ino) == f1)
check("devs records the device the root lives on",
      d.kdev(st.st_dev) in idx.devs, str(idx.devs))
check("an unknown inode does not resolve",
      idx.lookup(d.kdev(st.st_dev), 999999999, allow_rebuild=False) is None)
check("a foreign device does not resolve",
      idx.lookup(424242, st.st_ino, allow_rebuild=False) is None)

# a new file is picked up once the index is allowed to go stale
f2 = os.path.join(tree, "e02.mkv")
with open(f2, "wb") as fh:
    fh.write(b"b" * 1024)
st2 = os.stat(f2)
check("a new file misses a fresh index",
      idx.lookup(d.kdev(st2.st_dev), st2.st_ino, allow_rebuild=False) is None)
idx.max_age = 0.0                        # pretend it is stale
check("a new file is found after rebuild",
      idx.lookup(d.kdev(st2.st_dev), st2.st_ino) == f2)
check("rebuilding happened", idx.builds == 2, f"{idx.builds}")

check("a missing root is tolerated",
      d.InodeIndex([os.path.join(tmp, "does-not-exist")]).build() == 0)

# --- store: what reaches history --------------------------------------------
print("\nstore: only demand reaches play history")
db = os.path.join(tmp, "o.db")
store = d.Store(db)


def sig(**kw):
    base = {"bytes": 40 << 20, "coverage": 1.0, "duration": 300.0,
            "rate": 140000.0, "requests": 320, "monotonic": 1.0,
            "method": "splice", "dev": 45, "ino": 7, "client": "10.0.0.1"}
    base.update(kw)
    return base


check("non-media paths never reach history even when labelled PLAY",
      (store.record("/m/cover.jpg", sig(), "PLAY", 999.0),
       store.history() == [])[1], "artwork must not count as demand")
check("DEMAND is exactly PLAY and FETCH", d.DEMAND == {"PLAY", "FETCH"}, str(d.DEMAND))

store.record("/m/a.mkv", sig(), "PLAY", 1000.0)
store.record("/m/b.mkv", sig(), "PROBE", 1001.0)
store.record("/m/c.mkv", sig(), "COPY", 1002.0)
store.record("/m/d.mkv", sig(), "BULK", 1003.0)
store.record("/m/e.mkv", sig(), "FETCH", 1004.0)
paths = {r["path"] for r in store.history()}
check("PLAY reaches history", "/m/a.mkv" in paths)
check("FETCH reaches history", "/m/e.mkv" in paths)
check("PROBE never reaches history", "/m/b.mkv" not in paths)
check("COPY never reaches history", "/m/c.mkv" not in paths)
check("BULK never reaches history", "/m/d.mkv" not in paths)
check("every session is still logged", store.counts()["sessions"] == 6,
      str(store.counts()))

# an unresolved path is logged but cannot be history
store.record(None, sig(), "PLAY", 1005.0)
check("an unresolved PLAY adds no history row", len(store.history()) == 2,
      str(len(store.history())))
check("but its session is recorded", store.counts()["sessions"] == 7)

# checkpoint then final must count once
store.record("/m/long.mkv", sig(bytes=100 << 20), "PLAY", 2000.0, count_it=True)
store.record("/m/long.mkv", sig(bytes=400 << 20), "PLAY", 2300.0, count_it=False)
row = [r for r in store.history() if r["path"] == "/m/long.mkv"][0]
check("a checkpointed play counts exactly once", row["play_count"] == 1,
      str(row["play_count"]))
check("last_play advances to the final report", row["last_play"] == 2300.0,
      str(row["last_play"]))
con = sqlite3.connect(db)
b = con.execute("SELECT last_bytes FROM plays WHERE path='/m/long.mkv'").fetchone()[0]
check("last_bytes takes the larger of the two", b == 400 << 20, str(b))

# players read in bursts, so one viewing must not count several times
print("\n  replay gap (buffered playback)")
rs = d.Store(os.path.join(tmp, "r.db"), replay_gap=1800.0)
P = "/m/ep.mkv"
# the pattern measured on real traffic: checkpoint, close, checkpoint, close,
# checkpoint -- with an ELEVEN MINUTE silent gap in the middle while the client
# drained its buffer
for ts, first in ((1000.0, True), (1234.0, False), (1297.0, True),
                  (2368.0, False), (3044.0, True)):
    rs.record(P, sig(), "PLAY", ts, count_it=first)
row = rs.history()[0]
check("a buffered episode counts as ONE play", row["play_count"] == 1,
      str(row["play_count"]))
check("but last_play still advances to the latest burst",
      row["last_play"] == 3044.0, str(row["last_play"]))
check("a genuine rewatch later still counts",
      (rs.record(P, sig(), "PLAY", 3044.0 + 90000, count_it=True),
       rs.history()[0]["play_count"])[1] == 2,
      str(rs.history()[0]["play_count"]))
rs.record("/m/other.mkv", sig(), "PLAY", 3100.0, count_it=True)
check("other files are unaffected by the gap", len(rs.history()) == 2,
      str(len(rs.history())))
rs2 = d.Store(os.path.join(tmp, "r2.db"), replay_gap=0.0)
rs2.record(P, sig(), "PLAY", 1000.0, count_it=True)
rs2.record(P, sig(), "PLAY", 1001.0, count_it=True)
check("replay_gap=0 disables the de-duplication",
      rs2.history()[0]["play_count"] == 2, str(rs2.history()[0]["play_count"]))

# history filtering
check("since= filters history", len(store.history(since=2000.0)) == 1,
      str(len(store.history(since=2000.0))))

# pruning keeps the table bounded for an indefinite run
small = d.Store(os.path.join(tmp, "p.db"), keep_sessions=3)
for i in range(10):
    small.record(f"/m/{i}.mkv", sig(), "COPY", 3000.0 + i)
small.prune()
check("prune bounds the sessions table", small.counts()["sessions"] == 3,
      str(small.counts()["sessions"]))

# --- clocks ------------------------------------------------------------------
print("\nclocks")
be = d.boot_epoch()
import time                                                     # noqa: E402
check("boot_epoch converts monotonic to a plausible epoch",
      abs((be + time.clock_gettime(time.CLOCK_MONOTONIC)) - time.time()) < 2.0)
check("boot_epoch is positive and not absurd", 0 < be < time.time())

import shutil                                                   # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
