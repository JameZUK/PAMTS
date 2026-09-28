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
        if allow_rebuild and age > self.max_age:
            self.build()
            with self._lock:
                return self._map.get((dev, ino))
        return None


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
    requests INTEGER, monotonic REAL, method TEXT
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

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30.0)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    def record(self, path, sig, label, epoch_ts, count_it=True):
        c = self._conn()
        with c:
            c.execute(
                "INSERT INTO sessions(ts,path,dev,ino,client,label,bytes,coverage,"
                "duration,rate,requests,monotonic,method) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (epoch_ts, path, sig.get("dev"), sig.get("ino"), sig.get("client"),
                 label, sig["bytes"], sig["coverage"], sig["duration"], sig["rate"],
                 sig["requests"], sig["monotonic"], sig["method"]))
            if path and label in DEMAND:
                # A session reported mid-flight and then again on close must not
                # count as two plays, so the increment is carried by count_it.
                inc = 1 if count_it else 0
                if inc:
                    prev = c.execute(
                        "SELECT last_play FROM plays WHERE path = ?",
                        (path,)).fetchone()
                    if prev and (epoch_ts - prev[0]) < self.replay_gap:
                        inc = 0          # same viewing, resumed after a buffer gap
                c.execute(
                    "INSERT INTO plays(path,last_play,play_count,last_label,last_bytes,updated) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "  last_play=MAX(last_play,excluded.last_play), "
                    "  play_count=play_count+?, last_label=excluded.last_label, "
                    "  last_bytes=MAX(last_bytes,excluded.last_bytes), "
                    "  updated=excluded.updated",
                    (path, epoch_ts, inc, label, sig["bytes"], time.time(), inc))

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
    def __init__(self, store, index, tracker, cfg, log_sessions=True):
        self.store, self.index, self.tracker, self.cfg = store, index, tracker, cfg
        self.log_sessions = log_sessions
        self.started = time.time()
        self.records = 0
        self.unresolved = 0
        self.foreign = 0
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
        self.store.record(path, sig, label, epoch, count_it=first)
        if self.log_sessions:
            cov = "?" if sig["coverage"] is None else f"{sig['coverage']:.2f}"
            rate = "-" if not sig["rate"] else f"{sig['rate'] / (1 << 20):.1f}MB/s"
            logging.info(
                "%-6s %-15s %8.1fMB cov=%-5s %5.0fs %-9s reqs=%-5d %s%s",
                label, sig.get("client") or "?", sig["bytes"] / (1 << 20), cov,
                sig["duration"], rate, sig["requests"],
                path or f"<unresolved dev={sig['dev']} ino={sig['ino']}>",
                "" if first else "  (refresh)")

    def stats(self):
        return {
            "uptime_s": round(time.time() - self.started, 1),
            "records": self.records,
            "sessions_closed": self.tracker.closed,
            "checkpoints": self.tracker.checkpoints,
            "sessions_open": len(self.tracker._open),
            "labels": self.labels,
            "unresolved_paths": self.unresolved,
            "foreign_device_records": self.foreign,
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="PAMTS access observer daemon")
    ap.add_argument("--root", action="append", required=True,
                    help="export root to index for inode->path (repeatable)")
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
    ap.add_argument("--replay-gap", type=float, default=1800.0,
                    help="seconds within which the same file counts as the SAME "
                         "viewing rather than a new play; players read in bursts "
                         "with long gaps, so without this one episode counts "
                         "several times")
    ap.add_argument("--all-devices", action="store_true",
                    help="do not drop reads on devices outside --root; useful for "
                         "diagnosing why something is not being seen")
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    cfg = {"idle_gap": args.idle_gap,
           "checkpoint_after": args.checkpoint_after}

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    index = InodeIndex(args.root, max_age=args.index_max_age)
    logging.info("indexing %s ...", ", ".join(index.roots))
    index.build()

    store = Store(args.db, keep_sessions=args.keep_sessions,
                  replay_gap=args.replay_gap)
    tracker = obs.SessionTracker(cfg)
    daemon = Daemon(store, index, tracker, cfg,
                    log_sessions=not args.quiet_sessions)
    tracker.on_close = daemon.on_close

    httpd = None
    if not args.no_http:
        host, _, port = args.listen.rpartition(":")
        httpd = ThreadingHTTPServer((host or "127.0.0.1", int(port)),
                                    make_handler(daemon))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        logging.info("serving on %s", args.listen)

    # Sessions close on silence, so something must close them when no records
    # arrive at all -- otherwise the last play of the night is never reported.
    def reaper():
        while True:
            time.sleep(min(5.0, args.idle_gap / 2))
            tracker.tick(time.clock_gettime(time.CLOCK_MONOTONIC))
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
        store.prune()
        if httpd:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
    logging.info("final: %s", json.dumps(daemon.stats()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
