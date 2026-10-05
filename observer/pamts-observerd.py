#!/usr/bin/env python3
"""PAMTS access observer daemon.

Consumes read events from a collector, groups them into per-file sessions,
classifies each one, and serves the result as a play history that PAMTS can read
like any other player.

    pamts-observerd.py --root /srv/media --source bpf
    pamts-observerd.py --root /srv/media --source ndjson < capture.ndjson

Sources:
  bpf     -- the eBPF collector (observer/pamts_bpf.py), real inodes and clients
  ndjson  -- newline-delimited JSON on stdin, for replay and testing

The classifier lives in pamts_observer.py and is shared with offline analysis, so
a capture replayed through `--source ndjson` is labelled identically to live
traffic. That is deliberate: it means the thing you calibrate is the thing that
runs.
"""
import argparse
import collections
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pamts_observer as obs                                    # noqa: E402


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------

def boot_epoch():
    """Epoch time of CLOCK_MONOTONIC zero.

    Kernel event timestamps are monotonic-since-boot; everything PAMTS stores is
    epoch. Recomputed rather than cached once because the two clocks drift, and a
    history timestamp that drifts is a file promoted or evicted on bad evidence.
    """
    return time.time() - time.clock_gettime(time.CLOCK_MONOTONIC)


# ---------------------------------------------------------------------------
# inode -> path
# ---------------------------------------------------------------------------

def s64(n):
    """Wrap an unsigned 64-bit value into SQLite's signed 64-bit INTEGER range.

    mergerfs synthesises union inodes as unsigned 64-bit hashes -- one real file
    came back as 18139166551967294708, about twice SQLite's signed maximum -- and
    inserting that raises OverflowError, killing the daemon on every session for a
    file read through a union. The mapping is a bijection, so values still
    round-trip and remain distinct; they are only ever used for diagnostics here,
    since path resolution goes through the in-memory index.
    """
    if n is None:
        return None
    n &= (1 << 64) - 1
    return n - (1 << 64) if n >= (1 << 63) else n


def kdev(st_dev):
    """Python's st_dev -> the kernel's dev_t encoding.

    The collector reports `super_block.s_dev`, a 32-bit kernel dev_t packed as
    (major << 20) | minor. Python's os.stat().st_dev uses glibc's 64-bit
    encoding, which is a DIFFERENT layout -- comparing them directly silently
    never matches, and every path lookup fails while looking like a cold index.
    """
    return (os.major(st_dev) << 20) | os.minor(st_dev)


