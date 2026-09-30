#!/usr/bin/env python3
"""Tests for pamts_observer.py. Pure arithmetic over synthetic trace text plus a
few signatures taken from a real 10-minute capture; contacts nothing.

The cases that matter most:
  - splice + done for one xid must count as ONE read, not two (double counting
    every read makes the classifier think all traffic is playback)
  - xid wrap must not fuse two unrelated reads
  - a scan must never be labelled PLAY, because PLAY drives promotion

Run:  python3 tests/test_observer.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _harness import check, summary, skip                       # noqa: E402

import pamts_observer as obs                                    # noqa: E402

MB = obs.MB


def line(ts, tp, xid, fh, offset, length=None, status=None):
    tail = f"len={length}" if length is not None else f"status={status}"
    return (f"            nfsd-1929462 [006] ..... {ts:.6f}: nfsd_{tp}: "
            f"xid={xid:#010x} fh_hash={fh:#010x} offset={offset} {tail}")


def read(ts, xid, fh, offset, requested, delivered=None, method="splice"):
    """The trace lines one complete read RPC produces."""
    delivered = requested if delivered is None else delivered
    return [line(ts, f"read_{method}", xid, fh, offset, requested),
            line(ts + 0.0007, "read_done", xid, fh, offset, delivered)]


# --- parsing -----------------------------------------------------------------
print("parsing")
ev = obs.parse_line(line(1028494.346530, "read_splice", 0x305fdf28, 0x3d50c855,
                         0, 131072))
check("parses a splice line", ev is not None and ev.tp == "read_splice")
check("parses fh_hash as int", ev is not None and ev.fh == 0x3d50c855)
check("parses offset and len", ev is not None and ev.offset == 0 and ev.length == 131072)
check("parses the timestamp", ev is not None and abs(ev.ts - 1028494.346530) < 1e-6)

err = obs.parse_line(line(1.0, "read_err", 1, 2, 4096, status=-5))
check("parses read_err without len", err is not None and err.status == -5 and err.length is None)

check("ignores unrelated lines", obs.parse_line("some other ftrace line") is None)
check("ignores a non-read nfsd line",
      obs.parse_line("  nfsd-1 [000] ..... 1.0: nfsd_file_put: xid=0x1") is None)

# --- the double-count guard --------------------------------------------------
print("\ndouble counting (splice + done are the same read)")
reqs = obs.join_requests(obs.parse(read(1.0, 0xAA, 0xF1, 0, 131072)))
check("splice+done collapse to one request", len(reqs) == 1, f"got {len(reqs)}")
check("bytes counted once", reqs and reqs[0].delivered == 131072)
check("method is retained as a feature", reqs and reqs[0].method == "splice")

sig = obs.signature(obs.sessionise(reqs)[0])
check("one read is not two", sig["bytes"] == 131072, f"got {sig['bytes']}")
check("method mix recorded", sig["frac_splice"] == 1.0 and sig["method"] == "splice")

# vector and direct are kept distinct from splice
for m in ("vector", "direct"):
    r = obs.join_requests(obs.parse(read(1.0, 0xBB, 0xF2, 0, 4096, method=m)))
    check(f"{m} method recorded", r and r[0].method == m)

# --- xid wrap ----------------------------------------------------------------
print("\nxid wrap must not fuse unrelated reads")
lines = read(1.0, 0xC0FFEE, 0xF3, 0, 131072)
lines += read(1.0 + obs.XID_WINDOW + 5, 0xC0FFEE, 0xF3, 999999, 131072)
reqs = obs.join_requests(obs.parse(lines))
check("same xid far apart = two requests", len(reqs) == 2, f"got {len(reqs)}")

# a start with no completion is not silently dropped
orphan = obs.join_requests(obs.parse([line(1.0, "read_start", 0xD1, 0xF4, 0, 8192)]))
check("an uncompleted start is kept", len(orphan) == 1)

# --- EOF / coverage ----------------------------------------------------------
print("\nEOF detection gives coverage without resolving the path")
lines = []
for i in range(4):
    lines += read(1.0 + i * 0.01, 0x100 + i, 0xF5, i * 131072, 131072)
lines += read(1.05, 0x104, 0xF5, 4 * 131072, 131072, delivered=4096)   # short => EOF
reqs = obs.join_requests(obs.parse(lines))
check("short read detected", sum(1 for r in reqs if r.short) == 1)
sig = obs.signature(obs.sessionise(reqs)[0])
check("filesize inferred from EOF", sig["filesize"] == 4 * 131072 + 4096,
      f"got {sig['filesize']}")
check("coverage computed", sig["coverage"] is not None and abs(sig["coverage"] - 1.0) < 0.01)
check("no EOF seen => coverage unknown",
      obs.signature(obs.sessionise(obs.join_requests(obs.parse(
          read(1.0, 0x1, 0xF6, 0, 131072))))[0])["coverage"] is None)

# --- sessionising ------------------------------------------------------------
print("\nsessionising")
lines = read(1.0, 0x1, 0xF7, 0, 4096) + read(500.0, 0x2, 0xF7, 4096, 4096)
s = obs.sessionise(obs.join_requests(obs.parse(lines)), idle_gap=30.0)
check("a long gap splits the session", len(s) == 2, f"got {len(s)}")
s = obs.sessionise(obs.join_requests(obs.parse(lines)), idle_gap=1000.0)
check("a large idle_gap keeps one session", len(s) == 1)
lines = read(1.0, 0x1, 0xA1, 0, 4096) + read(1.1, 0x2, 0xA2, 0, 4096)
check("different files are different sessions",
      len(obs.sessionise(obs.join_requests(obs.parse(lines)))) == 2)

# --- classification ----------------------------------------------------------
print("\nclassification")


def label(lines, cfg=None):
    reqs = obs.join_requests(obs.parse(lines))
    return obs.classify(obs.signature(obs.sessionise(reqs)[0], cfg), cfg)


# paced sequential video: 600 s, ~3 MB/s, partial coverage
lines = []
for i in range(600):
    lines += read(1.0 + i, 0x200 + i, 0xB1, i * 3 * MB, 131072)
lines += read(700.0, 0x999, 0xB1, 600 * 3 * MB, 131072, delivered=4096)
check("paced sequential stream is PLAY", label(lines) == "PLAY", label(lines))

# head+tail sip of a 40 MB file
lines = (read(1.0, 0x1, 0xB2, 0, 131072) +
         read(1.1, 0x2, 0xB2, 131072, 131072) +
         read(1.2, 0x3, 0xB2, 40 * MB - 131072, 131072, delivered=65536))
check("head/tail probe is PROBE", label(lines) == "PROBE", label(lines))

# a large file read straight through at wire speed: 105 MB in 0.8 s = 131 MB/s
lines = []
for i in range(800):
    lines += read(1.0 + i * 0.001, 0x300 + i, 0xB3, i * 131072, 131072)
lines += read(1.81, 0x998, 0xB3, 800 * 131072, 131072, delivered=1024)
sig = obs.signature(obs.sessionise(obs.join_requests(obs.parse(lines)))[0])
check("large fast read really is large and fast",
      sig["bytes"] > 64 * MB and sig["rate"] > 40 * MB,
      f"{sig['bytes']/MB:.0f} MB at {sig['rate']/MB:.0f} MB/s")
check("whole file at wire speed is COPY", label(lines) == "COPY", label(lines))

# whole small file, fast but not wire-speed: genuinely ambiguous
lines = []
for i in range(40):
    lines += read(1.0 + i * 0.0075, 0x400 + i, 0xB4, i * 131072, 131072)
lines += read(1.31, 0x997, 0xB4, 40 * 131072, 131072, delivered=8192)
check("whole small file, fast, is FETCH not PLAY", label(lines) == "FETCH", label(lines))

# the two regressions found on real data
single = read(1.0, 0x1, 0xB5, 0, 400 * 1024, delivered=400 * 1024)
single += [line(1.0008, "read_done", 0x1, 0xB5, 0, 4096)]   # short => EOF known
check("a single read with no elapsed time is never PLAY",
      label(single) != "PLAY", label(single))
check("rate is None when duration is zero",
      obs.signature(obs.sessionise(obs.join_requests(obs.parse(
          read(1.0, 0x1, 0xB6, 0, 4096))))[0])["rate"] is None)

# errors
e = [line(1.0, "read_start", 0x1, 0xB7, 0, 8192), line(1.001, "read_err", 0x1, 0xB7, 0, status=-5)]
check("a failed read is ERROR", label(e) == "ERROR", label(e))

# --- the bulk guard ----------------------------------------------------------
print("\nbulk sweep guard")


def whole_file_sig(fh, t):
    lines = []
    for i in range(20):
        lines += read(t + i * 0.01, (fh << 8) + i, fh, i * 131072, 131072)
    lines += read(t + 0.25, (fh << 8) + 99, fh, 20 * 131072, 131072, delivered=2048)
    return obs.signature(obs.sessionise(obs.join_requests(obs.parse(lines)))[0])


crowd = [whole_file_sig(0xC000 + i, 1.0 + i * 0.5) for i in range(40)]
check("40 whole-file reads in 20 s are flagged bulk",
      len(obs.detect_bulk(crowd)) >= 40, f"flagged {len(obs.detect_bulk(crowd))}")

lone = [whole_file_sig(0xD000, 1.0)]
check("a lone whole-file read is not bulk", len(obs.detect_bulk(lone)) == 0)

spread = [whole_file_sig(0xE000 + i, 1.0 + i * 600) for i in range(40)]
check("whole-file reads spread over hours are not bulk",
      len(obs.detect_bulk(spread)) == 0, f"flagged {len(obs.detect_bulk(spread))}")

check("analyse handles empty input", obs.analyse([]) == [])

# --- regression: signatures measured on a real 10 minute capture -------------
print("\nreal-capture regressions")
real_stream = {"fh": 0x757f50ab, "t_first": 0.0, "t_last": 591.9, "duration": 591.9,
               "requests": 15369, "bytes": 1935 * MB, "filesize": int(9214 * MB),
               "coverage": 0.21, "min_offset": 0, "max_offset": 9000 * MB,
               "span": 9000 * MB, "monotonic": 0.99, "regions": 6574,
               "starts_at_zero": True, "short_reads": 1, "errors": 0,
               "rate": 3.27 * MB, "frac_splice": 1.0, "frac_vector": 0.0,
               "frac_direct": 0.0, "method": "splice"}
check("the real 26 Mbps stream is PLAY", obs.classify(real_stream) == "PLAY",
      obs.classify(real_stream))

real_probe = {"fh": 0x56fac01f, "t_first": 0.0, "t_last": 0.3, "duration": 0.3,
              "requests": 5, "bytes": int(0.4 * MB), "filesize": 40 * MB,
              "coverage": 0.01, "min_offset": 0, "max_offset": 40 * MB,
              "span": 40 * MB, "monotonic": 1.0, "regions": 2,
              "starts_at_zero": True, "short_reads": 1, "errors": 0,
              "rate": 1.71 * MB, "frac_splice": 1.0, "frac_vector": 0.0,
              "frac_direct": 0.0, "method": "splice"}
check("the real head/tail probe is PROBE", obs.classify(real_probe) == "PROBE",
      obs.classify(real_probe))

# A playback-start prefill burst, measured live. It fell through every class:
# 14.3% coverage missed play_min_coverage (0.15) and 10s missed
# play_long_duration (30), so it scored UNKNOWN -- and now_playing() filters on
# PLAY, so promotion ignored the exact moment an episode started.
real_prefill = {"fh": 0x1234, "t_first": 0.0, "t_last": 10.0, "duration": 10.0,
                "requests": 1663, "bytes": int(207.8 * MB),
                "filesize": int(1453 * MB), "coverage": 0.143, "min_offset": 0,
                "max_offset": 207 * MB, "span": 207 * MB, "monotonic": 1.00,
                "regions": 1, "starts_at_zero": True, "short_reads": 0,
                "errors": 0, "rate": 20.7 * MB, "frac_splice": 1.0,
                "frac_vector": 0.0, "frac_direct": 0.0, "method": "splice"}
check("a real playback-start prefill burst is PLAY",
      obs.classify(real_prefill) == "PLAY", obs.classify(real_prefill))
check("a large sequential read qualifies on bytes alone",
      obs.classify(dict(real_prefill, coverage=0.02, duration=8.0)) == "PLAY",
      obs.classify(dict(real_prefill, coverage=0.02, duration=8.0)))
check("but the same volume read SCATTERED is not PLAY",
      obs.classify(dict(real_prefill, monotonic=0.3, regions=40)) != "PLAY",
      obs.classify(dict(real_prefill, monotonic=0.3, regions=40)))
check("and a volume under the floor still needs coverage or duration",
      obs.classify(dict(real_prefill, bytes=60 * MB, coverage=0.05,
                        duration=6.0, rate=10 * MB)) != "PLAY")
check("a whole file at wire speed is still COPY, not PLAY",
      obs.classify(dict(real_prefill, bytes=8000 * MB, coverage=1.0,
                        duration=40.0, rate=200 * MB)) == "COPY")

# Every row below is a real session measured on a live server. The rate spread is
# the whole point: nothing that is actually playback needs 30 MB/s sustained, and a
# media server analysing a freshly downloaded file runs at 38-110.
def measured(b, dur, rate, cov=None, mono=1.0):
    return {"fh": 0, "t_first": 0.0, "t_last": dur, "duration": dur,
            "requests": max(1, int(b / 131072)), "bytes": int(b),
            "filesize": None if cov is None else int(b / cov), "coverage": cov,
            "min_offset": 0, "max_offset": int(b), "span": int(b),
            "monotonic": mono, "regions": 1, "starts_at_zero": True,
            "short_reads": 0, "errors": 0, "rate": rate, "frac_splice": 1.0,
            "frac_vector": 0.0, "frac_direct": 0.0, "method": "splice"}


for _name, _sig, _want in (
        ("music FLAC at 0.2 MB/s", measured(21.4 * MB, 137, 0.2 * MB, 1.0), "PLAY"),
        ("2160p video at 3.1 MB/s", measured(3432 * MB, 1119, 3.1 * MB, 0.35), "PLAY"),
        ("a buffer refill at 9.6 MB/s", measured(553 * MB, 58, 9.6 * MB, 0.056), "PLAY"),
        ("a playback prefill at 20.7 MB/s", measured(207.8 * MB, 10, 20.7 * MB, 0.143), "PLAY"),
        # this one scored PLAY before max_play_rate: 534 MB in 14 SECONDS
        ("an analysis pass at 37.9 MB/s", measured(534.8 * MB, 14, 37.9 * MB), "COPY"),
        # these scored UNKNOWN before, purely because no EOF had been seen
        ("a whole episode at 96.9 MB/s", measured(1476 * MB, 15, 96.9 * MB), "COPY"),
        ("an analysis chunk at 98 MB/s", measured(165 * MB, 2, 98 * MB), "COPY"),
        ("a whole file at 109.8 MB/s", measured(1051 * MB, 10, 109.8 * MB, 1.0), "COPY")):
    check(f"measured: {_name} is {_want}", obs.classify(_sig) == _want,
          obs.classify(_sig))

check("COPY no longer needs a known file size",
      obs.classify(measured(800 * MB, 8, 100 * MB)) == "COPY")
check("but a fast SCATTERED read is not a copy",
      obs.classify(measured(800 * MB, 8, 100 * MB, mono=0.2)) != "COPY")
check("and the ceiling sits above every measured playback rate",
      obs.DEFAULTS["max_play_rate"] > 20.7 * MB
      and obs.DEFAULTS["max_play_rate"] < 37.9 * MB,
      f"{obs.DEFAULTS['max_play_rate'] / MB:.0f} MB/s")

real_music = dict(real_stream, fh=0xd1721f9f, duration=105.0, requests=57,
                  bytes=int(7.1 * MB), filesize=int(7.1 * MB), coverage=1.0,
                  monotonic=1.0, regions=1, rate=0.07 * MB)
check("a real-time music track is PLAY", obs.classify(real_music) == "PLAY",
      obs.classify(real_music))

# --- online tracking ---------------------------------------------------------
print("\nonline tracking (SessionTracker)")


def stream(n, ino=7, dev=45, client="10.0.0.1", t0=1000.0, step=1.0,
           cfg=None, tick_at=None):
    ev = []
    t = obs.SessionTracker(cfg or {"idle_gap": 20.0, "checkpoint_after": 60.0},
                           on_close=lambda k, s, l, f: ev.append((l, f, s)))
    for i in range(n):
        for kind in ("read_start", "read_splice", "read_done"):
            t.add(obs.Record(ts=t0 + i * step, kind=kind, xid=0x100 + i,
                             offset=i * 3 * MB, length=131072,
                             ino=ino, dev=dev, client=client))
    if tick_at is not None:
        t.tick(tick_at)
    return t, ev


check("eBPF-grade key uses (dev, ino, client)",
      obs.Record(ts=1.0, kind="read_done", ino=42, dev=7, client="a").key == (7, 42, "a"))
check("ftrace-grade key falls back to fh_hash",
      obs.Record(ts=1.0, kind="read_done", fh=0xabc).key == (None, 0xabc, None))
t = obs.SessionTracker({"idle_gap": 20.0})
for cl in ("10.0.0.1", "10.0.0.2"):
    t.add(obs.Record(ts=1000.0, kind="read_done", xid=1, offset=0, length=4096,
                     ino=7, dev=45, client=cl))
check("distinct clients are distinct sessions", len(t._open) == 2, f"{len(t._open)}")

tr, ev = stream(400, tick_at=1000.0 + 400 + 25)
check("a long play is reported mid-flight", tr.checkpoints == 1, f"{tr.checkpoints}")
check("a long play is reported twice in total", len(ev) == 2, f"{len(ev)}")
check("the mid-flight report carries the increment", ev[0][1] is True)
check("the final report does not re-count", ev[1][1] is False)
check("increments total exactly one", sum(1 for e in ev if e[1]) == 1)
check("the final report has the full duration", round(ev[1][2]["duration"]) == 399,
      str(round(ev[1][2]["duration"])))

tr, ev = stream(40, tick_at=1000.0 + 40 + 25)
check("a brief play is not checkpointed", tr.checkpoints == 0, f"{tr.checkpoints}")
check("a brief play is reported once", len(ev) == 1, f"{len(ev)}")
check("a brief play still counts once", sum(1 for e in ev if e[1]) == 1)

tr, ev = stream(400, cfg={"idle_gap": 20.0, "checkpoint_after": 0},
                tick_at=1000.0 + 400 + 25)
check("checkpointing can be disabled", tr.checkpoints == 0 and len(ev) == 1,
      f"cp={tr.checkpoints} reports={len(ev)}")

tr, _ = stream(5, tick_at=None)
check("an active session stays open", len(tr._open) == 1)
check("open_sessions exposes it", len(tr.open_sessions) == 1)
tr.flush()
check("flush closes everything", len(tr._open) == 0)

# a scan must not reach history even via the online path
ev = []
t = obs.SessionTracker({"idle_gap": 20.0}, on_close=lambda k, s, l, f: ev.append(l))
for i in range(2):
    for kind in ("read_start", "read_splice", "read_done"):
        t.add(obs.Record(ts=1000.0 + i * 0.05, kind=kind, xid=0x200 + i,
                         offset=i * 131072, length=131072, ino=9, dev=45, client="c"))
for kind, ln in (("read_start", 131072), ("read_splice", 131072), ("read_done", 8192)):
    t.add(obs.Record(ts=1000.2, kind=kind, xid=0x299, offset=40 * MB - 131072,
                     length=ln, ino=9, dev=45, client="c"))
t.flush()
check("an online head/tail scan is PROBE, not PLAY", ev == ["PROBE"], str(ev))

# --- the founding constraint, against patterns measured on a real server ------
print("\nsweeps must never reach play history")


def sess(t, ino, client, nbytes, dur, nreq, filesize, hit_eof=True):
    """Records for one session. hit_eof emits a SHORT final read, which is what
    reveals the file size and hence coverage. Omit it and the classifier sees
    coverage=None and behaves quite differently -- an earlier version of this
    test emitted no EOF and so never reproduced the labels it claimed to test."""
    recs, per = [], max(1, nbytes // max(1, nreq))
    for i in range(nreq):
        ts = t + (i * dur / max(1, nreq))
        for k in ("read_start", "read_splice", "read_done"):
            recs.append(obs.Record(ts=ts, kind=k, xid=(ino << 9 | i) & 0xffffffff,
                                   offset=i * per, length=per, ino=ino, dev=45,
                                   client=client))
    if hit_eof:
        ts = t + dur
        for k, ln in (("read_start", per), ("read_splice", per),
                      ("read_done", max(1, per // 4))):
            recs.append(obs.Record(ts=ts, kind=k, xid=(ino << 9 | 511) & 0xffffffff,
                                   offset=filesize - per, length=ln, ino=ino,
                                   dev=45, client=client))
    return recs


def labels_for(records, cfg=None):
    out = []
    tr = obs.SessionTracker(cfg or {"idle_gap": 30.0, "checkpoint_after": 0},
                            on_close=lambda k, s, l, f: out.append(l))
    for r in sorted(records, key=lambda r: r.ts):
        tr.add(r)
    tr.flush()
    return out, tr


# 1,350 .jpg files reached play history on a live run: tiny whole-file reads look
# exactly like a FETCH, and FETCH counts as demand.
art = []
for i in range(80):
    art += sess(4000.0 + i * 0.5, 9000 + i, "10.0.0.11", 40 * 1024, 0.05, 1, 40 * 1024)
unguarded, _ = labels_for(art, {"idle_gap": 30.0, "checkpoint_after": 0,
                                "bulk_min_files": 10 ** 9})
check("tiny whole-file reads DO score FETCH (the pattern that leaked)",
      unguarded.count("FETCH") == 80, str(unguarded.count("FETCH")))
guarded, tr = labels_for(art)
check("the bulk guard suppresses most of a storm", tr.bulk >= 50, str(tr.bulk))
check("but a rolling window cannot catch the first few",
      guarded.count("FETCH") > 0,
      "documented limitation: the extension filter is what fully handles artwork")
check("is_media rejects artwork and playlists",
      not obs.is_media("/m/cover.jpg") and not obs.is_media("/m/x.m3u")
      and not obs.is_media("/m/f.png"))
check("is_media accepts video and audio",
      obs.is_media("/m/a.mkv") and obs.is_media("/m/b.flac")
      and obs.is_media("/m/c.m4b"))
check("is_media is case-insensitive", obs.is_media("/m/A.MKV"))
check("is_media handles an extensionless path", not obs.is_media("/m/noext"))

# Measured: ten tracks read simultaneously, 1.4 MB each over 27s, from one client.
# Nobody plays ten tracks at once; it is an analysis pass.
yard = []
for i in range(10):
    yard += sess(1003.0, 6000 + i, "10.0.0.102", int(1.4 * MB), 27.0, 12, int(7 * MB))
alone, _ = labels_for(yard)
check("ten simultaneous partial reads score PLAY unguarded",
      alone.count("PLAY") == 10, str(alone.count("PLAY")))
mixed = yard + [r for i in range(60)
                for r in sess(1000.0 + i * 0.1, 5000 + i, "10.0.0.102",
                              400 * 1024, 0.3, 3, 40 * MB)]
inwave, tr = labels_for(mixed)
check("inside a scan wave they are suppressed as BULK",
      inwave.count("BULK") == 10 and inwave.count("PLAY") == 0,
      f"bulk={inwave.count('BULK')} play={inwave.count('PLAY')}")
check("the scan itself is still PROBE, not BULK",
      inwave.count("PROBE") == 60, str(inwave.count("PROBE")))

print("")
print("thread safety (a daemon closes sessions from two threads)")
import sys as _sys                                              # noqa: E402
import threading as _th                                         # noqa: E402

# A live daemon crashed here under a library scan: the ingest loop and the idle
# timer both sweep _open, giving "dictionary changed size during iteration" and a
# KeyError when both raced to close the same session.
#
# Reproducing it needs MANY open sessions, so the sweep's comprehension is long
# enough to be preempted, and an aggressive switch interval. A gentler version of
# this test passed happily against the unlocked code and proved nothing.
_errors = []
_old_switch = _sys.getswitchinterval()
_sys.setswitchinterval(1e-6)
_tr = obs.SessionTracker({"idle_gap": 5.0, "checkpoint_after": 0},
                         on_close=lambda k, s, l, f: None)
for _i in range(6000):                       # a big population of open sessions
    _tr.add(obs.Record(ts=1000.0, kind="read_done", xid=_i & 0xffff, offset=0,
                       length=4096, ino=90000 + _i, dev=45, client="10.0.0.1"))
_stop = _th.Event()


def _churn():
    try:
        i = 0
        while not _stop.is_set():
            i += 1
            _tr.add(obs.Record(ts=1000.0 + (i % 3) * 0.001, kind="read_done",
                               xid=i & 0xffff, offset=0, length=4096,
                               ino=500000 + i, dev=45, client="10.0.0.2"))
    except Exception as e:                                      # noqa: BLE001
        _errors.append(("churn", repr(e)))


def _reap():
    try:
        t = 1000.0
        while not _stop.is_set():
            t += 0.5
            _tr.tick(t)                      # ages sessions out under the churn
    except Exception as e:                                      # noqa: BLE001
        _errors.append(("reaper", repr(e)))


_threads = [_th.Thread(target=_churn), _th.Thread(target=_reap),
            _th.Thread(target=_reap)]
for _t in _threads:
    _t.start()
_time_mod = __import__("time")
_time_mod.sleep(1.5)
_stop.set()
for _t in _threads:
    _t.join(timeout=10)
_sys.setswitchinterval(_old_switch)
check("concurrent add() and tick() do not race", not _errors, str(_errors[:2]))
_tr.flush()
check("everything is closed after flush", len(_tr._open) == 0, str(len(_tr._open)))
check("open_sessions is safe to read concurrently",
      isinstance(_tr.open_sessions, list))

print("")
print("and legitimate use must survive the guard")
ep = []
for b in range(6):
    ep += sess(2000.0 + b * 200, 7777, "10.0.0.20", 300 * MB, 60.0, 2400,
               9000 * MB, hit_eof=False)
out, tr = labels_for(ep)
check("one episode across six buffer bursts is one PLAY",
      out == ["PLAY"], str(out))
check("a single viewer is never bulk", tr.bulk == 0)

alb = []
for i in range(10):
    alb += sess(3000.0 + i * 240, 8000 + i, "10.0.0.11", int(35 * MB), 230.0,
                280, int(35 * MB))
out, tr = labels_for(alb)
check("an album played track by track is ten PLAYs",
      out.count("PLAY") == 10, str(out.count("PLAY")))
check("playing an album is not a sweep", tr.bulk == 0, str(tr.bulk))

# distinct FILES, not sessions: one file re-read in bursts must not look bulky
many = []
for b in range(40):
    many += sess(5000.0 + b * 2, 4242, "10.0.0.20", 80 * MB, 1.0, 640,
                 9000 * MB, hit_eof=False)
_, tr = labels_for(many, {"idle_gap": 1.5, "checkpoint_after": 0})
check("40 bursts on ONE file are not a sweep", tr.bulk == 0, str(tr.bulk))
summary()
