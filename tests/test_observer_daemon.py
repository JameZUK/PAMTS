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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _harness import check, summary, skip                       # noqa: E402

import pamts_observer as obs                                    # noqa: E402
spec = importlib.util.spec_from_file_location(
    "observerd", ROOT / "observer" / "pamts-observerd.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)



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

# --- deferred demand ---------------------------------------------------------
print("\ndeferring the demand decision until a sweep is visible")

# Measured on a live server: two album tracks recorded as plays at 02:00, because
# a rolling window can only look BACKWARDS and they arrived near the start of a
# scan. That same client touched 55 distinct files within +/-120s of them.
dtmp = tempfile.mkdtemp()
dstore = d.Store(os.path.join(dtmp, "d.db"))


class _Idx:
    files = builds = 0
    last_build_s = 0.0
    devs = {45}

    def lookup(self, dev, ino, allow_rebuild=True):
        return "/m/track%d.flac" % ino


dtracker = obs.SessionTracker({"bulk_window": 120.0, "bulk_min_files": 25})
dd = d.Daemon(dstore, _Idx(), dtracker, {"bulk_window": 120.0,
                                         "bulk_min_files": 25},
              log_sessions=False, defer=True)


def dsig(ino, ts, client="10.0.0.11"):
    return {"bytes": 4 << 20, "coverage": 1.0, "duration": 2.0, "rate": 2 << 20,
            "requests": 32, "monotonic": 1.0, "method": "splice", "dev": 45,
            "ino": ino, "client": client, "t_first": ts, "t_last": ts}


dd.on_close((45, 7, "10.0.0.11"), dsig(7, 1000.0), "FETCH", True)
check("a demand decision is held, not written", dstore.history() == [] and
      len(dd._pending) == 1, str(len(dd._pending)))
check("but the session is logged immediately",
      dstore.counts()["sessions"] == 1, str(dstore.counts()["sessions"]))

# the sweep arrives AFTER it, which a backwards-only window could never see
for i in range(40):
    dtracker._note(dsig(100 + i, 1010.0 + i))
dd.flush_pending(1000.0 + 130, force=False)
check("once the sweep is visible the play is suppressed",
      dstore.history() == [], str(dstore.history()))
check("and it is counted as suppressed", dd.deferred_suppressed == 1,
      str(dd.deferred_suppressed))
check("the session row is corrected to BULK",
      sqlite3.connect(os.path.join(dtmp, "d.db")).execute(
          "SELECT label FROM sessions").fetchone()[0] == "BULK")

# an isolated play must still be written
dd2 = d.Daemon(d.Store(os.path.join(dtmp, "e.db")), _Idx(),
               obs.SessionTracker({"bulk_window": 120.0, "bulk_min_files": 25}),
               {"bulk_window": 120.0, "bulk_min_files": 25},
               log_sessions=False, defer=True)
dd2.on_close((45, 9, "10.0.0.20"), dsig(9, 2000.0, "10.0.0.20"), "PLAY", True)
dd2.flush_pending(2000.0 + 130)
check("an isolated play is written after the hold",
      len(dd2.store.history()) == 1, str(len(dd2.store.history())))
check("and counted as written", dd2.deferred_written == 1)
check("nothing is left pending", not dd2._pending)

# shutdown must judge held decisions, never drop them
dd3 = d.Daemon(d.Store(os.path.join(dtmp, "f.db")), _Idx(),
               obs.SessionTracker({"bulk_window": 120.0, "bulk_min_files": 25}),
               {"bulk_window": 120.0, "bulk_min_files": 25},
               log_sessions=False, defer=True)
dd3.on_close((45, 11, "10.0.0.20"), dsig(11, 3000.0, "10.0.0.20"), "PLAY", True)
check("still pending before shutdown", len(dd3._pending) == 1)
dd3.flush_pending(3000.0, force=True)
check("force releases held decisions at shutdown",
      len(dd3.store.history()) == 1 and not dd3._pending,
      str(len(dd3.store.history())))

# --defer off writes straight through
dd4 = d.Daemon(d.Store(os.path.join(dtmp, "g.db")), _Idx(),
               obs.SessionTracker(), {}, log_sessions=False, defer=False)
dd4.on_close((45, 13, "c"), dsig(13, 4000.0, "c"), "PLAY", True)
check("defer=False writes immediately", len(dd4.store.history()) == 1)

# non-media is never queued at all
dd5 = d.Daemon(d.Store(os.path.join(dtmp, "h.db")), type("I", (), {
    "files": 0, "builds": 0, "last_build_s": 0.0, "devs": {45},
    "lookup": lambda self, dev, ino, allow_rebuild=True: "/m/cover.jpg"})(),
    obs.SessionTracker(), {}, log_sessions=False, defer=True)
dd5.on_close((45, 15, "c"), dsig(15, 5000.0, "c"), "FETCH", True)
check("artwork is never even queued", not dd5._pending and dd5.non_media == 1)

shutil_mod = __import__("shutil")
shutil_mod.rmtree(dtmp, ignore_errors=True)

# --- clocks ------------------------------------------------------------------
print("\nclocks")
be = d.boot_epoch()
import time                                                     # noqa: E402
check("boot_epoch converts monotonic to a plausible epoch",
      abs((be + time.clock_gettime(time.CLOCK_MONOTONIC)) - time.time()) < 2.0)
check("boot_epoch is positive and not absurd", 0 < be < time.time())

import shutil                                                   # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
summary()
