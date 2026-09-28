#!/usr/bin/env python3
"""Tests for ObserverPlayer (pamts_players.ObserverPlayer).

Stands up a stub observer HTTP API and a two-tier tree on disk. The case that
matters most is that locality_group finds siblings on the SLOW tier: those are
exactly the files promotion exists to fetch, so a single-tier listing would make
the adapter useless while still passing a naive test.

Run:  python3 tests/test_observer_adapter.py
"""
import json, os, sys, tempfile, threading, shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pamts, pamts_players as pp

fails = []
def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" -- {detail}" if not cond and detail else ""))
    if not cond: fails.append(name)

tmp = tempfile.mkdtemp()
fast = os.path.join(tmp, "cache"); slow = os.path.join(tmp, "store")
show = "tv/Some Show (2024)/Season 2"
os.makedirs(os.path.join(fast, show)); os.makedirs(os.path.join(slow, show))
# E01 on fast (played), E02+E03 on SLOW -- promotion must find these
open(os.path.join(fast, show, "SS - S02E01.mkv"), "wb").write(b"x" * 5_000_000)
open(os.path.join(slow, show, "SS - S02E02.mkv"), "wb").write(b"y" * 10)
open(os.path.join(slow, show, "SS - S02E03.mkv"), "wb").write(b"z" * 10)
open(os.path.join(fast, show, "poster.jpg"), "wb").write(b"j")      # not media

pamts.ROOTS = {os.path.join(tmp, "library"): (fast, slow), fast: (fast, slow)}
played = os.path.join(fast, show, "SS - S02E01.mkv")

HIST = {"history": [
    {"path": played, "last_play": 1790000000.0, "play_count": 1, "label": "PLAY"},
    {"path": os.path.join(fast, show, "scanned.mkv"), "last_play": 1790000001.0,
     "play_count": 1, "label": "PROBE"},                      # must be ignored
    {"path": "/somewhere/else/x.mkv", "last_play": 1790000002.0,
     "play_count": 1, "label": "PLAY"},                       # outside ROOTS
]}
SESS = {"sessions": [
    {"path": played, "label": "PLAY", "client": "10.0.0.20",
     "bytes": 1_000_000, "coverage": None, "rate": 2_000_000.0, "idle_s": 1.0},
    {"path": played, "label": "PROBE", "client": "10.0.0.9",
     "bytes": 400_000, "coverage": 0.01, "rate": 1_700_000.0, "idle_s": 0.2},
]}

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        body = json.dumps({"": {"ok": True}}.get("", {"ok": True})
                          if self.path.startswith("/health")
                          else HIST if self.path.startswith("/history") else SESS).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}"

o = pp.build({"kind": "observer", "name": "obs", "url": url})
check("available() succeeds against a live observer", o.available())

items = o.library_items()
check("library_items returns only demand labels", len(items) == 1, f"{len(items)} items")
check("PROBE is excluded from history", all("scanned" not in i["title"] for i in items))
check("paths outside ROOTS are skipped", all(i["rel"].startswith("tv/") for i in items))
it = items[0]
check("rel is relative to the root", it["rel"] == f"{show}/SS - S02E01.mkv", it["rel"])
check("fast path resolved", it["fast"] == os.path.join(fast, it["rel"]))
check("slow path resolved", it["slow"] == os.path.join(slow, it["rel"]))
check("last_viewed carried through", it["last_viewed"] == 1790000000)
check("show_key is the containing directory", it["show_key"] == show, it["show_key"])
check("kind inferred as episode for .mkv", it["kind"] == "episode")

np = o.now_playing()
check("now_playing returns only PLAY", len(np) == 1, f"{len(np)}")
ev = np[0]
check("event carries the client in its label", "10.0.0.20" in ev.label, ev.label)
# 5 MB file, 1 MB read, 2 MB/s -> ~2 s left
check("remaining_s estimated from size/rate", 1.0 < ev.remaining_s < 3.0,
      f"{ev.remaining_s:.2f}")

grp = o.locality_group(ev)
check("locality finds the next episodes", len(grp) == 2, f"{len(grp)}")
check("next episodes come from the SLOW tier",
      [c.label for c in grp] == ["SS - S02E02.mkv", "SS - S02E03.mkv"],
      str([c.label for c in grp]))
check("non-media siblings excluded", all("poster" not in c.label for c in grp))
check("order is ascending", [c.order for c in grp] == [0, 1])

rp = o.recent_plays(0)
check("recent_plays returns played items", len(rp) == 1 and rp[0].path == played)

# a track
tp = os.path.join(fast, "music/Artist/Album/01 - t.flac")
os.makedirs(os.path.dirname(tp)); open(tp, "wb").write(b"f")
check("kind inferred as track for .flac", o._kind_for(tp) == "track")

# unreachable observer must be honest
bad = pp.build({"kind": "observer", "name": "bad", "url": "http://127.0.0.1:1"})
check("unreachable observer is not available", not bad.available())
check("library_items returns None (not []) on failure", bad.library_items() is None)
check("now_playing returns [] on failure", bad.now_playing() == [])

srv.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
print()
if fails: print(f"{len(fails)} FAILED: {', '.join(fails)}"); sys.exit(1)
print("all adapter checks passed")
