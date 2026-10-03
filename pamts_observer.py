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
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".flv",
             ".mpg", ".mpeg", ".m2ts", ".webm", ".iso")
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".wav", ".wma",
             ".aac", ".alac", ".ape", ".dsf", ".dff", ".aiff", ".aif")
MEDIA_EXT = VIDEO_EXT + AUDIO_EXT


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
    # Above this, sustained and in order, it cannot be playback whatever else is
    # known -- so COPY no longer needs the file size. Measured on a real server:
    # music plays at 0.2 MB/s, 2160p video at 2.8-3.4, a buffer refill at 9.6, a
    # playback-start prefill at 20.7 -- and a media server analysing a freshly
    # downloaded file at 37.9 to 110. A whole 8.6 GB episode read at 97 MB/s was
    # scoring UNKNOWN purely because no EOF had been seen, and a 534 MB read at
    # 37.9 MB/s scored PLAY, two megabytes a second under the old threshold.
    "max_play_rate":      30 * MB,
    # Per media type, because one ceiling cannot serve both. Measured over 172
    # genuine audio plays and 5 video ones on a live server:
    #
    #   audio PLAY   max 0.41 MB/s   (p95 0.37)
    #   audio COPY   min 26.2        median 43.2
    #   audio BULK   median 10.8     -- an audio scan sails under a 30 MB/s ceiling
    #   video PLAY   up to ~20.7     (a playback-start prefill burst)
    #   video COPY   min 51.5        median 110
    #
    # So audio gets a ceiling five times its observed maximum and still sits far
    # below anything that was not playback. Timing cannot do this job: a scan is
    # not tied to when a file arrived -- one episode landed at 00:00 and was
    # analysed at 03:10 -- and anchoring on arrival would also discard the normal
    # case of downloading something and watching it twenty minutes later.
    "max_play_rate_audio":  2 * MB,
    "max_play_rate_video": 30 * MB,
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
    # Requests held per OPEN session before the oldest are folded into an aggregate
    # (see fold_partials). An open session kept one object per read RPC for as long
    # as reads kept arriving, so a single sustained read grew without limit -- one
    # burst reached 2.7 million events and OOM-killed the daemon five times in a
    # fortnight. 20,000 is far above any genuine playback session (a 3-hour film at
    # 128 KB per read is ~170,000 over its whole length, and it closes long before
    # that) while capping the memory one session can hold at a few megabytes.
    "max_open_requests": 20_000,
    # bulk sweep guard
    "bulk_window":       120.0,
    "bulk_min_files":     25,
    # Report a play while it is still in progress. A 45 minute episode that is
    # only reported when it ENDS is useless for promotion, which needs to fetch
    # the next item during playback, not after it.
    "checkpoint_after":   60.0,
    # How long an arrival is remembered at all.
    "write_ttl":        7200.0,
}

WRITE_KINDS = frozenset(("write_start", "write_done", "write_err", "commit_done"))

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


def _finalise(f, c):
    """Turn a complete aggregate into the signature dict every caller expects."""
    total = f["bytes"]
    filesize = f["filesize"]
    known = sum(f["methods"].values()) or 1
    duration = f["t_last"] - f["t_first"]
    return {
        "fh":            f["fh"],
        "t_first":       f["t_first"],
        "t_last":        f["t_last"],
        "duration":      duration,
        "requests":      f["n"],
        "bytes":         total,
        "filesize":      filesize,
        "coverage":      (total / filesize) if filesize else None,
        "min_offset":    f["min_offset"] or 0,
        "max_offset":    f["max_offset"] or 0,
        "span":          (f["max_end"] - f["min_offset"]) if f["n"] else 0,
        "monotonic":     (f["mono_ok"] / f["mono_n"]) if f["mono_n"] else 1.0,
        "regions":       f["regions"],
        "starts_at_zero": f["min_offset"] == 0,
        "short_reads":   f["short_reads"],
        "errors":        f["errors"],
        "rate":          (total / duration) if duration > 0 else None,
        "frac_splice":   f["methods"]["splice"] / known,
        "frac_vector":   f["methods"]["vector"] / known,
        "frac_direct":   f["methods"]["direct"] / known,
        "method":        (max(f["methods"], key=f["methods"].get)
                          if sum(f["methods"].values()) else None),
        # So a consumer can tell that `regions` is approximate here.
        "folded":        f["n_folded"],
    }