class InodeIndex:
    """Maps (dev, ino) to a path by walking the export roots.

    The collector reports inode and device, not names -- resolving a path inside
    BPF is possible but fragile, and an index is cheap: a few seconds over a
    hundred thousand files on SSD, and it only has to be right for files that are
    actually read.

    Device comes from stat'ing each DIRECTORY rather than each file, because a
    file always sits on the same device as its parent, and that turns a hundred
    thousand stat calls into a few thousand.
    """

    def __init__(self, roots, max_age=900.0):
        self.roots = [os.path.abspath(r) for r in roots]
        self.max_age = max_age
        self._map = {}
        self._built = 0.0
        self._lock = threading.Lock()
        self.files = 0
        self.builds = 0
        self.last_build_s = 0.0
        self._rebuilding = False
        # Kernel dev_t of every device the roots actually live on. Reads on any
        # other device belong to a dataset we were not asked about, and dropping
        # them early keeps both the work and the log about the media we manage.
        self.devs = set()

    def build(self):
        t0 = time.monotonic()
        new = {}
        devs = set()
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    dev = kdev(os.stat(d).st_dev)
                    devs.add(dev)
                    entries = list(os.scandir(d))
                except OSError:
                    continue
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            new[(dev, e.inode())] = e.path
                    except OSError:
                        continue
        with self._lock:
            self._map = new
            self._built = time.monotonic()
            self.files = len(new)
            self.builds += 1
            self.devs = devs
            self.last_build_s = time.monotonic() - t0
        logging.info("indexed %d files in %.1fs across %d device(s): %s",
                     len(new), self.last_build_s, len(devs),
                     ",".join(str(d) for d in sorted(devs)))
        return len(new)

    def lookup(self, dev, ino, allow_rebuild=True):
        with self._lock:
            hit = self._map.get((dev, ino))
            age = time.monotonic() - self._built
        if hit is not None:
            return hit
        # A miss on a stale index means a new file, which is exactly the case
        # that matters for freshly downloaded media -- so rebuild, but rate
        # limited, or a scan of unknown files would rebuild continuously.
        #
        # The rebuild runs in a BACKGROUND thread. It is normally under a second,
        # but was measured at 87.9s under pool contention, and this is called from
        # the ingest path -- stalling there stops draining the kernel ring buffer
        # and silently loses events. A miss now simply stays a miss until the
        # rebuild lands.
        # force=True because this caller has ALREADY rate-limited itself, against
        # max_age. The floor inside rebuild_async exists for callers that have not --
        # see min_rebuild_interval -- and applying it here as well would mean a stale
        # index could refuse to refresh whenever max_age is the shorter of the two.
        if allow_rebuild and age > self.max_age:
            self.rebuild_async(force=True)
        return None

    #: Floor on the gap between rebuilds for callers that do NOT rate-limit themselves.
    #:
    #: lookup() applies max_age and so passes force=True. on_arrival() does not: a newly
    #: arrived file with no path IS the case a rebuild exists for, so it asked for one
    #: every time, and a burst of arrivals produced EIGHT full walks in 92 seconds. Each
    #: walk stats the cold branch over NFS and so wakes a spun-down array, which is the
    #: exact cost this whole system exists to avoid.
    #:
    #: Refusing is safe rather than lossy: lookup()'s age-based path is the backstop, so
    #: a file that arrives during the cooldown is named a few minutes later instead of
    #: immediately. on_arrival() already documents that it names a file "on the next
    #: arrival instead of blocking ingest".
    min_rebuild_interval = 300.0

    def rebuild_async(self, force=False):
        with self._lock:
            if self._rebuilding:
                return False
            since = time.monotonic() - self._built
            if not force and since < self.min_rebuild_interval:
                logging.debug("index rebuild refused: last was %.0fs ago, floor is "
                              "%.0fs", since, self.min_rebuild_interval)
                return False
            self._rebuilding = True
            self._built = time.monotonic()   # suppress a stampede of retries
        def run():
            try:
                self.build()
            except Exception:                                   # noqa: BLE001
                logging.exception("index rebuild failed")
            finally:
                with self._lock:
                    self._rebuilding = False
        threading.Thread(target=run, daemon=True).start()
        return True


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


class TierResolver:
    """Which tier served a read: "hot", "cold", or None when it cannot be told.

    Built from --tier-map UNION=HOT pairs, e.g.
        --tier-map /media/library/tv=/media/media-cache/tv

    A read arriving through a mergerfs union gives no hint of which branch answered
    it: nfsd reports the union's device, not the branch's. But the union's search
    policy is `ff` (first found) and the fast branch is first, so a file present on
    the fast branch is necessarily the one being read. One stat on the fast branch
    therefore settles it.

    Only the FAST branch is ever stat'd. Statting the slow branch to confirm a miss
    would wake the array to answer a question about a read that has already happened
    -- the exact cost this whole system exists to avoid. Absent from fast means cold.

    A path that is already under a fast branch is hot without any stat at all.
    """

    def __init__(self, tier_map=None):
        self.unions = []        # (union prefix, fast prefix), longest union first
        self.fast = []          # fast prefixes, longest first
        for union, fast in (tier_map or []):
            u = os.path.abspath(union).rstrip("/")
            f = os.path.abspath(fast).rstrip("/")
            self.unions.append((u, f))
            self.fast.append(f)
        self.unions.sort(key=lambda t: -len(t[0]))
        self.fast = sorted(set(self.fast), key=len, reverse=True)
        self.counts = {"hot": 0, "cold": 0, "unknown": 0}

    def tier_of(self, path):
        if not path:
            self.counts["unknown"] += 1
            return None
        # Read directly off the fast tier, not through a union.
        for f in self.fast:
            if path == f or path.startswith(f + "/"):
                self.counts["hot"] += 1
                return "hot"
        for union, fast in self.unions:
            if path.startswith(union + "/"):
                candidate = fast + path[len(union):]
                try:
                    hot = os.path.lexists(candidate)
                except OSError:
                    hot = False
                key = "hot" if hot else "cold"
                self.counts[key] += 1
                return key
        self.counts["unknown"] += 1
        return None


# ---------------------------------------------------------------------------
# Service attribution
# ---------------------------------------------------------------------------


