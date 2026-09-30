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
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
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


from _harness import check as _check, summary                  # noqa: E402


def check(cond, msg):
    """This suite has always taken (cond, msg), the opposite of every other one.

    Rather than rewrite ~100 call sites and risk transcribing one wrongly, the
    order is adapted here and the shared harness does the asserting -- so output
    and counting are identical everywhere, and the harness still rejects a call
    whose arguments are the wrong way round.
    """
    return _check(msg, cond)


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

    print("\n=== LMS locality: position is found by track id, not a guessed tag")
    lms = pamts_players.LmsPlayer({"name": "lms", "url": "http://unused"})
    album = [
        {"id": 11, "tracknum": "1", "filesize": "100",
         "url": "file:///player/tv/Alb/01%20a.flac"},
        {"id": 12, "tracknum": "2", "filesize": "200",
         "url": "file:///player/tv/Alb/02%20b.flac"},
        {"id": 13, "tracknum": "3", "filesize": "300",
         "url": "file:///player/tv/Alb/03%20c.flac"},
        # A remote stream in the middle must be ignored, not crash the walk.
        {"id": 14, "tracknum": "4", "url": "http://example.com/live"},
    ]

    def fake_rpc(pid, cmd):
        if cmd[0] == "songinfo":
            return {"songinfo_loop": [{"album_id": "77"}]}
        if cmd[0] == "titles":
            return {"titles_loop": album}
        return {}

    lms._rpc = fake_rpc
    ev = PlayEvent(source="lms", kind="track", path="/player/tv/Alb/02 b.flac",
                   label="x", group={"track_id": 12})
    got = lms.locality_group(ev)
    check([c.label for c in got] == ["track 03"],
          f"only tracks AFTER the playing one (got {[c.label for c in got]})")
    check(got and got[0].path == "/player/tv/Alb/03 c.flac",
          "percent-encoded file:// url decoded to a real path")
    check(got and got[0].size == 300, f"filesize carried through (got {got[0].size if got else None})")
    check(all("example.com" not in c.path for c in got), "remote stream skipped")

    ev_first = PlayEvent(source="lms", kind="track", path="x", label="x",
                         group={"track_id": 11})
    check([c.label for c in lms.locality_group(ev_first)] == ["track 02", "track 03"],
          "playing track 1 offers 2 and 3")
    ev_last = PlayEvent(source="lms", kind="track", path="x", label="x",
                        group={"track_id": 13})
    check(lms.locality_group(ev_last) == [], "playing the last track offers nothing")
    ev_unknown = PlayEvent(source="lms", kind="track", path="x", label="x",
                           group={"track_id": 999})
    check(len(lms.locality_group(ev_unknown)) == 3,
          "an id not in the album falls back to the whole album, not nothing")
    lms._rpc = lambda pid, cmd: {"songinfo_loop": [{}]}
    check(lms.locality_group(ev) == [], "a track with no album has no locality group")

    print("\n=== Navidrome: Subsonic path mapping needs music_folder")
    nd = pamts_players.NavidromePlayer({
        "name": "nd", "url": "http://unused", "username": "u",
        "token_file": "/dev/null", "music_folder": "/player/tv"})
    it = nd._song_item({"path": "Alb/01 a.flac", "size": 123, "album": "Alb",
                        "albumId": "77", "track": 1, "title": "a",
                        "played": "2026-01-02T03:04:05Z"})
    check(it is not None and it["rel"] == "Alb/01 a.flac",
          f"library-relative path resolved against music_folder (got {it})")
    check(it and it["last_viewed"] > 0, "ISO8601 'played' parsed to an epoch")
    outside = pamts_players.NavidromePlayer({
        "name": "nd2", "url": "http://unused", "username": "u",
        "token_file": "/dev/null", "music_folder": "/somewhere/else"})
    check(outside._song_item({"path": "Alb/x.flac", "size": 1}) is None,
          "a song whose music_folder is outside the configured roots is skipped")
    check(nd._song_item({"size": 1}) is None, "a song with no path is skipped")
    check(nd._epoch(None) == 0 and nd._epoch("not-a-date") == 0,
          "unparseable timestamps become 0, not an exception")

    print("\n=== history_db: LMS play history from a synthetic persist.db")
    import sqlite3
    lms_db = os.path.join(tmp, "persist.db")
    con = sqlite3.connect(lms_db)
    con.execute("CREATE TABLE tracks_persistent (id INTEGER, url TEXT, lastPlayed INTEGER, "
                "playCount INTEGER)")
    con.executemany("INSERT INTO tracks_persistent VALUES (?,?,?,?)", [
        (1, "file:///player/tv/Alb/01%20a.flac", 1000, 3),
        (2, "file:///player/tv/Alb/02%20b.flac", 2000, 1),
        (3, "file:///player/tv/Alb/03%20c.flac", None, 0),      # never played
        (4, "http://example.com/stream", 3000, 9),              # remote, unmappable
        (5, "file:///somewhere/else/x.flac", 4000, 1),          # outside the roots
    ])
    con.commit()
    con.close()

    # 127.0.0.1:1 refuses immediately, so the plugin probe fails fast. Capability is
    # only known after available() has looked -- see the sweep() ordering test below.
    plain = pamts_players.LmsPlayer({"name": "lms", "url": "http://127.0.0.1:1"})
    plain.available()
    check(plain.provides_history is False,
          "with no plugin and no history_db, LMS declares no history")
    check(plain.library_items() == [],
          "and returns [] -- a definite 'nothing', not a failure")

    withdb = pamts_players.LmsPlayer({"name": "lmsdb", "url": "http://127.0.0.1:1",
                                      "history_db": lms_db})
    withdb.available()
    check(withdb.provides_history is True, "with history_db, LMS declares history")
    items = withdb.library_items()
    rels = sorted(i["rel"] for i in items)
    check(rels == ["Alb/01 a.flac", "Alb/02 b.flac"],
          f"only played, mappable, in-root tracks (got {rels})")
    check(all(i["last_viewed"] > 0 for i in items), "play times carried through")
    byrel = {i["rel"]: i for i in items}
    check(byrel["Alb/02 b.flac"]["last_viewed"] == 2000, "the right timestamp per track")
    check(byrel["Alb/01 a.flac"]["fast"].startswith(fast), "mapped onto the fast tier")

    print("\n=== history_db: Navidrome history covers ALL users, unlike the API")
    nd_db = os.path.join(tmp, "navidrome.db")
    con = sqlite3.connect(nd_db)
    con.execute("CREATE TABLE media_file (id TEXT, path TEXT, size INT, album_id TEXT, "
                "track_number INT, disc_number INT, title TEXT)")
    con.execute("CREATE TABLE annotation (user_id TEXT, item_id TEXT, item_type TEXT, "
                "play_count INT, play_date TEXT)")
    con.executemany("INSERT INTO media_file VALUES (?,?,?,?,?,?,?)", [
        ("m1", "Alb/01 a.flac", 111, "al1", 1, 1, "a"),
        ("m2", "Alb/02 b.flac", 222, "al1", 2, 1, "b"),
        ("m3", "Alb/03 c.flac", 333, "al1", 3, 1, "c"),
    ])
    con.executemany("INSERT INTO annotation VALUES (?,?,?,?,?)", [
        # Two different users played m1; the NEWER play must win.
        ("userA", "m1", "media_file", 1, "2026-01-01T00:00:00Z"),
        ("userB", "m1", "media_file", 1, "2026-06-01T00:00:00Z"),
        ("userB", "m2", "media_file", 1, "2026-03-01T00:00:00Z"),
        ("userA", "m3", "media_file", 0, None),            # never played
        ("userA", "al1", "album", 1, "2026-07-01T00:00:00Z"),   # wrong item_type
    ])
    con.commit()
    con.close()

    nd2 = pamts_players.NavidromePlayer({
        "name": "nddb", "url": "http://unused", "username": "u",
        "token_file": "/dev/null", "music_folder": "/player/tv",
        "history_db": nd_db})
    check(nd2.all_users is True, "history_db means all users are covered")
    items = nd2.library_items()
    got = {i["rel"]: i["last_viewed"] for i in items}
    check(sorted(got) == ["Alb/01 a.flac", "Alb/02 b.flac"],
          f"only files anyone played, and only media_file rows (got {sorted(got)})")
    check(got["Alb/01 a.flac"] > got["Alb/02 b.flac"],
          "the NEWEST play across users wins for a file played by two people")
    check(nd2._epoch("2026-06-01T00:00:00Z") == got["Alb/01 a.flac"],
          "userB's June play won over userA's January one")
    byrel = {i["rel"]: i for i in items}
    check(byrel["Alb/01 a.flac"]["size"] == 111, "size carried from media_file")
    check(byrel["Alb/02 b.flac"]["episode"] == 2, "track number becomes the order key")

    print("\n=== recent_plays: the multi-user trigger, from the play records")
    nd4 = pamts_players.NavidromePlayer({
        "name": "nd4", "url": "http://unused", "username": "u",
        "token_file": "/dev/null", "music_folder": "/player/tv",
        "history_db": nd_db})
    # userB played m1 in June, m2 in March. A watermark before both sees both.
    ev_all = nd4.recent_plays(nd4._epoch("2026-01-01T00:00:00Z"))
    check(len(ev_all) == 2, f"both plays seen from an early watermark (got {len(ev_all)})")
    # A watermark after March but before June sees only the June one.
    ev_some = nd4.recent_plays(nd4._epoch("2026-04-01T00:00:00Z"))
    check([e.group["album_id"] for e in ev_some] == ["al1"] and len(ev_some) == 1,
          f"only plays newer than the watermark (got {len(ev_some)})")
    check(nd4.recent_plays(nd4._epoch("2026-12-01T00:00:00Z")) == [],
          "a watermark after everything sees nothing")
    check(all(e.remaining_s == 0.0 for e in ev_all),
          "no remaining time is claimed -- a recorded play has already finished")
    check(all(e.kind == "track" for e in ev_all), "events are tracks")
    nd5 = pamts_players.NavidromePlayer({
        "name": "nd5", "url": "http://unused", "username": "u",
        "token_file": "/dev/null", "music_folder": "/player/tv"})
    check(nd5.recent_plays(0) == [], "without history_db there is no recent-play trigger")
    check(pamts_players.PlexPlayer({"url": "http://x", "token_file": "/t"})
          .recent_plays(0) == [], "adapters that do not implement it return []")

    lms_rp = pamts_players.LmsPlayer({"name": "lrp", "url": "http://unused",
                                      "history_db": lms_db})
    check(len(lms_rp.recent_plays(0)) == 2,
          f"LMS recent_plays reads persist.db (got {len(lms_rp.recent_plays(0))})")
    check(len(lms_rp.recent_plays(1500)) == 1, "and honours the watermark")
    check(lms_rp.recent_plays(9999) == [], "nothing newer than the watermark")

    print("\n=== LMS companion plugin: discovered, preferred, and paged")
    PAGE = 2
    hist = [
        {"url": "file:///player/tv/Alb/01%20a.flac", "lastplayed": 1000,
         "playcount": 2, "filesize": 111},
        {"url": "file:///player/tv/Alb/02%20b.flac", "lastplayed": 2000,
         "playcount": 1, "filesize": 222},
        {"url": "http://example.com/stream", "lastplayed": 3000,
         "playcount": 9, "filesize": 0},          # remote: not a library file
        {"url": "file:///outside/x.flac", "lastplayed": 4000,
         "playcount": 1, "filesize": 5},          # outside the configured roots
    ]

    def plugin_rpc(pid, cmd):
        if cmd[:2] == ["pamts", "info"]:
            return {"played": len(hist), "tracks": 99, "max_page": PAGE,
                    "version": "0.1.0"}
        if cmd[:2] == ["pamts", "history"]:
            off, qty = int(cmd[2]), int(cmd[3])
            since = 0
            for a in cmd[4:]:
                if str(a).startswith("since:"):
                    since = int(str(a).split(":", 1)[1])
            rows = [h for h in hist if h["lastplayed"] > since]
            return {"count": len(rows), "history_loop": rows[off:off + qty]}
        return {}

    lms_p = pamts_players.LmsPlayer({"name": "lmsp", "url": "http://unused"})
    lms_p._rpc = plugin_rpc
    check(lms_p.available() is True, "available() succeeds")
    check(lms_p._plugin is True, "the companion plugin is discovered")
    check(lms_p.provides_history is True,
          "and flips provides_history on -- LMS has no history without it")
    check(lms_p._page == PAGE, f"page size adopted from the plugin (got {lms_p._page})")
    got = lms_p.library_items()
    rels = sorted(i["rel"] for i in got)
    check(rels == ["Alb/01 a.flac", "Alb/02 b.flac"],
          f"only mappable library files, paged correctly (got {rels})")
    byrel = {i["rel"]: i for i in got}
    check(byrel["Alb/02 b.flac"]["last_viewed"] == 2000, "play time carried through")
    check(byrel["Alb/01 a.flac"]["size"] == 111, "filesize carried through")
    check(byrel["Alb/01 a.flac"]["fast"].startswith(fast), "mapped onto the fast tier")

    evs = lms_p.recent_plays(1500)
    check([e.path for e in evs] == ["/player/tv/Alb/02 b.flac"],
          f"recent_plays honours since: server-side (got {[e.path for e in evs]})")
    check(all(e.remaining_s == 0 for e in evs),
          "a recorded play claims no remaining time")

    print("\n=== LMS without the plugin: falls back, or reports no history")
    def no_plugin_rpc(pid, cmd):
        if cmd[:2] == ["pamts", "info"]:
            return {}                      # unknown query -> no 'played' key
        return {}

    bare = pamts_players.LmsPlayer({"name": "bare", "url": "http://unused"})
    bare._rpc = no_plugin_rpc
    bare.available()
    check(bare._plugin is False, "plugin absence is detected, not an error")
    check(bare.provides_history is False, "and provides_history goes off")
    check(bare.library_items() == [], "library_items is [] -- a definite 'nothing'")
    check(bare.recent_plays(0) == [], "and no recent-play trigger")

    fallback = pamts_players.LmsPlayer({"name": "fb", "url": "http://unused",
                                        "history_db": lms_db})
    fallback._rpc = no_plugin_rpc
    fallback.available()
    check(fallback.provides_history is True,
          "with history_db configured it still provides history")
    check(len(fallback.library_items()) == 2, "and reads it from the database")

    print("\n=== sweep() asks available() BEFORE trusting capability flags")
    # An adapter can only learn what it can do by asking the server, so a flag read
    # before available() would be wrong. lms_p starts optimistic and is confirmed.
    fresh = pamts_players.LmsPlayer({"name": "fresh", "url": "http://unused"})
    fresh._rpc = plugin_rpc
    items_sw, ok_sw = pamts_players.sweep([fresh])
    check(ok_sw is True and len(items_sw) == 2,
          f"sweep discovered the plugin and got its items (ok={ok_sw}, n={len(items_sw)})")

    print("\n=== history_db: failures degrade safely")
    missing = pamts_players.LmsPlayer({"name": "x", "url": "http://u",
                                       "history_db": os.path.join(tmp, "nope.db")})
    check(missing.library_items() is None,
          "a missing history_db returns None (could not determine), not []")
    bad = os.path.join(tmp, "bad.db")
    open(bad, "wb").write(b"this is not a database")
    broken = pamts_players.LmsPlayer({"name": "y", "url": "http://u", "history_db": bad})
    check(broken.library_items() is None, "an unreadable/changed schema returns None")
    nd3 = pamts_players.NavidromePlayer({
        "name": "z", "url": "http://u", "username": "u", "token_file": "/dev/null",
        "history_db": nd_db})       # no music_folder
    check(nd3.library_items() is None, "history_db without music_folder is refused")

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
    summary()                      # exits; SystemExit propagates out of main()


if __name__ == "__main__":
    sys.exit(main())
