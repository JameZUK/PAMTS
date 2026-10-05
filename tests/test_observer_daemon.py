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
import time

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

# --- SQLite cannot hold an unsigned 64-bit inode -----------------------------
print("\nunsigned 64-bit inodes (mergerfs unions)")
BIG = 18139166551967294708          # a real union inode, ~2x SQLite's signed max
check("s64 wraps a value above the signed maximum", d.s64(BIG) < 0, str(d.s64(BIG)))
check("the mapping is a bijection", d.s64(BIG) + (1 << 64) == BIG)
check("ordinary inodes are untouched", d.s64(581121) == 581121)
check("None survives", d.s64(None) is None)
check("distinct inodes stay distinct", d.s64(BIG) != d.s64(BIG - 1))

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
# The rebuild is ASYNC: it must not run inline, because lookup() is called from
# the ingest path and a rebuild measured 87.9s under pool contention -- stalling
# there stops draining the kernel ring buffer and silently loses events. So the
# miss stays a miss until the rebuild lands.
check("a stale miss does not block the caller",
      idx.lookup(d.kdev(st2.st_dev), st2.st_ino) is None)
for _ in range(200):                     # let the background rebuild finish
    if idx.builds >= 2:
        break
    time.sleep(0.02)
check("the rebuild happened in the background", idx.builds == 2, f"{idx.builds}")
check("and the new file resolves afterwards",
      idx.lookup(d.kdev(st2.st_dev), st2.st_ino, allow_rebuild=False) == f2)
check("a rebuild already running is not started twice",
      (idx.__dict__.update({"_rebuilding": True}),
       idx.rebuild_async())[1] is False)
idx._rebuilding = False

# Blocking startup on the index means the API is down for as long as the walk takes,
# which was measured at 101.8s against a union with a cold NFS branch.
_ai = d.InodeIndex([os.path.join(tmp, "media")])
check("rebuild_async returns immediately", _ai.rebuild_async() is True)
for _ in range(300):
    if _ai.builds >= 1:
        break
    time.sleep(0.02)
check("and the index lands shortly after", _ai.builds == 1, str(_ai.builds))
check("a lookup before it lands simply misses, it does not block",
      _ai.lookup(1, 1, allow_rebuild=False) is None)

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
# A file read through a mergerfs union killed the daemon on every session before
# s64: OverflowError, "Python int too large to convert to SQLite INTEGER".
_bsig = sig(ino=18139166551967294708, dev=109)
store.record_session("/m/union.mkv", _bsig, "PLAY", 1500.0)
check("a union inode inserts without overflowing",
      store.counts()["sessions"] >= 1)

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
check("every session is still logged", store.counts()["sessions"] == 7,
      str(store.counts()))

# an unresolved path is logged but cannot be history
store.record(None, sig(), "PLAY", 1005.0)
check("an unresolved PLAY adds no history row", len(store.history()) == 2,
      str(len(store.history())))
check("but its session is recorded", store.counts()["sessions"] == 8)

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

# a row that exists must mean a play happened
print("\n  no impossible play_count")
zs = d.Store(os.path.join(tmp, "z.db"))
# The real sequence that produced count=0: a FLAC listened to for 212s was
# suppressed at its 61s checkpoint because a scan overlapped, then admitted on
# close -- so the only write carried count_it=False.
zs.record("/m/t.flac", sig(), "PLAY", 1000.0, count_it=False)
check("a close-only write still counts once",
      zs.history()[0]["play_count"] == 1, str(zs.history()[0]["play_count"]))
zs.record("/m/u.flac", sig(), "PLAY", 2000.0, count_it=True)
zs.record("/m/u.flac", sig(), "PLAY", 2100.0, count_it=False)
check("a checkpoint plus close still counts once",
      [r for r in zs.history() if r["path"] == "/m/u.flac"][0]["play_count"] == 1)
check("no row can have play_count below 1",
      all(r["play_count"] >= 1 for r in zs.history()))

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
# ------------------------------------------------------------------- tier resolution
print("=== TIER: which tier served the read, without ever touching the slow branch")
_troot = pathlib.Path(tempfile.mkdtemp(prefix="pamts-tier-"))
_fast = _troot / "cache" / "tv"
_slow = _troot / "slow" / "TV"
_union = _troot / "library" / "tv"
for _d in (_fast, _slow, _union):
    _d.mkdir(parents=True, exist_ok=True)