def _merged_signature(folded, reqs, c):
    """Folded aggregate + the requests still held = one signature."""
    n_folded = folded["n"]
    f = fold_partials(dict(folded, methods=dict(folded["methods"])), reqs,
                      c["region_gap"])
    # max, not the tail's last: a request can arrive with a ts earlier than
    # something already folded, and duration must not go backwards.
    f["t_last"] = max(f["t_last"], reqs[-1].ts)
    f["n_folded"] = n_folded
    return _finalise(f, c)


def _folded_only_signature(folded, c):
    """Everything was folded away -- possible only if the cap is tiny."""
    f = dict(folded, methods=dict(folded["methods"]))
    f["t_last"] = f["t_first"]
    f["n_folded"] = f["n"]
    return _finalise(f, c)


def fold_partials(folded, reqs, region_gap):
    """Absorb `reqs` into a running aggregate so they need not be kept.

    WHY. A session holds one Request per read RPC for as long as it stays open, and
    nothing closed it while reads kept arriving. A sustained read of a single file --
    a library analysis pass, a big copy -- therefore grew that list without limit:
    one observed burst was 2.7 MILLION read events, and the daemon was OOM-killed
    five times in a fortnight against its 1 GB ceiling, losing coverage during
    exactly the periods generating the most events.

    WHAT SURVIVES EXACTLY. Everything classification depends on except one field:
    byte count, request count, duration, rate, coverage, filesize, offsets, span,
    monotonicity, short reads and errors are all either sums, minima or maxima, and
    fold without loss.

    WHAT DEGRADES, and only in one circumstance. The aggregate carries prev_end and
    last_offset across the boundary, so `regions` and `monotonic` come out EXACT for
    requests folded in timestamp order -- verified against the unfolded signature on
    both sequential and random access patterns. The exception is a request that
    arrives with a timestamp EARLIER than something already folded, which happens
    when reads interleave: signature() would have sorted it into place, and a fold
    cannot be re-sorted. Then `monotonic` reflects arrival order rather than
    timestamp order for that one step. `regions` is unaffected, and classify() only
    trusts regions alongside a low monotonic score anyway.
    """
    reqs = sorted(reqs, key=lambda r: r.ts)
    if not reqs:
        return folded
    f = folded or {
        "n": 0, "bytes": 0, "fh": reqs[0].fh, "t_first": reqs[0].ts,
        "t_last": reqs[0].ts,
        "min_offset": None, "max_offset": None, "max_end": 0, "filesize": None,
        "methods": {m: 0 for m in METHODS}, "mono_ok": 0, "mono_n": 0,
        "regions": 0, "prev_end": None, "last_offset": None,
        "short_reads": 0, "errors": 0,
    }
    for r in reqs:
        end = r.offset + (r.delivered or r.requested or 0)
        f["n"] += 1
        if r.delivered:
            f["bytes"] += r.delivered
        f["t_first"] = min(f["t_first"], r.ts)
        f["t_last"] = max(f["t_last"], r.ts)
        f["min_offset"] = (r.offset if f["min_offset"] is None
                           else min(f["min_offset"], r.offset))
        f["max_offset"] = (r.offset if f["max_offset"] is None
                           else max(f["max_offset"], r.offset))
        f["max_end"] = max(f["max_end"], end)
        if r.eof_hint is not None:
            f["filesize"] = (r.eof_hint if f["filesize"] is None
                             else max(f["filesize"], r.eof_hint))
        if r.method in f["methods"]:
            f["methods"][r.method] += 1
        if f["last_offset"] is not None:
            f["mono_n"] += 1
            if r.offset >= f["last_offset"]:
                f["mono_ok"] += 1
        if f["prev_end"] is None:
            f["regions"] = 1
        elif abs(r.offset - f["prev_end"]) > region_gap:
            f["regions"] += 1
        f["prev_end"] = end if f["prev_end"] is None else max(f["prev_end"], end)
        f["last_offset"] = r.offset
        if r.short:
            f["short_reads"] += 1
        if r.status is not None:
            f["errors"] += 1
    return f


