#!/usr/bin/env python3
"""Tests for pamts-promote.py. Temp dirs and a fake player only; never contacts a
real media server and never touches real storage.

Run:  python3 tests/test_promote.py
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
import pamts                                                     # noqa: E402
import pamts_players                                             # noqa: E402
from pamts_players import Candidate, Caps, PlayEvent             # noqa: E402

spec = importlib.util.spec_from_file_location("pamts_promote", ROOT / "pamts-promote.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)

if shutil.which("rsync") is None:
    print("SKIP: rsync is not installed. PAMTS drives rsync for every transfer, so the\n"
          "      suite cannot verify behaviour without it. Install rsync and re-run.")
    sys.exit(77)

GB = 1024 ** 3
fails = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def mkfile(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)


class FakePlayer(pamts_players.Player):
    """A player with no server behind it, to drive the engine deterministically."""
    kind = "fake"
    caps = Caps(max_items=3, max_bytes=10 * 1024)

    def __init__(self, events, groups):
        super().__init__({"url": "http://fake", "token_file": "/dev/null"})
        self._events, self._groups = events, groups

    def library_items(self):
        return []

    def now_playing(self):
        return self._events

    def locality_group(self, event):
        return self._groups.get(event.label, [])


def main():
    tmp = tempfile.mkdtemp(prefix="pamts-promote-test-")
    fast = os.path.join(tmp, "fast", "tv")
    slow = os.path.join(tmp, "slow", "tv")
    os.makedirs(fast, exist_ok=True)
    os.makedirs(slow, exist_ok=True)
    # ROOTS is shared state read by pamts.split_root, so set it, don't shadow it.
    pamts.ROOTS = {"/player/tv": (fast, slow)}
    pamts.PATHS = dict(pamts.DEFAULTS["paths"])
    pamts.PATHS["state_file"] = os.path.join(tmp, "state.json")
    pamts.PATHS["promote_log"] = os.path.join(tmp, "promote.log")
    pamts.TIER = dict(pamts.DEFAULTS["tier"])
    pamts.PROMOTE = dict(pamts.DEFAULTS["promote"])
    pamts.setup_logging(pamts.PATHS["promote_log"], False)

    print("\n=== path mapping")
    got = pamts.split_root("/player/tv/Show/Season 1/e01.mkv")
    check(got is not None and got[0] == "Show/Season 1/e01.mkv", "relative path extracted")
    check(pamts.split_root("/nope/x.mkv") is None, "unknown root rejected")
    pamts.ROOTS["/player/tv/special"] = (fast + "2", slow + "2")
    m = pamts.split_root("/player/tv/special/a.mkv")
    check(m is not None and m[1] == fast + "2", "longest prefix wins")
    del pamts.ROOTS["/player/tv/special"]

    print("\n=== filter_candidates: what gets skipped")
    for i in range(1, 7):
        mkfile(os.path.join(slow, "Show", "Season 1", f"e{i:02d}.mkv"), 2 * 1024)
    mkfile(os.path.join(fast, "Show", "Season 1", "e02.mkv"), 2 * 1024)   # already local
    mkfile(os.path.join(slow, "Show", "Season 1", "e07.mkv.part"), 512)   # in progress
    cands = [Candidate(path=f"/player/tv/Show/Season 1/e{i:02d}.mkv",
                       label=f"E{i:02d}", size=2 * 1024, order=i) for i in range(1, 7)]
    cands.append(Candidate(path="/player/tv/Show/Season 1/e07.mkv.part",
                           label="E07", size=512, order=7))
    cands.append(Candidate(path="/player/tv/Show/Season 1/e99.mkv",
                           label="E99", size=2 * 1024, order=99))   # on neither tier
    sel = promote.filter_candidates(cands, Caps(10, 10 ** 9), 10 ** 9)
    rels = [s["rel"] for s in sel]
    check("Show/Season 1/e02.mkv" not in rels, "already-local file skipped")
    check(not any(r.endswith(".part") for r in rels), "in-progress marker skipped")
    check("Show/Season 1/e99.mkv" not in rels, "file absent from slow storage skipped")
    check(len(sel) == 5, f"5 of 8 candidates selected (got {len(sel)})")

    print("\n=== caps")
    check(len(promote.filter_candidates(cands, Caps(2, 10 ** 9), 10 ** 9)) == 2,
          "max_items honoured")
    check(sum(s["size"] for s in promote.filter_candidates(
        cands, Caps(10, 5 * 1024), 10 ** 9)) <= 5 * 1024, "max_bytes honoured")
    check(sum(s["size"] for s in promote.filter_candidates(
        cands, Caps(10, 10 ** 9), 3 * 1024)) <= 3 * 1024, "budget headroom honoured")
    check(len(promote.filter_candidates(cands, Caps(10, 10 ** 9), 0)) == 0,
          "zero headroom promotes nothing")

    print("\n=== play order is respected")
    sel = promote.filter_candidates(cands, Caps(2, 10 ** 9), 10 ** 9)
    check([s["label"] for s in sel] == ["E01", "E03"],
          f"lowest order first, skipping the already-local E02 "
          f"(got {[s['label'] for s in sel]})")

    print("\n=== copy_items copies and does NOT remove the source")
    sel = promote.filter_candidates(
        [Candidate(path="/player/tv/Show/Season 1/e03.mkv", label="E03",
                   size=2 * 1024, order=3)], Caps(5, 10 ** 9), 10 ** 9)
    check(sel and sel[0]["group_dir"] == os.path.join(fast, "Show", "Season 1"),
          "protection is recorded against the containing directory, the unit "
          "tiering evicts")
    check(promote.copy_items(sel, dry_run=False), "copy reported success")
    check(os.path.exists(os.path.join(fast, "Show", "Season 1", "e03.mkv")),
          "file now on fast storage")
    check(os.path.exists(os.path.join(slow, "Show", "Season 1", "e03.mkv")),
          "STILL on slow storage -- promotion copies, never moves")

    print("\n=== dry-run writes nothing")
    sel = promote.filter_candidates(
        [Candidate(path="/player/tv/Show/Season 1/e04.mkv", label="E04",
                   size=2 * 1024, order=4)], Caps(5, 10 ** 9), 10 ** 9)
    promote.copy_items(sel, dry_run=True)
    check(not os.path.exists(os.path.join(fast, "Show", "Season 1", "e04.mkv")),
          "dry-run did not create the file")

    print("\n=== TIME BUDGET: only promote what can arrive before this item ends")
    rate = 100 * 1024 ** 2
    base = Caps(max_items=24, max_bytes=60 * GB)
    c = promote.effective_caps(base, 400 * GB, 24 * 60, rate)
    expect = min(24 * 60 * rate * float(pamts.PROMOTE["time_safety"]), 60 * GB)
    check(abs(c.max_bytes - expect) < 1024 ** 2,
          f"24 min at 100MB/s -> {pamts.human(c.max_bytes)}")
    short = promote.effective_caps(base, 400 * GB, 120, rate)
    check(short.max_bytes < c.max_bytes, "less time left means a smaller allowance")
    floor = int(pamts.PROMOTE["min_event_bytes_gb"]) * GB
    check(short.max_bytes >= floor,
          "but never below the floor -- the next item is always attempted")
    check(short.max_items >= int(pamts.PROMOTE["min_event_items"]), "item floor holds")
    notime = promote.effective_caps(base, 400 * GB, 0, rate)
    check(notime.max_bytes == min(
        int(400 * GB * float(pamts.PROMOTE["headroom_fraction"])), 60 * GB),
        f"unknown duration applies no time limit ({pamts.human(notime.max_bytes)})")

    print("\n=== SPACE BUDGET: generous when empty, conservative when full")
    empty = promote.effective_caps(base, 400 * GB, 0, rate)
    tight = promote.effective_caps(base, 10 * GB, 0, rate)
    check(empty.max_bytes > tight.max_bytes, "a fuller tier promotes less")
    check(empty.max_items >= tight.max_items, "and fewer items")
    check(tight.max_bytes >= floor, "tight still respects the floor")
    check(empty.max_items <= base.max_items, "never exceeds the adapter ceiling")
    check(empty.max_bytes <= base.max_bytes, "never exceeds the byte ceiling")
    check(promote.effective_caps(base, 0, 0, rate).max_bytes >= floor,
          "zero headroom floors rather than crashing")

    print("\n=== THROUGHPUT estimate self-tunes")
    st = {}
    promote.record_rate(st, {"bytes": 1000 * 1024 ** 2, "seconds": 10.0})
    check(abs(st["rate_bps"] - 100 * 1024 ** 2) < 1024, "first measurement taken as-is")
    promote.record_rate(st, {"bytes": 200 * 1024 ** 2, "seconds": 10.0})
    check(20 * 1024 ** 2 < st["rate_bps"] < 100 * 1024 ** 2,
          f"later measurements smoothed, not replaced ({pamts.human(st['rate_bps'])}/s)")
    before = st["rate_bps"]
    promote.record_rate(st, {"bytes": 0, "seconds": 0})
    check(st["rate_bps"] == before, "an empty pass does not corrupt the estimate")

    print("\n=== state round-trips")
    pamts.save_state({"promotions": {"X": {"at": 1.0}}}, dry_run=False)
    check(pamts.load_state()["promotions"].get("X", {}).get("at") == 1.0, "state persisted")
    pamts.save_state({"promotions": {"Y": {"at": 2.0}}}, dry_run=True)
    check("Y" not in pamts.load_state()["promotions"], "dry-run did not write state")
    now = time.time()
    stt = {"promotions": {"Fresh": {"at": now - 3600},
                          "Old": {"at": now - (int(pamts.TIER["promote_protect_days"]) + 1) * 86400}}}
    check(pamts.prune_state(stt, now) == 1, "one expired record dropped")
    check("Fresh" in stt["promotions"] and "Old" not in stt["promotions"],
          "fresh kept, expired removed")

    print("\n=== a film yields no locality group (reactive film promotion is pointless)")
    plex = pamts_players.PlexPlayer({"url": "http://127.0.0.1:1",
                                     "token_file": "/nonexistent"})
    check(plex.locality_group(PlayEvent(source="plex", kind="movie", label="Film",
                                        path="/player/tv/x.mkv", group=None)) == [],
          "film -> no candidates")
    check(plex.locality_group(PlayEvent(source="plex", kind="episode", label="S",
                                        path="/x", group=None)) == [],
          "episode with no group handle -> no candidates")

    print("\n=== adapter registry and availability")
    check(pamts_players.build({"kind": "plex", "url": "http://x",
                               "token_file": "/t"}).kind == "plex",
          "registry builds the configured adapter")
    try:
        pamts_players.build({"kind": "nope"})
        check(False, "unknown adapter kind is refused")
    except pamts.ConfigError:
        check(True, "unknown adapter kind is refused")
    check(pamts_players.PlexPlayer({"url": "http://x", "token_file": "/nonexistent"})
          .available() is False, "a missing token file reports unavailable")
    check(pamts_players.PlexPlayer({"url": "", "token_file": "/t"})
          .available() is False, "a missing url reports unavailable")

    print("\n=== fake player drives the engine end to end")
    fp = FakePlayer(
        events=[PlayEvent(source="fake", kind="episode", label="Show S01E01",
                          path="/player/tv/Show/Season 1/e01.mkv", group={})],
        groups={"Show S01E01": [
            Candidate(path=f"/player/tv/Show/Season 1/e{i:02d}.mkv",
                      label=f"E{i:02d}", size=2 * 1024, order=i) for i in (5, 6)]})
    ev = fp.now_playing()[0]
    sel = promote.filter_candidates(fp.locality_group(ev), fp.caps, 10 ** 9)
    check(len(sel) == 2 and promote.copy_items(sel, dry_run=False),
          "fake player promoted its group")
    check(all(os.path.exists(os.path.join(fast, "Show", "Season 1", f"e{i:02d}.mkv"))
              for i in (5, 6)), "both files landed on fast storage")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n=== {len(fails)} failure(s)")
    for f in fails:
        print(f"    {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