# A show on the fast branch, and one only on the slow branch. The union directory is
# deliberately left EMPTY: the resolver must not depend on it, because in production
# the union is a FUSE mount this test cannot create.
# Real FILES, not just directories: the resolver asks whether the file itself is on
# the fast branch, so a bare directory would (correctly) answer "cold".
(_fast / "Hot Show").mkdir()
(_fast / "Hot Show" / "e01.mkv").write_bytes(b"x")
(_slow / "Cold Show").mkdir()
(_slow / "Cold Show" / "e01.mkv").write_bytes(b"x")

tr = d.TierResolver([(str(_union), str(_fast))])
check("a file under the union present on the fast branch is hot",
      tr.tier_of(str(_union / "Hot Show" / "e01.mkv")) == "hot",
      str(tr.tier_of(str(_union / "Hot Show" / "e01.mkv"))))
check("a file under the union absent from the fast branch is cold",
      tr.tier_of(str(_union / "Cold Show" / "e01.mkv")) == "cold",
      str(tr.tier_of(str(_union / "Cold Show" / "e01.mkv"))))
check("a path read directly off the fast branch is hot with no stat at all",
      tr.tier_of(str(_fast / "Anything" / "e01.mkv")) == "hot")
check("a file on neither branch under the union reads as cold, not unknown",
      tr.tier_of(str(_union / "Nowhere" / "e01.mkv")) == "cold")
check("a path under no known root resolves to None",
      tr.tier_of("/somewhere/else/x.mkv") is None)
check("a missing path resolves to None rather than raising",
      tr.tier_of(None) is None)
# Counted on a FRESH resolver making exactly these calls. Counting on `tr` above
# would depend on how many times each check happens to invoke it -- two of them call
# it twice, once for the assertion and once for the failure detail -- so the expected
# numbers would silently drift as checks are added.
_tc = d.TierResolver([(str(_union), str(_fast))])
_tc.tier_of(str(_union / "Hot Show" / "e01.mkv"))
_tc.tier_of(str(_union / "Cold Show" / "e01.mkv"))
_tc.tier_of(None)
check("counts are tallied, one per call",
      _tc.counts == {"hot": 1, "cold": 1, "unknown": 1}, str(_tc.counts))

# The whole point: answering "cold" must never stat the slow branch, or the question
# would wake the array that the system exists to keep asleep.
_statted = []
_real_lexists = os.path.lexists
try:
    def _spy(p):
        _statted.append(p)
        return _real_lexists(p)
    # The resolver calls os.path.lexists, so that is what has to be patched -- an
    # earlier version of this test patched os.lexists and silently observed nothing,
    # which made the assertion vacuous rather than failing.
    os.path.lexists = _spy
    tr2 = d.TierResolver([(str(_union), str(_fast))])
    tr2.tier_of(str(_union / "Cold Show" / "e01.mkv"))
finally:
    os.path.lexists = _real_lexists
check("resolving a COLD read stats only the fast branch",
      _statted and all(str(_slow) not in p for p in _statted), str(_statted))
check("and it stats exactly once", len(_statted) == 1, str(_statted))

# No map configured must mean no tier, not a crash.
tr3 = d.TierResolver([])
check("with no tier map everything is None", tr3.tier_of(str(_union / "x.mkv")) is None)

print("=== TIER: the column is added to an existing database, and recorded")
_tdb = str(_troot / "obs.db")
import sqlite3 as _sq
_c = _sq.connect(_tdb)
# A database as an older daemon left it: sessions WITHOUT a tier column.
_c.executescript("""
CREATE TABLE plays (path TEXT PRIMARY KEY, last_play REAL NOT NULL,
  play_count INTEGER NOT NULL DEFAULT 0, last_label TEXT, last_bytes INTEGER,
  updated REAL NOT NULL);
CREATE TABLE sessions (ts REAL NOT NULL, path TEXT, dev INTEGER, ino INTEGER,
  client TEXT, label TEXT NOT NULL, bytes INTEGER, coverage REAL, duration REAL,
  rate REAL, requests INTEGER, monotonic REAL, method TEXT);
""")
_c.execute("INSERT INTO sessions VALUES(1,'/old.mkv',1,2,'c','PLAY',1,0.5,1,1,1,1,'s')")
_c.commit(); _c.close()