class ServiceResolver:
    """Turn (client address, client uid) into a service name.

    Built from --service [CLIENT:]UID=NAME pairs, e.g.
        --service 10.0.0.11:114=lms
        --service 10.0.0.11:1000=navidrome
        --service 10.0.0.11:0=audiomuse

    WHY BOTH HALVES. The client address names a HOST, and one host commonly serves
    several services: a music server, a second music server and an analyser can all
    sit behind one address. The uid names the process that mounted and read, which
    separates exactly those -- but a uid means nothing on its own, because 1000 is
    one service on one host and somebody else entirely on the next. The pair is the
    smallest thing that is actually a service.

    A bare UID=NAME entry applies to any client, as a fallback for a uid that is
    the same service wherever it appears. Client-specific entries win.

    WHAT IT CANNOT DO. It cannot separate peers running as the same user, and
    containers that set no user all run as uid 0 -- a group of those resolves to one
    name, which is the honest answer rather than a guess between them. Separating
    them needs a different axis: a per-service local address, which svc_rqst.rq_daddr
    would give, at the cost of an address and an export per service.

    Names are NOT written to the sessions table. The uid is the fact and is stored;
    the name is an interpretation of it, and keeping it out of the history means
    correcting a wrong mapping fixes the past too, instead of leaving a wrong label
    baked into every row already written.
    """

    def __init__(self, service_map=None):
        self.by_pair = {}       # (client, uid) -> name
        self.by_uid = {}        # uid -> name, any client
        for client, uid, name in (service_map or []):
            if client:
                self.by_pair[(client, uid)] = name
            else:
                self.by_uid[uid] = name
        self.counts = collections.Counter()

    def name_for(self, client, uid):
        """-> a service name, or None when the pair is not mapped."""
        if uid is None:
            self.counts["no-uid"] += 1
            return None
        name = self.by_pair.get((client, uid))
        if name is None:
            name = self.by_uid.get(uid)
        self.counts[name or ("unmapped:%s:%s" % (client or "?", uid))] += 1
        return name

    def describe(self, client, uid, uids=()):
        """A label for logs and the API: the service name when known, else the
        raw pair, so an unmapped reader is visible rather than silently blank."""
        if uids and len(uids) > 1:
            return "ambiguous(uids=%s)" % ",".join(str(u) for u in uids)
        name = self.name_for(client, uid)
        if name:
            return name
        if uid is None:
            return None
        return "uid:%d" % uid


def parse_service_specs(specs, fail):
    """["[CLIENT:]UID=NAME", ...] -> [(client|None, uid, name), ...].

    `fail` is called with a message for anything malformed; main passes
    argparse's ap.error, tests pass something that raises. Separate from main so
    it can be tested at all -- the colon handling is the kind of thing that works
    for IPv4 and quietly mangles IPv6.
    """
    out = []
    for spec in specs or ():
        if "=" not in spec:
            fail("--service wants [CLIENT:]UID=NAME, got %r" % spec)
            continue
        who, name = spec.split("=", 1)
        name = name.strip()
        if not name:
            fail("--service needs a non-empty NAME, got %r" % spec)
            continue
        # Split on the LAST colon: an IPv6 client address is full of them and the
        # uid is always the final field, so partitioning on the first would take
        # the leading hextet as the uid.
        client, sep, uid_s = who.rpartition(":")
        if not sep:
            client, uid_s = "", who
        try:
            uid = int(uid_s)
        except ValueError:
            fail("--service UID must be an integer, got %r in %r" % (uid_s, spec))
            continue
        if uid < 0:
            fail("--service UID must not be negative, got %r" % spec)
            continue
        out.append((client.strip() or None, uid, name))
    return out


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    path       TEXT PRIMARY KEY,
    last_play  REAL NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_label TEXT,
    last_bytes INTEGER,
    updated    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plays_last_play ON plays(last_play);
