#!/usr/bin/env python3
"""Access-pattern observation for PAMTS: turn NFS server read events into a
per-file demand signal.

WHY THIS EXISTS
---------------
Player APIs tell you what a *player* knows. That is per-user, per-service, and
some services will not tell you at all. The filesystem sees every consumer
equally. If the media is served over NFS from one host, the server sees all of
it, which makes one observer worth more than one adapter per service.

WHAT IT CANNOT DO
-----------------
Exports using `all_squash` erase user identity at the server, so this yields
FILE-LEVEL DEMAND and never per-user history. Good for tiering, useless for
anything user-facing. The kernel's nfsd read tracepoints also carry no client
address, so two clients reading one file concurrently merge into one session.
Recovering the client needs eBPF (`svc_rqst`); ftrace alone cannot.

THE LIFECYCLE
-------------
All of nfsd's read tracepoints carry (xid, fh_hash, offset, len):

    read_start(len=requested)
        -> read_splice | read_vector | read_direct      (the method)
        -> read_done(len=delivered) | read_err(status)

Both matter. `read_done` is the authoritative byte count and summing across
several tracepoints double-counts every read; but WHICH method fired is a
feature, so the method events are kept rather than discarded.

`read_done.len < read_start.len` means the client hit EOF, which reveals the
file size -- so coverage (bytes read / file size) is computable WITHOUT ever
resolving fh_hash to a path. Coverage is the strongest play-vs-scan signal we
have, and path resolution is the expensive part of this problem, so getting
coverage for free matters.
"""
import re
import threading
from collections import defaultdict, deque

KB = 1024
MB = 1024 ** 2

METHODS = ("splice", "vector", "direct")

# Only these are tierable. Artwork, playlists and sidecars are read constantly by
# scanning media servers and are far too small to be worth moving between tiers --
# and being tiny, a whole-file read of one looks exactly like a FETCH. Left
# unfiltered, one library scan put 1,350 .jpg files into play history.
MEDIA_EXT = (
    ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".flv", ".mpg",
    ".mpeg", ".m2ts", ".webm", ".iso",
    ".flac", ".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".wav", ".wma", ".aac",
    ".alac", ".ape", ".dsf", ".dff", ".aiff", ".aif",
)


def is_media(path):
    """Is this a file worth tiering at all?"""
    if not path:
        return False
    dot = path.rfind(".")
    return dot >= 0 and path[dot:].lower() in MEDIA_EXT

# xid is a 32-bit RPC transaction id and WILL wrap on a long capture, so a
# start is only ever matched to a completion seen within this many seconds.
XID_WINDOW = 10.0

DEFAULTS = {
    "idle_gap":           30.0,   # s of silence that ends a session
    "probe_max_coverage":  0.05,
    "probe_max_bytes":     2 * MB,
    "probe_max_duration":  5.0,
    "play_min_coverage":   0.15,
    "copy_min_coverage":   0.85,
    "copy_min_rate":      40 * MB,  # bytes/s: above this a human is not listening
    "min_monotonic":       0.80,
    "play_min_bytes":      1 * MB,   # below this there is nothing to pace
    # A sequential read of THIS MUCH of one file is playback whatever fraction of
    # the file it is, because nothing else reads hundreds of megabytes in order
    # and then stops. Measured: a playback-start prefill burst of 207 MB in 10s
    # at 20.7 MB/s fell through every class, because 14.3% coverage missed
    # play_min_coverage and 10s missed play_long_duration.
    "play_min_bytes_abs": 64 * MB,
    "play_min_duration":   5.0,      # floor before "progressive" means anything
    "play_long_duration":  30.0,     # long+progressive counts even if coverage unknown
    "fetch_max_filesize": 64 * MB,   # a whole file this small, read fast, is a fetch
    "region_gap":         4 * MB,  # a jump this large starts a new region
    # bulk sweep guard
    "bulk_window":       120.0,
    "bulk_min_files":     25,
    # Report a play while it is still in progress. A 45 minute episode that is
    # only reported when it ENDS is useless for promotion, which needs to fetch
    # the next item during playback, not after it.
    "checkpoint_after":   60.0,
}