_store = d.Store(_tdb)
_cols = {r[1] for r in _sq.connect(_tdb).execute("PRAGMA table_info(sessions)")}
check("opening an old database adds the tier column", "tier" in _cols, str(sorted(_cols)))
_old = _sq.connect(_tdb).execute("SELECT tier FROM sessions WHERE path='/old.mkv'").fetchone()
check("rows written before the migration keep NULL, not a guess", _old[0] is None, str(_old))

_sig = {"dev": 1, "ino": 9, "client": "10.0.0.5", "bytes": 100, "coverage": 1.0,
        "duration": 2.0, "rate": 50.0, "requests": 3, "monotonic": 1.0,
        "method": "splice", "t_last": 0.0}
_store.record_session("/media/library/tv/Hot Show/e01.mkv", _sig, "PLAY", 1000.0, "hot")
_store.record_session("/media/library/tv/Cold Show/e01.mkv", _sig, "PLAY", 1001.0, "cold")
_rows = dict(_sq.connect(_tdb).execute(
    "SELECT path, tier FROM sessions WHERE tier IS NOT NULL"))
check("a hot session stores tier='hot'",
      _rows.get("/media/library/tv/Hot Show/e01.mkv") == "hot", str(_rows))
check("a cold session stores tier='cold'",
      _rows.get("/media/library/tv/Cold Show/e01.mkv") == "cold", str(_rows))
shutil.rmtree(_troot, ignore_errors=True)


# =====================================================================  uid / service
# The collector now reports the client's RPC credential, which is the only thing that
# separates services sharing one host. These tests cover the three places that can go
# wrong independently: the wire layout, the mapping from (client, uid) to a name, and
# the column it is stored in.

print("=== UID: the event layout the decoder assumes matches the object that emits it")
_obsdir = ROOT / "observer"
sys.path.insert(0, str(_obsdir))
import pamts_bpf as _bpf                                        # noqa: E402

# Offsets, not just size. `uid` was added into bytes that used to be tail padding, so
# the struct is 72 bytes before AND after -- a size check alone cannot tell the two
# apart, and everything after uid would shift silently if the format string were wrong.
import struct as _st                                            # noqa: E402
_off, _seen = 0, {}
for _c in ("Q", "Q", "Q", "q", "I", "I", "I", "i", "I", "16s", "B", "3x"):
    _seen[_off] = _c
    _off += _st.calcsize("<" + _c)
check("the offsets walked here cover the whole struct", _off == 72, str(_off))
check("EVENT is 72 bytes", _bpf.EVENT.size == 72, str(_bpf.EVENT.size))
check("uid sits at byte 48, after status and before addr",
      _seen.get(48) == "I", str(_seen))
check("addr still starts at byte 52", _seen.get(52) == "16s", str(_seen))
check("af still sits at byte 68", _seen.get(68) == "B", str(_seen))

# A real buffer, with every field a DIFFERENT recognisable value: if uid were read from
# the wrong offset it would come back as one of the neighbours rather than as 114, and
# a buffer of zeroes or repeated values could not show that.
_buf = _st.pack("<QQQqIIIiI16sB3x",
                1_000_000_000,            # ts
                4242,                     # ino
                65536,                    # offset
                131072,                   # len
                0x00800011,               # dev
                0xDEADBEEF,               # xid
                4,                        # kind = read_done
                0,                        # status
                114,                      # uid -- a packaged music server
                __import__("socket").inet_aton("10.0.0.11") + b"\0" * 12,
                2)                        # AF_INET
_dec = _bpf.Collector.decode(_buf)
check("decode reads the uid", _dec["uid"] == 114, str(_dec))
check("the client address is still decoded correctly after uid was inserted",
      _dec["client"] == "10.0.0.11", str(_dec))