CREATE TABLE IF NOT EXISTS sessions (
    ts       REAL NOT NULL,
    path     TEXT,
    dev      INTEGER, ino INTEGER, client TEXT,
    label    TEXT NOT NULL,
    bytes    INTEGER, coverage REAL, duration REAL, rate REAL,
    requests INTEGER, monotonic REAL, method TEXT,
    tier     TEXT,
    uid      INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_ts ON sessions(ts);
"""

# Only these advance play history. FETCH is deliberately included: something read
# the whole file, which is demand, and the bulk guard has already stripped sweeps
# out by relabelling them BULK. PROBE and COPY must never count -- that is the
# founding constraint of the whole system.
DEMAND = {"PLAY", "FETCH"}


class Store:
    def __init__(self, path, keep_sessions=200_000, replay_gap=1800.0):
        self.path = path
        self.keep = keep_sessions
        # A player does not read steadily while you watch: it reads far ahead,
        # goes quiet while the buffer drains, then refills in a burst. Observed
        # on real traffic, one 2160p episode produced gaps of ELEVEN MINUTES,
        # splitting a single viewing into several sessions.
        #
        # Widening idle_gap enough to absorb that would make a pause
        # indistinguishable from starting the next episode, so instead the same
        # file seen again within replay_gap is treated as the same viewing:
        # last_play advances, play_count does not.
        self.replay_gap = replay_gap
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
            # so an older database keeps its old shape. Add the column rather than
            # recreating the table: the history is the point of this database.
            have = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
            if "tier" not in have:
                c.execute("ALTER TABLE sessions ADD COLUMN tier TEXT")
                logging.info("sessions: added the 'tier' column "
                             "(existing rows keep NULL -- their tier was not recorded)")
            if "uid" not in have:
                c.execute("ALTER TABLE sessions ADD COLUMN uid INTEGER")
                logging.info("sessions: added the 'uid' column (existing rows keep "
                             "NULL -- the collector did not report a credential yet)")

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30.0)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    def record_session(self, path, sig, label, epoch_ts, tier=None):
        """Log the session. Always happens, immediately, whatever the verdict.

        `tier` is recorded AT ACCESS TIME on purpose. Which tier served a read is a
        property of the moment, not of the file: tonight's eviction will move
        thousands of albums, and deriving the tier later would then report where the
        file is now rather than where it was read from.
        """
        c = self._conn()
        with c:
            cur = c.execute(
                "INSERT INTO sessions(ts,path,dev,ino,client,label,bytes,coverage,"
                "duration,rate,requests,monotonic,method,tier,uid) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (epoch_ts, path, s64(sig.get("dev")), s64(sig.get("ino")),
                 sig.get("client"), label, sig["bytes"], sig["coverage"],
                 sig["duration"], sig["rate"], sig["requests"], sig["monotonic"],
                 sig["method"], tier, sig.get("uid")))
            return cur.lastrowid

    def relabel_session(self, rowid, label):
        """Correct a session's label once a later verdict overrides it, so the
        log never disagrees with what actually reached play history."""
        if rowid is None:
            return
        c = self._conn()
        with c:
            c.execute("UPDATE sessions SET label = ? WHERE rowid = ?", (label, rowid))

    def record_play(self, path, sig, label, epoch_ts, count_it=True):
        """Advance play history. Separated from session logging so the decision
        can be deferred until a sweep would have become visible."""
        c = self._conn()
        with c:
            # The media check lives HERE rather than in the caller, so no code
            # path can record artwork as demand by forgetting to filter. One live
            # scan put 1,350 .jpg files into play history exactly that way.
            if path and label in DEMAND and obs.is_media(path):
                # A session reported mid-flight and then again on close must not
                # count as two plays, so the increment is carried by count_it.
                inc = 1 if count_it else 0
                if inc:
                    prev = c.execute(
                        "SELECT last_play FROM plays WHERE path = ?",
                        (path,)).fetchone()
                    if prev and (epoch_ts - prev[0]) < self.replay_gap:
                        inc = 0          # same viewing, resumed after a buffer gap
                # An INSERT means this is the first time the file has reached play
                # history, so it counts -- whatever count_it said. Otherwise a session
                # whose mid-flight report was suppressed but whose final report was
                # admitted creates a row with play_count = 0, which is not a state that
                # can mean anything. Observed on a real run: a FLAC listened to for
                # 212s was suppressed at its 61s checkpoint because a scan overlapped,
                # then admitted on close, and landed as count 0.
                c.execute(
                    "INSERT INTO plays(path,last_play,play_count,last_label,last_bytes,updated) "
                    "VALUES(?,?,MAX(?,1),?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "  last_play=MAX(last_play,excluded.last_play), "
                    "  play_count=play_count+?, last_label=excluded.last_label, "
                    "  last_bytes=MAX(last_bytes,excluded.last_bytes), "
                    "  updated=excluded.updated",
                    (path, epoch_ts, inc, label, sig["bytes"],
                     time.time(), inc))

    def record(self, path, sig, label, epoch_ts, count_it=True):
        """Log the session and advance history in one step, with no deferral.
        The daemon uses the split pair instead; this stays for simple callers."""
        self.record_session(path, sig, label, epoch_ts)
        self.record_play(path, sig, label, epoch_ts, count_it)

    def history(self, since=0.0, limit=100_000):
        c = self._conn()
        rows = c.execute(
            "SELECT path,last_play,play_count,last_label FROM plays "
            "WHERE last_play > ? ORDER BY last_play DESC LIMIT ?",
            (since, limit)).fetchall()
        return [{"path": p, "last_play": lp, "play_count": pc, "label": lb}
                for p, lp, pc, lb in rows]

    def prune(self):
        c = self._conn()
        with c:
            c.execute("DELETE FROM sessions WHERE rowid NOT IN "
                      "(SELECT rowid FROM sessions ORDER BY ts DESC LIMIT ?)",
                      (self.keep,))

    def counts(self):
        c = self._conn()
        return {
            "plays": c.execute("SELECT COUNT(*) FROM plays").fetchone()[0],
            "sessions": c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, store, index, tracker, cfg, log_sessions=True,
                 defer=True, collector=None, tiers=None, services=None):
        self.store, self.index, self.tracker, self.cfg = store, index, tracker, cfg
        self.tiers = tiers
        self.services = services or ServiceResolver()
        self.log_sessions = log_sessions
        self.collector = collector
        # A rolling window can only look backwards, so the first files of a sweep
        # have nothing to be compared against and record as demand -- two tracks
        # of an album leaked into history that way. Holding the decision until the
        # window has passed lets the sweep be seen from both sides.
        #
        # This costs nothing that matters: promotion reads /sessions for what is
        # playing NOW, and only /history lags, by bulk_window.
        self.defer = defer
        self._pending = []
        self._plock = threading.Lock()
        self.deferred_suppressed = 0
        self.deferred_written = 0
        self.started = time.time()
        self.records = 0
        self.unresolved = 0
        self.foreign = 0
        self.non_media = 0
        self.arrivals = 0
        self.bytes_in = 0
        self.labels = {}

    def on_close(self, key, sig, label, first=True):
        if first:
            self.labels[label] = self.labels.get(label, 0) + 1
        path = None
        if sig.get("ino") is not None:
            path = self.index.lookup(sig["dev"], sig["ino"])
            if path is None:
                self.unresolved += 1
        epoch = boot_epoch() + sig["t_last"]
        # Artwork, playlists and sidecars are not tierable. They are read
        # constantly by scanning servers and, being tiny, a whole-file read of one
        # is indistinguishable from a FETCH -- one scan put 1,350 .jpg files into
        # play history. The session is still logged, so nothing is hidden.
        if path is not None and not obs.is_media(path):
            self.non_media += 1
        tier = self.tiers.tier_of(path) if self.tiers else None
        rowid = self.store.record_session(path, sig, label, epoch, tier)

        is_demand = bool(path) and label in DEMAND and obs.is_media(path)
        if not is_demand:
            return
        if not self.defer:
            self.store.record_play(path, sig, label, epoch, count_it=first)
            self.deferred_written += 1
            return
        with self._plock:
            self._pending.append({
                "path": path, "sig": sig, "label": label, "epoch": epoch,
                "first": first, "rowid": rowid,
                "client": sig.get("client"), "ts": sig["t_last"],
            })
        if self.log_sessions:
            cov = "?" if sig["coverage"] is None else f"{sig['coverage']:.2f}"
            rate = "-" if not sig["rate"] else f"{sig['rate'] / (1 << 20):.1f}MB/s"
            who = self.services.describe(sig.get("client"), sig.get("uid"),
                                         sig.get("uids") or ())
            logging.info(
                "%-6s %-15s %-12s %8.1fMB cov=%-5s %5.0fs %-9s reqs=%-5d %s%s",
                label, sig.get("client") or "?", who or "-",
                sig["bytes"] / (1 << 20), cov,
                sig["duration"], rate, sig["requests"],
                path or f"<unresolved dev={sig['dev']} ino={sig['ino']}>",
                "" if first else "  (refresh)")

    def on_arrival(self, key, w):
        """A file finished being written. This is the only signal that content
        ARRIVED; before write tracking, a download was invisible here and "when
        did this land" had to be inferred from mtime.
        """
        dev, ino = key
        path = self.index.lookup(dev, ino)
        if path is None:
            # A brand new file is exactly what the index does not know about yet.
            # The rebuild is asynchronous, so name it on the next arrival instead
            # of blocking ingest for it.
            self.index.rebuild_async()
        self.arrivals += 1
        self.bytes_in += w.get("bytes", 0)
        if self.log_sessions:
            who = self.services.describe(w.get("client"), w.get("uid"))
            logging.info("ARRIVE %-15s %-12s %8.1fMB in %4.0fs  %s",
                         w.get("client") or "?", who or "-",
                         w.get("bytes", 0) / (1 << 20),
                         w.get("last", 0) - w.get("first", 0),
                         path or f"<new, dev={dev} ino={ino}>")

    def flush_pending(self, now_mono, force=False):
        """Release held demand decisions once they can be judged fairly.

        Re-counts the client's distinct files SYMMETRICALLY around the session, so
        a sweep is recognised whether the session fell at its start, middle or end.
        """
        window = self.cfg.get("bulk_window", obs.DEFAULTS["bulk_window"])
        minf = self.cfg.get("bulk_min_files", obs.DEFAULTS["bulk_min_files"])
        ready, keep = [], []
        with self._plock:
            for e in self._pending:
                (ready if force or now_mono - e["ts"] >= window else keep).append(e)
            self._pending = keep
        for e in ready:
            n = self.tracker.files_in_window(e["client"], e["ts"], window)
            if n >= minf:
                self.deferred_suppressed += 1
                # keep the log honest about what actually happened
                self.store.relabel_session(e["rowid"], "BULK")
                if self.log_sessions:
                    logging.info("BULK   %-15s suppressed on review (%d files in "
                                 "window) %s", e["client"] or "?", n, e["path"])
                continue
            self.store.record_play(e["path"], e["sig"], e["label"], e["epoch"],
                                   count_it=e["first"])
            self.deferred_written += 1

    def stats(self):
        drops = self.collector.counters() if self.collector else {}
        return {
            "kernel_emitted": drops.get("emitted"),
            "kernel_dropped": drops.get("dropped"),
            # Backpressure in USERSPACE, which the kernel counters cannot show. A
            # non-zero userspace_dropped means sessionising fell behind the ring.
            "userspace_pending": (len(self.collector._queue)
                                  if self.collector is not None
                                  and hasattr(self.collector, "_queue") else None),
            "userspace_dropped": getattr(self.collector, "queue_dropped", None),
            "uptime_s": round(time.time() - self.started, 1),
            "records": self.records,
            "sessions_closed": self.tracker.closed,
            "checkpoints": self.tracker.checkpoints,
            "sessions_open": len(self.tracker._open),
            "labels": self.labels,
            "unresolved_paths": self.unresolved,
            "foreign_device_records": self.foreign,
            "non_media_sessions": self.non_media,
            "bulk_suppressed": self.tracker.bulk,
            # Requests folded out of open sessions to bound memory. Non-zero means
            # something read one file very hard; see pamts_observer.fold_partials.
            "folded_requests": self.tracker.folded_requests,
            "arrivals": self.arrivals,
            "bytes_written": self.tracker.bytes_written,
            "deferred_pending": len(self._pending),
            "deferred_suppressed": self.deferred_suppressed,
            "deferred_written": self.deferred_written,
            # How much traffic can actually be named. An "unmapped:" entry is a
            # (client, uid) pair nothing in --service covers, which is the thing
            # to look at when attribution has a hole.
            "services": dict(self.services.counts),
            "index_files": self.index.files,
            "index_builds": self.index.builds,
            "index_build_s": round(self.index.last_build_s, 1),
            **self.store.counts(),
        }


def make_handler(daemon):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/health":
                    self._send({"ok": True})
                elif u.path == "/stats":
                    self._send(daemon.stats())
                elif u.path == "/history":
                    since = float(q.get("since", ["0"])[0])
                    limit = int(q.get("limit", ["100000"])[0])
                    self._send({"history": daemon.store.history(since, limit)})
                elif u.path == "/sessions":
                    now = time.clock_gettime(time.CLOCK_MONOTONIC)
                    be = boot_epoch()
                    out = []
                    for sig, label in daemon.tracker.open_sessions:
                        path = (daemon.index.lookup(sig["dev"], sig["ino"], False)
                                if sig.get("ino") is not None else None)
                        out.append({"path": path, "label": label,
                                    "client": sig.get("client"),
                                    "uid": sig.get("uid"),
                                    "uids": sig.get("uids") or [],
                                    "service": daemon.services.describe(
                                        sig.get("client"), sig.get("uid"),
                                        sig.get("uids") or ()),
                                    "bytes": sig["bytes"],
                                    "coverage": sig["coverage"],
                                    "started": be + sig["t_first"],
                                    "idle_s": round(now - sig["t_last"], 1)})
                    self._send({"sessions": out})
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as e:                              # noqa: BLE001
                self._send({"error": str(e)}, 500)

        def log_message(self, *a):
            pass
    return H


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def source_ndjson(stream):
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            yield obs.Record.from_json(json.loads(line))
        except (ValueError, KeyError):
            continue


def source_bpf(obj_path):
    from pamts_bpf import Collector                             # noqa: PLC0415
    return Collector(obj_path).records()


class BoundedHTTPServer(ThreadingHTTPServer):
    """A ceiling on concurrent connections.

    The stock class spawns an unbounded thread per connection. This API is on
    localhost and answers in milliseconds, but a client that opens sockets without
    sending costs a thread each, and this process is the one that must never fall over
    -- it is the only thing watching the storage.
    """
    max_workers = 8
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(self.max_workers)

    def process_request_thread(self, request, client_address):
        with self._slots:
            super().process_request_thread(request, client_address)


def main(argv=None):
    ap = argparse.ArgumentParser(description="PAMTS access observer daemon")
    ap.add_argument("--root", action="append", required=True,
                    help="export root to index for inode->path (repeatable)")
    ap.add_argument("--tier-map", action="append", default=[], metavar="UNION=FAST",
                    help="map a mergerfs union mount to its FAST branch so each "
                         "session records which tier served it, e.g. "
                         "/media/library/tv=/media/media-cache/tv (repeatable). "
                         "Only the fast branch is ever stat'd; absent from it means "
                         "the read came off the slow tier.")
    ap.add_argument("--service", action="append", default=[],
                    metavar="[CLIENT:]UID=NAME",
                    help="name the service a client uid belongs to, e.g. "
                         "10.0.0.11:114=lms (repeatable). The client address "
                         "names only a HOST and several services share one, so the "
                         "uid is what separates them; a bare UID=NAME applies to "
                         "any client. Unmapped pairs are counted in /stats under "
                         "'services' rather than silently dropped.")
    ap.add_argument("--db", default="/var/lib/pamts/observer.db")
    ap.add_argument("--source", choices=("bpf", "ndjson"), default="bpf")
    ap.add_argument("--bpf-object", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pamts_nfsd.bpf.o"))
    ap.add_argument("--listen", default="127.0.0.1:8621")
    ap.add_argument("--idle-gap", type=float, default=obs.DEFAULTS["idle_gap"])
    ap.add_argument("--index-max-age", type=float, default=900.0)
    ap.add_argument("--no-http", action="store_true")
    ap.add_argument("--checkpoint-after", type=float,
                    default=obs.DEFAULTS["checkpoint_after"],
                    help="report an in-progress play after this many seconds "
                         "(0 disables); promotion needs this, see docs")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run until signalled)")
    ap.add_argument("--quiet-sessions", action="store_true",
                    help="do not log a line per classified session")
    ap.add_argument("--prune-every", type=float, default=3600.0,
                    help="seconds between session-table prunes (0 = only at exit); "
                         "a long run must prune periodically or the table grows "
                         "without bound")
    ap.add_argument("--keep-sessions", type=int, default=200_000)
    ap.add_argument("--bulk-window", type=float,
                    default=obs.DEFAULTS["bulk_window"])
    ap.add_argument("--bulk-min-files", type=int,
                    default=obs.DEFAULTS["bulk_min_files"])
    ap.add_argument("--replay-gap", type=float, default=1800.0,
                    help="seconds within which the same file counts as the SAME "
                         "viewing rather than a new play; players read in bursts "
                         "with long gaps, so without this one episode counts "
                         "several times")
    ap.add_argument("--no-defer", action="store_true",
                    help="write play history immediately instead of holding it for "
                         "bulk_window; faster to observe, but the first files of a "
                         "sweep will be recorded as plays")
    ap.add_argument("--all-devices", action="store_true",
                    help="do not drop reads on devices outside --root; useful for "
                         "diagnosing why something is not being seen")
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    cfg = {"idle_gap": args.idle_gap,
           "checkpoint_after": args.checkpoint_after,
           "bulk_window": args.bulk_window,
           "bulk_min_files": args.bulk_min_files}

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    index = InodeIndex(args.root, max_age=args.index_max_age)
    # Build the index in the BACKGROUND and start serving immediately. Walking a
    # mergerfs union whose cold branch is NFS to a spun-down array took 101.8s on a
    # cold ARC, against 2.0s warm -- and blocking startup on that means the API and
    # the collector are both down for a minute and a half, which is indistinguishable
    # from the daemon being broken. Lookups simply miss until the index lands, which
    # is the contract rebuilds already use.
    logging.info("indexing %s in the background ...", ", ".join(index.roots))
    index.rebuild_async()

    store = Store(args.db, keep_sessions=args.keep_sessions,
                  replay_gap=args.replay_gap)
    tracker = obs.SessionTracker(cfg)
    tier_pairs = []
    for spec in args.tier_map:
        if "=" not in spec:
            ap.error("--tier-map wants UNION=FAST, got %r" % spec)
        u, f = spec.split("=", 1)
        if not u.startswith("/") or not f.startswith("/"):
            ap.error("--tier-map paths must be absolute, got %r" % spec)
        tier_pairs.append((u, f))
    tiers = TierResolver(tier_pairs) if tier_pairs else None
    if tiers:
        for u, f in tiers.unions:
            logging.info("tier map: %s -> fast branch %s", u, f)
    else:
        logging.info("no --tier-map given; sessions will not record a tier")

    svc_triples = parse_service_specs(args.service, ap.error)
    services = ServiceResolver(svc_triples)
    if svc_triples:
        for client, uid, name in svc_triples:
            logging.info("service map: %s uid %d -> %s",
                         client or "any client", uid, name)
    else:
        logging.info("no --service given; sessions still record the client uid, "
                     "but nothing will put a service name to it")

    daemon = Daemon(store, index, tracker, cfg, tiers=tiers, services=services,
                    log_sessions=not args.quiet_sessions,
                    defer=not args.no_defer)
    tracker.on_close = daemon.on_close
    tracker.on_arrival = daemon.on_arrival
    # The tracker has inodes, not paths, but the playback-rate ceiling depends on
    # whether this is audio or video -- genuine audio never exceeds 0.41 MB/s
    # while a video prefill burst reaches 20.7, so one ceiling cannot serve both.
    tracker.media_of = lambda dev, ino: obs.media_kind(
        index.lookup(dev, ino, allow_rebuild=False))

    httpd = None
    if not args.no_http:
        host, _, port = args.listen.rpartition(":")
        httpd = BoundedHTTPServer((host or "127.0.0.1", int(port)),
                                    make_handler(daemon))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        logging.info("serving on %s", args.listen)

    # Sessions close on silence, so something must close them when no records
    # arrive at all -- otherwise the last play of the night is never reported.
    def reaper():
        # If this thread dies, sessions stop being closed and the daemon goes
        # quietly deaf -- which is exactly what happened when an unhandled
        # KeyError killed it. Never let one bad tick end the loop.
        while True:
            time.sleep(min(5.0, args.idle_gap / 2))
            try:
                now = time.clock_gettime(time.CLOCK_MONOTONIC)
                tracker.tick(now)
                daemon.flush_pending(now)
            except Exception:                                   # noqa: BLE001
                logging.exception("reaper tick failed; continuing")
    threading.Thread(target=reaper, daemon=True).start()

    if args.prune_every > 0:
        def pruner():
            while True:
                time.sleep(args.prune_every)
                try:
                    store.prune()
                except sqlite3.Error as e:
                    logging.warning("prune failed: %s", e)
        threading.Thread(target=pruner, daemon=True).start()

    collector = None
    if args.source == "bpf":
        from pamts_bpf import Collector                         # noqa: PLC0415
        collector = Collector(args.bpf_object)
        logging.info("attached: %s", ", ".join(collector.attached))
        # The Daemon is built before the collector exists, so hand it over now:
        # its /stats needs the kernel-side emitted/dropped counters.
        daemon.collector = collector
        src = collector.records()
    else:
        src = source_ndjson(sys.stdin)

    stopping = threading.Event()

    def request_stop(signum, _frame):
        stopping.set()
        if collector is not None:
            collector.stop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, sig_name), request_stop)
        except (AttributeError, ValueError):
            pass

    if args.duration > 0:
        threading.Timer(args.duration, lambda: request_stop(0, None)).start()

    try:
        for rec in src:
            daemon.records += 1
            # Reads on a device none of our roots live on are someone else's
            # dataset. Dropping them here rather than at path-resolution time
            # keeps the log about our own media and avoids sessionising traffic
            # we can never name.
            if (args.all_devices is False and rec.dev is not None
                    and index.devs and rec.dev not in index.devs):
                daemon.foreign += 1
                continue
            tracker.add(rec)
            if stopping.is_set():
                break
    except KeyboardInterrupt:
        stopping.set()
        if collector is not None:
            collector.stop()
    finally:
        tracker.flush()
        # Held decisions must still be judged, not silently dropped: the window
        # history is retained long enough to rule on them at shutdown.
        daemon.flush_pending(time.clock_gettime(time.CLOCK_MONOTONIC), force=True)
        store.prune()
        if httpd:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
    logging.info("final: %s", json.dumps(daemon.stats()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