LINE = re.compile(
    r"(?P<ts>\d+\.\d+):\s+nfsd_(?P<tp>read_\w+):\s+(?P<rest>.*)$"
)
KV = re.compile(r"(\w+)=(0x[0-9a-fA-F]+|-?\d+)")


class Event:
    __slots__ = ("ts", "tp", "xid", "fh", "offset", "length", "status")

    def __init__(self, ts, tp, xid, fh, offset, length, status):
        self.ts, self.tp, self.xid, self.fh = ts, tp, xid, fh
        self.offset, self.length, self.status = offset, length, status

    def __repr__(self):
        return (f"Event({self.ts:.6f} {self.tp} fh={self.fh:#x} "
                f"off={self.offset} len={self.length})")


def parse_line(line):
    """One ftrace line -> Event, or None if it is not an nfsd read event."""
    m = LINE.search(line)
    if not m:
        return None
    kv = {k: int(v, 0) for k, v in KV.findall(m.group("rest"))}
    if "xid" not in kv or "fh_hash" not in kv:
        return None
    return Event(
        ts=float(m.group("ts")),
        tp=m.group("tp"),                 # read_start / read_splice / ... / read_done
        xid=kv["xid"],
        fh=kv["fh_hash"],
        offset=kv.get("offset", 0),
        length=kv.get("len"),             # absent on read_err
        status=kv.get("status"),
    )


def parse(lines):
    out = []
    for ln in lines:
        ev = parse_line(ln)
        if ev is not None:
            out.append(ev)
    out.sort(key=lambda e: e.ts)
    return out


class Request:
    """One read RPC, reassembled from its several tracepoints."""
    __slots__ = ("ts", "fh", "offset", "requested", "delivered", "method",
                 "status")

    def __init__(self, ts, fh, offset):
        self.ts, self.fh, self.offset = ts, fh, offset
        self.requested = None
        self.delivered = None
        self.method = None
        self.status = None

    @property
    def short(self):
        """Delivered less than asked for => the client reached EOF."""
        return (self.requested is not None and self.delivered is not None
                and self.delivered < self.requested)

    @property
    def eof_hint(self):
        return self.offset + self.delivered if self.short else None

    def __repr__(self):
        return (f"Request(fh={self.fh:#x} off={self.offset} "
                f"req={self.requested} got={self.delivered} {self.method})")


def join_requests(events, xid_window=XID_WINDOW):
    """Group events into Requests by xid, scoped in time because xid wraps.

    A request is closed by read_done or read_err. An xid seen again after its
    request closed, or after xid_window, starts a fresh one -- never reuses the
    old record, which is how wrap-around would otherwise fuse two reads.
    """
    open_reqs = {}           # xid -> Request still awaiting completion
    done = []

    for ev in events:
        cur = open_reqs.get(ev.xid)
        if cur is not None and ev.ts - cur.ts > xid_window:
            done.append(cur)                      # abandoned: never completed
            cur = None
            del open_reqs[ev.xid]

        if ev.tp == "read_start":
            if cur is not None:                   # previous never completed
                done.append(cur)
            cur = Request(ev.ts, ev.fh, ev.offset)
            cur.requested = ev.length
            open_reqs[ev.xid] = cur
            continue

        if cur is None:
            # Capture started mid-request, or read_start was not enabled.
            cur = Request(ev.ts, ev.fh, ev.offset)
            open_reqs[ev.xid] = cur

        name = ev.tp[len("read_"):]
        if name in METHODS:
            cur.method = name
            if cur.requested is None:
                cur.requested = ev.length
        elif ev.tp == "read_done":
            cur.delivered = ev.length
            if cur.requested is None:
                cur.requested = ev.length
            done.append(cur)
            del open_reqs[ev.xid]
        elif ev.tp == "read_err":
            cur.status = ev.status
            cur.delivered = 0
            done.append(cur)
            del open_reqs[ev.xid]

    done.extend(open_reqs.values())
    done.sort(key=lambda r: r.ts)
    return done