check("and the fields before uid are untouched",
      (_dec["ino"], _dec["xid"], _dec["kind"]) == (4242, 0xDEADBEEF, "read_done"),
      str(_dec))

# The sentinel must not surface as uid 0: uid 0 is a real client here (every *arr
# container that sets no user), so reporting unknown as 0 would attribute whole
# hosts to root.
_unk = _bpf.Collector.decode(_st.pack("<QQQqIIIiI16sB3x", 1, 2, 0, 0, 0, 0, 4, 0,
                                      _bpf.UID_UNKNOWN, b"\0" * 16, 2))
check("an unknown credential decodes to None, not 0", _unk["uid"] is None, str(_unk))
check("the sentinel is not zero", _bpf.UID_UNKNOWN != 0)

# The strong version of the same check, against the compiled object's own BTF. Skipped
# rather than failed when libbpf or the object is absent, because neither is needed to
# run the rest of this suite.
_objp = _obsdir / "pamts_nfsd.bpf.o"
try:
    import ctypes as _ct
    _lib = _ct.CDLL("libbpf.so.1", use_errno=True)
except OSError:
    _lib = None
if _lib is None or not _objp.exists() or not hasattr(_lib, "btf__resolve_size"):
    skip("BTF layout assertion (needs libbpf.so.1 and a built pamts_nfsd.bpf.o)")
else:
    _col = _bpf.Collector.__new__(_bpf.Collector)
    _col.obj_path, _col.lib = str(_objp), _lib
    _col._bind()
    _col.obj = _lib.bpf_object__open_file(str(_objp).encode(), None)
    check("the built object's BTF agrees with EVENT", _col._check_abi() == 72,
          str(_col._check_abi()))
    # And prove the guard can fail. Without this the check above passes whether or
    # not _check_abi compares anything at all.
    _real_event = _bpf.EVENT
    try:
        _bpf.EVENT = _st.Struct("<QQQqIIIiI16sB7x")      # 76: deliberately wrong
        try:
            _col._check_abi()
            _caught = False
        except _bpf.BpfError:
            _caught = True
    finally:
        _bpf.EVENT = _real_event
    check("a decoder out of step with the object is refused at load", _caught,
          "a stale object would otherwise decode uid from the client address")

print("=== UID: (client, uid) -> service name")
_sr = d.ServiceResolver([("10.0.0.11", 114, "lms"),
                         ("10.0.0.11", 1000, "navidrome"),
                         ("10.0.0.11", 0, "audiomuse"),
                         (None, 1001, "get_iplayer")])
check("a client-specific pair resolves",
      _sr.name_for("10.0.0.11", 114) == "lms")
check("three services behind ONE address are separated by uid",
      (_sr.name_for("10.0.0.11", 114),
       _sr.name_for("10.0.0.11", 1000),
       _sr.name_for("10.0.0.11", 0)) == ("lms", "navidrome", "audiomuse"),
      "this is the whole reason uid was added")
check("a bare UID entry applies to any client",
      _sr.name_for("10.0.0.102", 1001) == "get_iplayer")
check("the same uid on a DIFFERENT host does not inherit another host's name",
      _sr.name_for("10.0.0.102", 1000) is None,
      "uid 1000 is navidrome on ONE host, not everywhere")
check("an unmapped pair is None, never a guess",
      _sr.name_for("10.9.9.9", 7) is None)
check("no credential at all is None", _sr.name_for("10.0.0.11", None) is None)

# A client-specific entry must win over a bare one for the same uid, or a fallback
# would quietly override the precise answer.
_sr2 = d.ServiceResolver([(None, 0, "root-somewhere"),
                          ("10.0.0.11", 0, "audiomuse")])
check("a client-specific entry beats a bare one for the same uid",
      _sr2.name_for("10.0.0.11", 0) == "audiomuse",
      str(_sr2.name_for("10.0.0.11", 0)))
check("and the bare one still covers other clients",
      _sr2.name_for("10.0.0.99", 0) == "root-somewhere")

