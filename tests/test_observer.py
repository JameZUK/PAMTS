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
import pamts_observer as obs                                    # noqa: E402

MB = obs.MB
fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))
        fails.append(name)


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

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