def sessionise(requests, idle_gap=None):
    """Split each file's requests into sessions separated by idle_gap seconds."""
    if idle_gap is None:
        idle_gap = DEFAULTS["idle_gap"]
    by_fh = defaultdict(list)
    for r in requests:
        by_fh[r.fh].append(r)

    sessions = []
    for fh, reqs in by_fh.items():
        reqs.sort(key=lambda r: r.ts)
        cur = [reqs[0]]
        for r in reqs[1:]:
            if r.ts - cur[-1].ts > idle_gap:
                sessions.append(cur)
                cur = [r]
            else:
                cur.append(r)
        sessions.append(cur)
    sessions.sort(key=lambda s: s[0].ts)
    return sessions


def signature(session, cfg=None):
    """Per-file access signature. Pure arithmetic over one session's requests."""
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)

    reqs = sorted(session, key=lambda r: r.ts)
    delivered = [r for r in reqs if r.delivered]
    total = sum(r.delivered for r in delivered)

    offsets = [r.offset for r in reqs]
    ends = [r.offset + (r.delivered or r.requested or 0) for r in reqs]

    # EOF sighting gives us the file size for free.
    eofs = [r.eof_hint for r in reqs if r.eof_hint is not None]
    filesize = max(eofs) if eofs else None
    coverage = (total / filesize) if filesize else None

    # Monotonicity over successive reads: streaming walks forward.
    steps = [1 if offsets[i] >= offsets[i - 1] else 0
             for i in range(1, len(offsets))]
    monotonic = (sum(steps) / len(steps)) if steps else 1.0

    # Distinct regions: head-and-tail probing shows up as 2 regions far apart.
    regions, prev_end = 1, ends[0] if ends else 0
    for i in range(1, len(reqs)):
        if abs(offsets[i] - prev_end) > c["region_gap"]:
            regions += 1
        prev_end = max(prev_end, ends[i])

    span = (max(ends) - min(offsets)) if reqs else 0
    duration = reqs[-1].ts - reqs[0].ts
    # No elapsed time means no measurable rate. Inventing one from the byte
    # count made a single 0.4 MB read look like paced playback.
    rate = (total / duration) if duration > 0 else None

    methods = {m: 0 for m in METHODS}
    for r in reqs:
        if r.method in methods:
            methods[r.method] += 1
    known = sum(methods.values()) or 1

    return {
        "fh":            reqs[0].fh,
        "t_first":       reqs[0].ts,
        "t_last":        reqs[-1].ts,
        "duration":      duration,
        "requests":      len(reqs),
        "bytes":         total,
        "filesize":      filesize,
        "coverage":      coverage,
        "min_offset":    min(offsets) if offsets else 0,
        "max_offset":    max(offsets) if offsets else 0,
        "span":          span,
        "monotonic":     monotonic,
        # NB: on a real video stream this counted 6574 "regions" -- it is
        # measuring readahead discontinuity, NOT seeking. Only trust it
        # alongside a low monotonic score.
        "regions":       regions,
        "starts_at_zero": bool(offsets) and min(offsets) == 0,
        "short_reads":   sum(1 for r in reqs if r.short),
        "errors":        sum(1 for r in reqs if r.status is not None),
        "rate":          rate,
        # method mix is a feature, not bookkeeping
        "frac_splice":   methods["splice"] / known,
        "frac_vector":   methods["vector"] / known,
        "frac_direct":   methods["direct"] / known,
        "method":        max(methods, key=methods.get) if sum(methods.values()) else None,
    }