# Counting on a FRESH resolver: the checks above call name_for a varying number of
# times, so tallying on _sr would drift as checks are added.
_sr3 = d.ServiceResolver([("1.1.1.1", 5, "known")])
_sr3.name_for("1.1.1.1", 5)
_sr3.name_for("1.1.1.1", 6)
_sr3.name_for("1.1.1.1", None)
check("mapped, unmapped and credential-less traffic are counted separately",
      _sr3.counts == {"known": 1, "unmapped:1.1.1.1:6": 1, "no-uid": 1},
      str(dict(_sr3.counts)))

check("describe() names a known service", _sr.describe("10.0.0.11", 114) == "lms")
check("describe() falls back to the raw uid so an unmapped reader is visible",
      _sr.describe("10.9.9.9", 7) == "uid:7", str(_sr.describe("10.9.9.9", 7)))
check("describe() refuses to pick when a session carried several credentials",
      _sr.describe("10.0.0.11", None, (114, 1000)) == "ambiguous(uids=114,1000)",
      str(_sr.describe("10.0.0.11", None, (114, 1000))))
check("describe() is None when there is nothing to say",
      _sr.describe("10.0.0.11", None) is None)
_empty = d.ServiceResolver()
check("with no --service given nothing resolves, and nothing raises",
      _empty.describe("10.0.0.11", 114) == "uid:114")

print("=== UID: --service spec parsing")
_errs = []
_triples = d.parse_service_specs(
    ["10.0.0.11:114=lms", "1000=navidrome", " 10.0.0.1:0 = audiomuse "],
    _errs.append)
check("a CLIENT:UID=NAME spec parses",
      ("10.0.0.11", 114, "lms") in _triples, str(_triples))
check("a bare UID=NAME spec parses with no client",
      (None, 1000, "navidrome") in _triples, str(_triples))
check("surrounding whitespace is stripped",
      ("10.0.0.1", 0, "audiomuse") in _triples, str(_triples))
check("valid specs produce no errors", _errs == [], str(_errs))

# IPv6 is why the split is on the LAST colon. Partitioning on the first would read
# "2001" as the uid and accept it silently -- a wrong mapping, not an error.
_v6 = d.parse_service_specs(["2001:db8::5:114=lms"], _errs.append)
check("an IPv6 client keeps its colons and the uid is the final field",
      _v6 == [("2001:db8::5", 114, "lms")], str(_v6))

for _bad, _why in (("no-equals-sign", "missing ="),
                   ("10.0.0.11:abc=lms", "non-integer uid"),
                   ("10.0.0.11:-5=lms", "negative uid"),
                   ("10.0.0.11:114=", "empty name")):
    _e = []
    _got = d.parse_service_specs([_bad], _e.append)
    check(f"a malformed spec is rejected ({_why})", _e and _got == [],
          f"{_bad!r} -> {_got!r} errors={_e!r}")

print("=== UID: the column is added to an existing database, and recorded")
_uroot = pathlib.Path(tempfile.mkdtemp(prefix="pamts-uid-"))
_udb = str(_uroot / "obs.db")
_c = _sq.connect(_udb)
# A database as the PREVIOUS daemon left it: sessions with tier but no uid.
_c.executescript("""
CREATE TABLE plays (path TEXT PRIMARY KEY, last_play REAL NOT NULL,
  play_count INTEGER NOT NULL DEFAULT 0, last_label TEXT, last_bytes INTEGER,
  updated REAL NOT NULL);
CREATE TABLE sessions (ts REAL NOT NULL, path TEXT, dev INTEGER, ino INTEGER,
  client TEXT, label TEXT NOT NULL, bytes INTEGER, coverage REAL, duration REAL,
  rate REAL, requests INTEGER, monotonic REAL, method TEXT, tier TEXT);
""")
_c.execute("INSERT INTO sessions VALUES(1,'/old.mkv',1,2,'c','PLAY',1,0.5,1,1,1,1,'s','hot')")
_c.commit(); _c.close()

_ustore = d.Store(_udb)
_ucols = {r[1] for r in _sq.connect(_udb).execute("PRAGMA table_info(sessions)")}
check("opening an older database adds the uid column", "uid" in _ucols,
      str(sorted(_ucols)))
