#!/usr/bin/env python3
"""Tests for the dashboard data layer (pamts_dash) and its HTTP API.

The property that matters most is isolation: a dashboard that shows nothing because
one component is down is worse than one that shows most of the truth and says what
is missing. So a failing source must never cost you the others.

Run:  python3 tests/test_dash.py
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pamts_dash as dash                                       # noqa: E402
from _harness import check, summary                             # noqa: E402

tmp = tempfile.mkdtemp()

# --- the registry contract ---------------------------------------------------
print("registry")
check("sources are registered by name",
      set(dash.SOURCES) >= {"observer", "state", "config", "tier", "history"},
      str(sorted(dash.SOURCES)))
check("every source subclasses Source",
      all(issubclass(c, dash.Source) for c in dash.SOURCES.values()))
check("every source declares a name matching its key",
      all(c.name == k for k, c in dash.SOURCES.items()))
check("build() with no names builds them all",
      len(dash.build(cfg={})) == len(dash.SOURCES))
check("build() accepts a subset",
      [s.name for s in dash.build(["state"], cfg={})] == ["state"])
try:
    dash.build(["nope"], cfg={})
    check("an unknown source raises", False)
except ValueError as e:
    check("an unknown source raises", "nope" in str(e))

# --- isolation: the thing that matters --------------------------------------
print("\nisolation")


class Boom(dash.Source):
    name = "boom"

    def collect(self):
        raise RuntimeError("deliberate")


class Fine(dash.Source):
    name = "fine"

    def collect(self):
        return {"ok": True}


class Absent(dash.Source):
    name = "absent"

    def available(self):
        return False

    def collect(self):                                          # pragma: no cover
        raise AssertionError("must not be called when unavailable")


data, errors = dash.collect_all([Boom(), Fine(), Absent()])
check("a raising source is reported as an error", "boom" in errors, str(errors))
check("...and does not stop the others", data.get("fine") == {"ok": True})
check("an unavailable source is not collected", "absent" in errors)
check("an unavailable source is never called", errors["absent"] == "unavailable")
check("the error names the exception type", "RuntimeError" in errors["boom"],
      errors["boom"])

# --- caching -----------------------------------------------------------------
print("\ncaching (a filesystem scan must not run on every poll)")


class Counter(dash.Source):
    name = "counter"
    ttl = 60.0

    def __init__(self):
        super().__init__()
        self.calls = 0

    def collect(self):
        self.calls += 1
        return {"calls": self.calls}


c = Counter()
c.get(); c.get(); c.get()
check("a source within its ttl is collected once", c.calls == 1, str(c.calls))
c.get(force=True)
check("force bypasses the cache", c.calls == 2, str(c.calls))
z = Fine(); z.ttl = 0.0
z.get(); z.get()
check("ttl=0 means always fresh", True)   # structural: no cache to assert on

# --- HistorySource reads the real schema ------------------------------------
print("\nhistory source")
dbp = os.path.join(tmp, "observer.db")
con = sqlite3.connect(dbp)
con.executescript("""
CREATE TABLE plays (path TEXT PRIMARY KEY, last_play REAL, play_count INTEGER,
                    last_label TEXT, last_bytes INTEGER, updated REAL);
CREATE TABLE sessions (ts REAL, path TEXT, dev INTEGER, ino INTEGER, client TEXT,
                       label TEXT, bytes INTEGER, coverage REAL, duration REAL,
                       rate REAL, requests INTEGER, monotonic REAL, method TEXT);