def classify(sig, cfg=None):
    """Label one signature. Only PLAY should ever drive promotion.

    PROBE exists because a library scan must never move data -- the founding
    constraint. COPY and FETCH exist because a bulk reader looks like an
    extremely keen listener.

    FETCH is the honest answer to an ambiguity that cannot be resolved from one
    file's read pattern: a player buffering a whole 5 MB track in 0.3 s and a
    fingerprinter reading that same track in full are INDISTINGUISHABLE here.
    Both are whole-file, both are fast. What separates them is whether the
    neighbours were read too, which is `detect_bulk`'s job, not this one's.
    Policy decides whether a lone FETCH counts as demand; a FETCH in a crowd
    is a sweep.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)

    cov = sig["coverage"]
    rate = sig["rate"]

    if sig["bytes"] == 0:
        return "ERROR" if sig["errors"] else "UNKNOWN"

    whole = cov is not None and cov >= c["copy_min_coverage"]
    fast = rate is None or rate >= c["copy_min_rate"]

    # Header/tail sip: tag read, container probe, thumbnailer.
    small = (cov is not None and cov <= c["probe_max_coverage"]) or \
            (cov is None and sig["bytes"] <= c["probe_max_bytes"])
    if small and sig["duration"] <= c["probe_max_duration"]:
        return "PROBE"

    # Whole file, no pacing. Big => a copy or analysis pass. Small => could be
    # either a buffered play or a scan, so say FETCH and let policy decide.
    if whole and sig["duration"] < c["play_min_duration"]:
        if sig["bytes"] <= c["fetch_max_filesize"] and not fast:
            return "FETCH"
        return "COPY"
    if whole and fast:
        return "COPY"

    # Paced, progressive, and sustained long enough for the pacing to be real.
    if (sig["monotonic"] >= c["min_monotonic"]
            and rate is not None and rate < c["copy_min_rate"]
            and sig["bytes"] >= c["play_min_bytes"]
            and sig["duration"] >= c["play_min_duration"]):
        if (cov is not None and cov >= c["play_min_coverage"]) or \
           sig["duration"] >= c["play_long_duration"] or \
           sig["bytes"] >= c["play_min_bytes_abs"]:
            return "PLAY"

    if sig["regions"] > 2 and sig["monotonic"] < c["min_monotonic"]:
        return "SEEK"

    return "UNKNOWN"


def detect_bulk(sigs, cfg=None):
    """Mark whole-file reads that arrive in a crowd as BULK, not PLAY.

    A player reads one track then waits roughly its playing time. A sweep --
    rsync over NFS, a loudness-analysis pass, a fingerprinter -- reads many
    files back to back. Per-file signatures cannot tell these apart, because
    the difference is in the ARRIVAL RATE ACROSS FILES, so it is decided here.

    Note the local backup jobs never appear at all: they run inside the storage
    host and so are not NFS reads. This guards against remote sweeps.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)

    whole = sorted(
        [s for s in sigs
         if s["coverage"] is not None and s["coverage"] >= c["copy_min_coverage"]],
        key=lambda s: s["t_first"],
    )
    flagged = set()
    for i, s in enumerate(whole):
        group = [w for w in whole[i:] if w["t_first"] - s["t_first"] <= c["bulk_window"]]
        if len({w["fh"] for w in group}) >= c["bulk_min_files"]:
            flagged.update(id(w) for w in group)
    return flagged


def analyse(lines, cfg=None):
    """ftrace text -> [(signature, label)], with the bulk guard applied."""
    reqs = join_requests(parse(lines))
    sessions = sessionise(reqs, (cfg or {}).get("idle_gap"))
    sigs = [signature(s, cfg) for s in sessions]
    bulk = detect_bulk(sigs, cfg)
    out = []
    for s in sigs:
        label = "BULK" if id(s) in bulk else classify(s, cfg)
        out.append((s, label))
    return out


# ---------------------------------------------------------------------------
# Online tracking
#
# Everything above is batch: give it a whole capture, get signatures back. A
# daemon cannot work that way -- it sees records one at a time, forever, and has
# to decide when a session has ended without ever seeing the future.
# ---------------------------------------------------------------------------