check("the tier column survives the second migration", "tier" in _ucols,
      str(sorted(_ucols)))
_orow = _sq.connect(_udb).execute(
    "SELECT tier, uid FROM sessions WHERE path='/old.mkv'").fetchone()
check("rows written before this migration keep NULL rather than uid 0",
      _orow == ("hot", None), str(_orow))

_base = {"dev": 1, "ino": 9, "client": "10.0.0.11", "bytes": 100,
         "coverage": 1.0, "duration": 2.0, "rate": 50.0, "requests": 3,
         "monotonic": 1.0, "method": "splice", "t_last": 0.0}
_ustore.record_session("/media/music/a.flac", dict(_base, uid=114), "PLAY", 10.0, "hot")
_ustore.record_session("/media/music/b.flac", dict(_base, uid=0), "PLAY", 11.0, "hot")
# An ambiguous session: _sig leaves uid None when it saw more than one credential.
_ustore.record_session("/media/music/c.flac", dict(_base, uid=None), "PLAY", 12.0, "hot")
_urows = dict(_sq.connect(_udb).execute(
    "SELECT path, uid FROM sessions WHERE ts >= 10.0"))
check("a session's uid is stored", _urows.get("/media/music/a.flac") == 114,
      str(_urows))
check("uid 0 is stored as 0, not confused with absent",
      _urows.get("/media/music/b.flac") == 0, str(_urows))
check("an ambiguous session stores NULL", _urows.get("/media/music/c.flac") is None,
      str(_urows))
# A sig from an ftrace-grade source has no uid key at all; it must not raise.
_ustore.record_session("/media/music/d.flac", dict(_base), "PLAY", 13.0, "hot")
check("a sig with no uid key at all is stored as NULL",
      _sq.connect(_udb).execute(
          "SELECT uid FROM sessions WHERE path='/media/music/d.flac'"
      ).fetchone() == (None,))
shutil.rmtree(_uroot, ignore_errors=True)

print("=== UID: the ingest queue is bounded and drains in linear time")
# ring_buffer__poll hands every pending event to the callback in one go, so the queue
# is the only thing between a kernel burst and userspace. Two properties matter:
# draining must not be quadratic, and it must not grow without limit -- the unit sets
# MemoryMax=1G on the one process that must not fall over.
import collections as _co                                        # noqa: E402
_c2 = _bpf.Collector.__new__(_bpf.Collector)
_c2.max_pending = 8
_c2._queue = _co.deque(maxlen=_c2.max_pending)
_c2.queue_dropped = 0
_c2.lost = 0
check("the queue is a deque, not a list",
      isinstance(_c2._queue, _co.deque),
      "a list drained with pop(0) is O(n) per item; one observed burst held ~233,000")
check("and it has popleft, which is the O(1) drain", hasattr(_c2._queue, "popleft"))
# The bound has to be small enough to BE a bound. At 500,000 a full queue of dicts was
# worth roughly 250 MB -- a quarter of the daemon's ceiling -- so it could not prevent
# the OOM it was added for. Observed pending depth is 0 even through bursts of millions,
# because the kernel ring absorbs them and userspace drains faster than they arrive.
check("the queue bound is small enough to bound memory",
      _bpf.Collector.max_pending <= 150_000,
      f"{_bpf.Collector.max_pending} entries of ~500 B is "
      f"~{_bpf.Collector.max_pending * 500 / 1e6:.0f} MB; against a 2 GB ceiling a "
      "bound worth hundreds of megabytes is not a safety bound")
check("but comfortably above any observed pending depth",
      _bpf.Collector.max_pending >= 50_000,
      f"{_bpf.Collector.max_pending} must stay well clear of normal operation, "
      "where measured depth is 0")

# Overfill it. The oldest go, and the loss is COUNTED -- "we stopped keeping up" and
# "nothing happened" must not look the same.
# Driven through _on_sample the way libbpf drives it: a raw address and a length.
_ev = _st.pack("<QQQqIIIiI16sB3x", 1, 2, 0, 0, 0, 0, 4, 0, 114, b"\0" * 16, 2)
_bufp = _ct.create_string_buffer(_ev, len(_ev))
for _i in range(12):
    _bpf.Collector._on_sample(_c2, None, _ct.addressof(_bufp), len(_ev))