def signature(session, cfg=None, folded=None):
    """Per-file access signature. Pure arithmetic over one session's requests.

    `folded` carries the aggregate of requests already discarded to bound memory;
    see fold_partials. Without it this is exactly as it always was.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)

    reqs = sorted(session, key=lambda r: r.ts)
    if folded and reqs:
        return _merged_signature(folded, reqs, c)
    if folded:
        return _folded_only_signature(folded, c)
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


def media_kind(path):
    """-> "audio" | "video" | None. Drives the playback-rate ceiling."""
    if not path:
        return None
    dot = path.rfind(".")
    if dot < 0:
        return None
    ext = path[dot:].lower()
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return None


def classify(sig, cfg=None, media=None):
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

    # Too fast to be playback, and read in order: a copy or an analysis pass. This
    # is deliberately checked BEFORE coverage-based rules, because the expensive
    # mistake was requiring a known file size before COPY could be considered.
    ceiling = c.get(f"max_play_rate_{media}") or c["max_play_rate"]
    if (rate is not None and rate >= ceiling
            and sig["monotonic"] >= c["min_monotonic"]
            and sig["bytes"] >= c["play_min_bytes"]):
        return "COPY"

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
                 "ino", "dev", "client", "uid")

    def __init__(self, ts, kind, xid=0, fh=0, offset=0, length=None,
                 status=None, ino=None, dev=None, client=None, uid=None):
        self.ts, self.kind, self.xid = ts, kind, xid
        self.fh, self.offset, self.length, self.status = fh, offset, length, status
        self.ino, self.dev, self.client = ino, dev, client
        # The client's RPC credential, when the collector could read one. None from
        # ftrace captures, which never carried it.
        self.uid = uid

    @property
    def key(self):
        """What identifies "the same file, from the same reader".

        Prefer (dev, ino, client): it is unambiguous, and including the client
        stops two viewers of one file merging into a nonsense session. Fall back
        to fh_hash when only ftrace-grade data is available, which is exactly
        when that merging becomes unavoidable.

        uid is deliberately NOT part of this. Adding it would split a session
        whenever a credential changed mid-read and, more to the point, would feed
        a different key into the bulk guard -- so it would alter classification,
        not merely annotate it. The uid is carried alongside instead; see _sig.
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
                   client=obj.get("client"), uid=obj.get("uid"))


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
        # (dev, ino) -> arrival record. A download is otherwise invisible: the tap
        # only ever saw reads, so "when did this land" had to come from mtime.
        self._writes = {}
        self.arrivals = 0
        #: How many held requests have been folded away, so the cost is visible.
        self.folded_requests = 0
        self.bytes_written = 0
        self.on_arrival = None
        # Optional (dev, ino) -> "audio"/"video"/None. The tracker has no paths,
        # but the rate ceiling depends on what kind of thing is being read.
        self.media_of = None
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
        if rec.kind in WRITE_KINDS:
            self._ingest_write(rec)
            return
        with self._lock:
            self._ingest(rec)
        # sweep outside the ingest critical section; it dispatches callbacks
        self._sweep(rec.ts)

    def _ingest_write(self, rec):
        """Record an arrival. Writes do not make read-sessions: nothing about a
        write says anything was wanted, which is the only question this asks."""
        key = (rec.dev, rec.ino)
        if rec.ino is None:
            return
        done = []
        with self._lock:
            w = self._writes.get(key)
            if w is None:
                w = {"first": rec.ts, "bytes": 0, "client": rec.client,
                     "uid": rec.uid, "reported": False}
                self._writes[key] = w
                self.arrivals += 1
            w["last"] = rec.ts
            if rec.kind == "write_done" and rec.length:
                w["bytes"] += rec.length
                self.bytes_written += rec.length
            # commit means the client has flushed: as close to "finished" as NFS
            # offers without waiting for silence.
            if rec.kind == "commit_done" and not w["reported"]:
                w["reported"] = True
                done.append((key, dict(w)))
            ttl = self.cfg["write_ttl"]
            for k in [k for k, v in self._writes.items() if rec.ts - v["last"] > ttl]:
                del self._writes[k]
        for key, w in done:
            if self.on_arrival:
                self.on_arrival(key, w)

    def _maybe_fold(self, st):
        """Keep an open session's held requests bounded.

        Folds the OLDEST half rather than one at a time: folding on every arrival
        past the cap would do the work once per read for the rest of the session's
        life, which on a flood is the cost we are trying to avoid.
        """
        cap = int(self.cfg.get("max_open_requests")
                  or DEFAULTS["max_open_requests"])
        if cap <= 0 or len(st["reqs"]) <= cap:
            return
        keep = cap // 2
        drop = st["reqs"][:-keep] if keep else st["reqs"]
        st["folded"] = fold_partials(st["folded"], drop, self.cfg["region_gap"])
        st["reqs"] = st["reqs"][-keep:] if keep else []
        self.folded_requests += len(drop)

    def recent_write(self, dev, ino):
        """When was this file last written? -> ts, or None if not seen recently.

        Arrival is NOT used to classify a read. It was, briefly, and that was wrong
        in both directions: a scan is not tied to when a file landed (one episode
        arrived at 00:00 and was analysed at 03:10), and anchoring on arrival would
        discard the ordinary case of downloading something and watching it twenty
        minutes later. Read RATE decides that; this exists so the arrival itself is
        visible, and so age-on-tier can come from observation rather than mtime.
        """
        with self._lock:
            w = self._writes.get((dev, ino))
        return w["last"] if w else None

    def _ingest(self, rec):
        key = rec.key
        st = self._open.get(key)
        if st is None:
            st = {"reqs": [], "pending": {}, "last": rec.ts,
                  "first": rec.ts, "checkpointed": False, "uids": set(),
                  "folded": None}
            self._open[key] = st
        st["last"] = max(st["last"], rec.ts)
        if rec.uid is not None:
            st["uids"].add(rec.uid)
        self._maybe_fold(st)

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
                    sig = self._sig(key, reqs, st["uids"], st.get("folded"))
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

    def _sig(self, key, reqs, uids=None, folded=None):
        """Build the session signature. `uids` is every credential seen on it.

        Two fields, because one number cannot honestly answer both questions:
        `uid` is the credential ONLY when the session had exactly one, and `uids`
        is the full set. A session with two is real -- two processes on one host
        reading the same file at the same time -- and reporting either of them as
        "the" reader would be a guess. None means "do not attribute this".
        """
        sig = signature(reqs, self.cfg, folded=folded)
        sig["key"] = key
        sig["dev"], sig["ino"] = key[0], key[1]
        sig["client"] = key[2] if len(key) > 2 else None
        seen = sorted(uids or ())
        sig["uids"] = seen
        sig["uid"] = seen[0] if len(seen) == 1 else None
        return sig

    def _note(self, sig):
        """Record this file against its client, and say how many distinct files
        that client touched in the window BEFORE it.

        Counting DISTINCT FILES, not sessions, is the point: one viewer re-reading
        one file in bursts must not look like a sweep, while a scanner walking a
        library touches hundreds of different files in the same period.

        History is kept for twice the window, so the same session can later be
        judged against what arrived AFTER it too -- see files_in_window().
        """
        window = self.cfg["bulk_window"]
        now, client = sig["t_last"], sig.get("client")
        q = self._recent[client]
        q.append((now, (sig.get("dev"), sig.get("ino"), sig.get("fh"))))
        while q and now - q[0][0] > 2 * window:
            q.popleft()
        return len({k for t, k in q if now - t <= window})

    def files_in_window(self, client, ts, half=None):
        """Distinct files this client touched within +/- half of `ts`.

        A rolling window can only ever look backwards, so the first files of a
        sweep are indistinguishable from a genuine play -- there is nothing yet to
        compare them against. Judging a session once the window has passed sees
        the sweep from both sides and removes that blind spot entirely.
        """
        if half is None:
            half = self.cfg["bulk_window"]
        with self._lock:
            q = list(self._recent.get(client, ()))
        return len({k for t, k in q if abs(t - ts) <= half})

    def _pop(self, key):
        """Remove a session and return (key, sig, label, first). Caller holds the
        lock and is responsible for dispatching the callback."""
        st = self._open.pop(key, None)
        if st is None:
            return None                  # another thread closed it first
        reqs = st["reqs"] + list(st["pending"].values())
        if not reqs and not st.get("folded"):
            self.dropped += 1
            return None
        sig = self._sig(key, reqs, st["uids"], st.get("folded"))
        media = None
        if self.media_of:
            try:
                media = self.media_of(sig.get("dev"), sig.get("ino"))
            except Exception:                                   # noqa: BLE001
                media = None
        sig["media"] = media
        label = classify(sig, self.cfg, media)
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
            snapshot = [(k, st["reqs"] + list(st["pending"].values()),
                         set(st["uids"]), st.get("folded"))
                        for k, st in self._open.items()]
        for key, reqs, uids, folded in snapshot:
            if not reqs:
                continue
            sig = self._sig(key, reqs, uids, folded)
            out.append((sig, classify(sig, self.cfg)))
        return out