class Record:
    """One read event from the collector, however it arrived.

    The eBPF collector gives us more than ftrace can: a real inode and device,
    and the client address. `fh` stays available so ftrace captures can feed the
    same code path.
    """
    __slots__ = ("ts", "kind", "xid", "fh", "offset", "length", "status",
                 "ino", "dev", "client")

    def __init__(self, ts, kind, xid=0, fh=0, offset=0, length=None,
                 status=None, ino=None, dev=None, client=None):
        self.ts, self.kind, self.xid = ts, kind, xid
        self.fh, self.offset, self.length, self.status = fh, offset, length, status
        self.ino, self.dev, self.client = ino, dev, client

    @property
    def key(self):
        """What identifies "the same file, from the same reader".

        Prefer (dev, ino, client): it is unambiguous, and including the client
        stops two viewers of one file merging into a nonsense session. Fall back
        to fh_hash when only ftrace-grade data is available, which is exactly
        when that merging becomes unavoidable.
        """
        if self.ino is not None:
            return (self.dev, self.ino, self.client)
        return (None, self.fh, self.client)

    @classmethod
    def from_json(cls, obj):
        return cls(ts=obj["ts"], kind=obj["kind"], xid=obj.get("xid", 0),
                   fh=obj.get("fh", 0), offset=obj.get("offset", 0),
                   length=obj.get("len"), status=obj.get("status"),
                   ino=obj.get("ino"), dev=obj.get("dev"),
                   client=obj.get("client"))