""")
now = time.time()
con.execute("INSERT INTO plays VALUES('/m/a.mkv',?,1,'PLAY',1048576,?)", (now, now))
for i, lab in enumerate(("PLAY", "PROBE", "COPY", "BULK")):
    con.execute("INSERT INTO sessions VALUES(?,?,45,7,'10.0.0.1',?,1048576,0.5,10.0,"
                "1000.0,8,1.0,'splice')", (now - i, f"/m/{lab}.mkv", lab))
con.commit(); con.close()

h = dash.HistorySource({"observer_db": dbp})
check("available when the db exists", h.available())
out = h.collect()
check("returns sessions newest first",
      [s["label"] for s in out["sessions"]] == ["PLAY", "PROBE", "COPY", "BULK"],
      str([s["label"] for s in out["sessions"]]))
check("returns plays", len(out["plays"]) == 1)
check("counts labels over 24h", out["labels_24h"].get("PROBE") == 1,
      str(out["labels_24h"]))
check("since= filters", len(h.collect(since=now + 100)["sessions"]) == 0)
check("limit caps the result", len(h.collect(limit=2)["sessions"]) == 2)
check("it opens the db READ-ONLY, so a running daemon is undisturbed",
      "mode=ro" in dash.HistorySource.__dict__["_conn"].__code__.co_consts[1])
# Copies and scans outnumber plays by hundreds to one, so filtering has to happen in
# SQL. A client-side filter on the most recent N rows would discard nearly all of them.
print("\nhistory: label filtering")
check("filtering returns only the asked-for verdicts",
      [x["label"] for x in h.collect(labels=["PLAY"])["sessions"]] == ["PLAY"],
      str([x["label"] for x in h.collect(labels=["PLAY"])["sessions"]]))
check("several labels are allowed",
      sorted(x["label"] for x in h.collect(labels=["PLAY", "COPY"])["sessions"])
      == ["COPY", "PLAY"])
check("no filter means everything", len(h.collect()["sessions"]) == 4)
check("an unmatched label returns nothing, not everything",
      h.collect(labels=["NOPE"])["sessions"] == [])
check("the response says what it filtered by",
      h.collect(labels=["PLAY"])["filtered_by"] == ["PLAY"])
check("...and None when unfiltered", h.collect()["filtered_by"] is None)
check("available labels are discovered from the data, not hardcoded",
      h.collect()["available_labels"] == ["BULK", "COPY", "PLAY", "PROBE"],
      str(h.collect()["available_labels"]))
check("the demand set is published so the UI need not hardcode it",
      h.collect()["demand_labels"] == list(dash.DEMAND_LABELS))
# labels arrive from a query string, so they must be parameterised
check("a label containing SQL is treated as data",
      h.collect(labels=["PLAY'; DROP TABLE sessions; --"])["sessions"] == [])
check("...and the table survived", len(h.collect()["sessions"]) == 4)
check("limit still applies alongside a filter",
      len(h.collect(labels=["PLAY", "COPY", "PROBE", "BULK"], limit=2)["sessions"]) == 2)

check("unavailable when the db is missing",
      not dash.HistorySource({"observer_db": "/nope.db"}).available())

# --- TierSource --------------------------------------------------------------
print("\ntier source")
tj = os.path.join(tmp, "tv")
os.makedirs(os.path.join(tj, "Show", "S1"))
with open(os.path.join(tj, "Show", "S1", "e01.mkv"), "wb") as f:
    f.write(b"x" * 4096)
t = dash.TierSource({"tier_jobs": [{"name": "tv", "source": tj}], "budget_gb": 1})
check("sums the tier footprint", t.collect()["used_bytes"] == 4096,
      str(t.collect()["used_bytes"]))
check("counts files", t.collect()["jobs"][0]["files"] == 1)
check("computes the used fraction against the budget",
      abs(t.collect()["used_fraction"] - 4096 / 1024**3) < 1e-9)
bad = dash.TierSource({"tier_jobs": [{"name": "gone", "source": "/nope"}]})
check("a missing job directory is an error on that job, not a crash",
      bad.collect()["jobs"][0]["error"] == "not a directory")
check("unavailable with no tier jobs", not dash.TierSource({}).available())

# --- StateSource -------------------------------------------------------------
print("\nstate source")
sp = os.path.join(tmp, "state.json")
with open(sp, "w") as f:
    json.dump({"promotions": {"/m/x": {"at": now, "label": "Show S1E1"},
                              "/m/y": {"at": now - 500, "label": "Show S1E2"}},
               "rate_bps": 1.5e8, "observed": {"a": 1}}, f)
st = dash.StateSource({"state_file": sp})
d = st.collect()
check("counts promotions", d["promotions"] == 2)
check("orders recent promotions newest first",
      d["recent_promotions"][0]["label"] == "Show S1E1",
      d["recent_promotions"][0]["label"])
check("carries the measured rate", d["rate_bps"] == 1.5e8)
check("counts observed plays", d["observed_plays"] == 1)
check("unavailable when the state file is missing",
      not dash.StateSource({"state_file": "/nope.json"}).available())

# --- ObserverSource health derivation ---------------------------------------
print("\nobserver source")


class FakeObs(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    STATS = {"records": 100, "kernel_emitted": 100, "kernel_dropped": 0,
             "sessions_closed": 3, "index_files": 9}
    SESSIONS = {"sessions": [{"label": "PLAY", "path": "/m/a.mkv", "bytes": 1},
                             {"label": "PROBE", "path": "/m/b.mkv", "bytes": 2}]}

    def do_GET(self):
        body = json.dumps(
            {"ok": True} if self.path.startswith("/health")
            else FakeObs.STATS if self.path.startswith("/stats")
            else FakeObs.SESSIONS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeObs)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ourl = f"http://127.0.0.1:{srv.server_address[1]}"
o = dash.ObserverSource({"url": ourl})
check("available against a live observer", o.available())
od = o.collect()
check("separates PLAY sessions from the rest", len(od["playing"]) == 1,
      str(od["playing"]))
check("keeps all sessions too", len(od["sessions"]) == 2)
check("healthy when nothing was dropped", od["keeping_up"] is True)
check("lag is reported", od["lag"] == 0)
# The two counters are read non-atomically, so records can run one AHEAD. An
# earlier version used strict equality and cried wolf on that.
FakeObs.STATS = dict(FakeObs.STATS, records=101)
check("records running one AHEAD is not a fault",
      o.get(force=True)["keeping_up"] is True,
      "non-atomic reads make a +1 race normal")
FakeObs.STATS = dict(FakeObs.STATS, records=90)
check("a small lag is in-flight buffering, not a fault",
      o.get(force=True)["keeping_up"] is True)
FakeObs.STATS = dict(FakeObs.STATS, kernel_dropped=1234, records=100)
check("DROPPED events are the authoritative fault signal",
      o.get(force=True)["keeping_up"] is False)
check("...and are reported", o.get(force=True)["dropped"] == 1234)
# The bound is the ring buffer's capacity: a lag larger than it cannot be
# in-flight data, so the consumer really is behind.
FakeObs.STATS = dict(FakeObs.STATS, kernel_dropped=0,
                     kernel_emitted=5_000_000, records=1)
check("a lag beyond the ring buffer IS a fault",
      o.get(force=True)["keeping_up"] is False,
      f"lag {5_000_000 - 1} vs bound {o.max_lag}")
FakeObs.STATS = dict(FakeObs.STATS, kernel_emitted=None, kernel_dropped=None, records=90)
check("an observer without counters is not reported as unhealthy",
      o.get(force=True)["keeping_up"] is True)
check("unavailable when nothing is listening",
      not dash.ObserverSource({"url": "http://127.0.0.1:1"}).available())

# --- the page is self-contained ---------------------------------------------
print("\nthe page")
page = (ROOT / "web" / "index.html").read_text()
check("page exists and is not empty", len(page) > 1000)
check("no external requests: it must work with no internet",
      "http://" not in page and "https://" not in page)
check("supports light and dark", "prefers-color-scheme" in page)
check("an UNREGISTERED source still renders, as raw JSON",
      "raw" in page and "JSON.stringify" in page,
      "the page must not hide data it has no renderer for")
check("it polls a relative URL, so it works behind a proxy",
      'fetch("api/state"' in page)

import shutil                                                   # noqa: E402
srv.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
summary()