check("the queue never exceeds its bound", len(_c2._queue) == 8, str(len(_c2._queue)))
check("and the overflow is counted, not silent", _c2.queue_dropped == 4,
      f"queue_dropped={_c2.queue_dropped}, expected 12 - 8")
check("a short buffer is still rejected before decoding",
      (_bpf.Collector._on_sample(_c2, None, _ct.addressof(_bufp), 4), _c2.lost)[1] == 1,
      f"lost={_c2.lost}")

# ======================================================= per-event cost must be flat
# Two latent accumulation bugs became the dominant cost once the daemon stopped being
# OOM-killed daily -- the crash had been resetting the state they grew. Both are O(n)
# in work already done, so they are quadratic over a long run and invisible over a
# short one. These tests assert flatness, which is the only property that catches that.
print("=== COST: handling one event must not get slower the longer it runs")

_rc = _bpf.Collector.__new__(_bpf.Collector)
_ev = {"ts": 1.0, "kind": "read_done", "xid": 1, "offset": 0, "len": 4096,
       "status": None, "ino": 1, "dev": 1, "client": "10.0.0.1", "uid": 0}
_before = len(sys.path)
for _i in range(20_000):
    _rc._default_record(_ev)
check("building a Record does not grow sys.path",
      len(sys.path) - _before <= 1,
      f"sys.path grew by {len(sys.path) - _before} over 20,000 records; "
      "it used to grow by one PER RECORD, and insert(0) is O(n), so the collector "
      "slowed from 79,100 to 18,594 rec/s between 50k and 200k events")

# Timed halves rather than an absolute rate, so the check is about the SHAPE of the
# cost and does not fail on a slow machine.
_t0 = time.perf_counter()
for _i in range(40_000):
    _rc._default_record(_ev)
_first = time.perf_counter() - _t0
_t0 = time.perf_counter()
for _i in range(40_000):
    _rc._default_record(_ev)
_second = time.perf_counter() - _t0
check("and the second 40,000 records cost about the same as the first",
      _second < _first * 3 + 0.05,
      f"first={_first:.3f}s second={_second:.3f}s -- a rising curve here is the "
      "quadratic behaviour returning")

print("=== COST: the inode index must not be rebuilt in a stampede")
_ir = pathlib.Path(tempfile.mkdtemp(prefix="pamts-idx-"))
(_ir / "a").mkdir()
(_ir / "a" / "f.mkv").write_bytes(b"x")
_idx = d.InodeIndex([str(_ir)], max_age=3600.0)
_idx.build()
_walks = []
_real_build = _idx.build
_idx.build = lambda: (_walks.append(1), _real_build())[1]

# on_arrival() calls rebuild_async() directly, with no age check -- a burst of new
# files produced EIGHT full walks in 92 seconds, each statting the cold branch over
# NFS and waking a spun-down array.
for _i in range(10):
    _idx.rebuild_async()
    time.sleep(0.05)          # let each thread finish, so _rebuilding is not the guard
check("a burst of unthrottled rebuild requests produces at most one walk",
      len(_walks) <= 1, f"{len(_walks)} walks for 10 requests")
check("the floor is long enough to batch a burst",
      d.InodeIndex.min_rebuild_interval >= 60,
      f"{d.InodeIndex.min_rebuild_interval}s")
_burst = len(_walks)

# force=True must still work: lookup() applies max_age BEFORE asking, so the floor must
# never be able to veto a refresh of a genuinely stale index. Compared against the burst
# count rather than an absolute, because the floor legitimately refuses all of the burst.
_idx.rebuild_async(force=True)
for _ in range(200):
    if len(_walks) > _burst:
        break
    time.sleep(0.02)
check("force=True overrides the floor, so lookup() can refresh a stale index",
      len(_walks) == _burst + 1,
      f"{len(_walks)} walks total, {_burst} before the forced call")
shutil.rmtree(_ir, ignore_errors=True)

summary()