class SessionTracker:
    """Streams records in, emits (key, signature, label) as sessions close.

    Sessions close on idle timeout, so `tick()` must be called even when no
    records are arriving -- otherwise the last session of the evening is never
    reported. Closing is driven by the record clock where possible and by the
    wall clock in `tick`, because a quiet tap and a stopped tap look identical
    from the inside.
    """

    def __init__(self, cfg=None, on_close=None):
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        self.on_close = on_close
        self.checkpoints = 0
        # Recent (ts, file-key) per client, for the bulk guard. detect_bulk()
        # works on a finished capture; a daemon never has one, so the same
        # decision has to be made from a rolling window instead.
        self._recent = defaultdict(deque)
        self.bulk = 0
        # A daemon closes sessions from TWO threads: the ingest loop via add(),
        # and a timer via tick() -- because a session ends by going quiet, which
        # no arriving record can signal. Both mutate _open, which crashed under a
        # library scan with "dictionary changed size during iteration" and with a
        # KeyError when both raced to close the same session.
        #
        # The lock is held only while mutating. on_close() is dispatched OUTSIDE
        # it, because it writes to SQLite and may rebuild the path index, and
        # stalling ingest for that long would overflow the kernel ring buffer.
        self._lock = threading.RLock()
        self._open = {}          # key -> {"reqs": [...], "pending": {xid: Request}}
        self.closed = 0
        self.dropped = 0

    # -- ingest ------------------------------------------------------------
    def add(self, rec):
        with self._lock:
            self._ingest(rec)
        # sweep outside the ingest critical section; it dispatches callbacks
        self._sweep(rec.ts)

    def _ingest(self, rec):
        key = rec.key
        st = self._open.get(key)
        if st is None:
            st = {"reqs": [], "pending": {}, "last": rec.ts,
                  "first": rec.ts, "checkpointed": False}
            self._open[key] = st
        st["last"] = max(st["last"], rec.ts)

        pend = st["pending"]
        cur = pend.get(rec.xid)
        if cur is not None and rec.ts - cur.ts > XID_WINDOW:
            st["reqs"].append(cur)
            cur = None
            del pend[rec.xid]

        if rec.kind == "read_start":
            if cur is not None:
                st["reqs"].append(cur)
            cur = Request(rec.ts, rec.fh, rec.offset)
            cur.requested = rec.length
            pend[rec.xid] = cur
            return

        if cur is None:
            cur = Request(rec.ts, rec.fh, rec.offset)
            pend[rec.xid] = cur

        name = rec.kind[len("read_"):] if rec.kind.startswith("read_") else rec.kind
        if name in METHODS:
            cur.method = name
            if cur.requested is None:
                cur.requested = rec.length
        elif rec.kind == "read_done":
            cur.delivered = rec.length
            if cur.requested is None:
                cur.requested = rec.length
            st["reqs"].append(cur)
            del pend[rec.xid]
        elif rec.kind == "read_err":
            cur.status = rec.status
            cur.delivered = 0
            st["reqs"].append(cur)
            del pend[rec.xid]


    # -- closing -----------------------------------------------------------
    def _sweep(self, now):
        """Close what has gone quiet and checkpoint what is still going.

        Everything that touches shared state happens under the lock; the
        callbacks are fired afterwards, so a slow consumer cannot block ingest.
        """
        pending = []
        with self._lock:
            # Close idle sessions FIRST. Otherwise a session that has already
            # gone quiet gets checkpointed and then immediately closed, writing
            # the same play twice for no benefit.
            gap = self.cfg["idle_gap"]
            for key in [k for k, st in list(self._open.items())
                        if now - st["last"] > gap]:
                got = self._pop(key)
                if got:
                    pending.append(got)

            after = self.cfg.get("checkpoint_after") or 0
            if after:
                for key, st in list(self._open.items()):
                    if st["checkpointed"] or now - st["first"] < after:
                        continue
                    reqs = st["reqs"] + list(st["pending"].values())
                    if not reqs:
                        continue
                    sig = self._sig(key, reqs)
                    if classify(sig, self.cfg) == "PLAY":
                        st["checkpointed"] = True
                        self.checkpoints += 1
                        # The checkpoint IS the first report of this session, so
                        # it carries the increment; the later close must not.
                        pending.append((key, sig, "PLAY", True))

        for key, sig, label, first in pending:
            if self.on_close:
                self.on_close(key, sig, label, first)

    def tick(self, now):
        """Close sessions that have gone quiet. Safe to call as often as you like."""
        self._sweep(now)

    def flush(self):
        pending = []
        with self._lock:
            for key in list(self._open):
                got = self._pop(key)
                if got:
                    pending.append(got)
        for key, sig, label, first in pending:
            if self.on_close:
                self.on_close(key, sig, label, first)

    def _sig(self, key, reqs):
        sig = signature(reqs, self.cfg)
        sig["key"] = key
        sig["dev"], sig["ino"] = key[0], key[1]
        sig["client"] = key[2] if len(key) > 2 else None
        return sig

    def _note(self, sig):
        """Record this file against its client, and say how many distinct files
        that client has touched in the bulk window.

        Counting DISTINCT FILES, not sessions, is the point: one viewer re-reading
        one file in bursts must not look like a sweep, while a scanner walking a
        library touches hundreds of different files in the same period.
        """
        window = self.cfg["bulk_window"]
        now, client = sig["t_last"], sig.get("client")
        q = self._recent[client]
        q.append((now, (sig.get("dev"), sig.get("ino"), sig.get("fh"))))
        while q and now - q[0][0] > window:
            q.popleft()
        return len({k for _, k in q})

    def _pop(self, key):
        """Remove a session and return (key, sig, label, first). Caller holds the
        lock and is responsible for dispatching the callback."""
        st = self._open.pop(key, None)
        if st is None:
            return None                  # another thread closed it first
        reqs = st["reqs"] + list(st["pending"].values())
        if not reqs:
            self.dropped += 1
            return None
        sig = self._sig(key, reqs)
        label = classify(sig, self.cfg)
        distinct = self._note(sig)
        sig["client_files_in_window"] = distinct
        # A sweep is decided ACROSS files, never within one: a player reads a
        # track then waits roughly its playing time, while a scanner reads the
        # neighbours too. Only demand labels are downgraded -- relabelling a
        # PROBE as BULK would lose information for no gain.
        if label in ("PLAY", "FETCH") and distinct >= self.cfg["bulk_min_files"]:
            label = "BULK"
            self.bulk += 1
        self.closed += 1
        # `first` is False when this session was already reported mid-flight, so
        # the consumer can refresh it without counting the play twice.
        return key, sig, label, not st["checkpointed"]

    @property
    def open_sessions(self):
        """Sessions in flight, for a "what is playing right now" endpoint."""
        out = []
        with self._lock:
            snapshot = [(k, st["reqs"] + list(st["pending"].values()))
                        for k, st in self._open.items()]
        for key, reqs in snapshot:
            if not reqs:
                continue
            sig = self._sig(key, reqs)
            out.append((sig, classify(sig, self.cfg)))
        return out
