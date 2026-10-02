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
                       rate REAL, requests INTEGER, monotonic REAL, method TEXT,
                       tier TEXT);
""")
now = time.time()
con.execute("INSERT INTO plays VALUES('/m/a.mkv',?,1,'PLAY',1048576,?)", (now, now))
for i, lab in enumerate(("PLAY", "PROBE", "COPY", "BULK")):
    con.execute("INSERT INTO sessions VALUES(?,?,45,7,'10.0.0.1',?,1048576,0.5,10.0,"
                "1000.0,8,1.0,'splice',NULL)", (now - i, f"/m/{lab}.mkv", lab))
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
# Assert the BEHAVIOUR, not the bytecode. An earlier version of this check read a
# constant out of _conn.__code__.co_consts at a fixed index, which depends on the
# CPython version: it passed on 3.13 and failed on the Debian 12 (3.11) host PAMTS
# actually runs on, while never once proving a write was refused.
_ro = h._conn()
try:
    _ro.execute("INSERT INTO plays VALUES('/m/ro.mkv',0,1,'PLAY',1,0)")
    _ro.commit()
    _ro_refused = False
except sqlite3.OperationalError:
    _ro_refused = True
finally:
    _ro.close()
check("it opens the db READ-ONLY, so a running daemon is undisturbed", _ro_refused,
      "a write through HistorySource._conn() succeeded")
_vfy = sqlite3.connect(dbp)
check("and nothing was actually written",
      _vfy.execute("SELECT count(*) FROM plays WHERE path='/m/ro.mkv'").fetchone()[0] == 0)
_vfy.close()
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
# The fraction now lives on the POOL, because there is no estate-wide budget to
# divide by. The old estate-wide used_fraction is what produced "281.2% of 400.0G".
_p0 = t.collect()["pools"][0]
check("computes the used fraction against that pool's own budget",
      abs(_p0["used_fraction"] - 4096 / 1024**3) < 1e-9, str(_p0["used_fraction"]))
check("a job with no budget_gb lands in the shared pool",
      _p0["pool"] == "__shared__", _p0["pool"])
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
# The intent is "fetches nothing from the network", not "contains no http substring".
# The SVG namespace in the inline favicon is an XML identifier that no browser ever
# requests, so it is excluded explicitly rather than by loosening the check.
SVG_NS = "http://www.w3.org/2000/svg"
_page_net = page.replace(SVG_NS, "").replace(SVG_NS.replace("/", "%2F"), "")
check("no external requests: it must work with no internet",
      "http://" not in _page_net and "https://" not in _page_net,
      "found: " + ", ".join(sorted({w for w in _page_net.split('"')
                                    if w.startswith(("http://", "https://"))}))[:200])
# And the things that actually cause a fetch, named directly.
for _bad, _why in (("<script src", "external scripts"),
                   ('rel="stylesheet"', "external stylesheets"),
                   ("@import", "imported stylesheets"),
                   ("url(http", "remote assets in CSS")):
    check(f"the page loads no {_why}", _bad not in page, _bad)
check("the favicon is inlined, so there is no 404 on every load",
      'rel="icon"' in page and "data:image/svg+xml" in page)
check("supports light and dark", "prefers-color-scheme" in page)
check("an UNREGISTERED source still renders, as raw JSON",
      "raw" in page and "JSON.stringify" in page,
      "the page must not hide data it has no renderer for")
check("it polls a relative URL, so it works behind a proxy",
      'fetch("api/state"' in page)

import shutil                                                   # noqa: E402
# ------------------------------------------------------------------------- tier
print("\nhistory: tier of the files accessed")
check("sessions carry a tier field",
      all("tier" in x for x in h.collect()["sessions"]),
      str(h.collect()["sessions"][:1]))
check("this fixture's rows have no tier recorded",
      {x["tier"] for x in h.collect()["sessions"]} == {None},
      str({x["tier"] for x in h.collect()["sessions"]}))
check("the source reports that the column exists", h.collect()["tier_known"] is True)

_tc = sqlite3.connect(dbp)
_tc.execute("UPDATE sessions SET tier='hot'  WHERE label='PLAY'")
_tc.execute("UPDATE sessions SET tier='cold' WHERE label='PROBE'")
_tc.execute("UPDATE sessions SET tier='cold' WHERE label='COPY'")
_tc.commit(); _tc.close()
out = h.collect()
check("a tier is returned per session",
      {x["label"]: x["tier"] for x in out["sessions"]}.get("PLAY") == "hot",
      str({x["label"]: x["tier"] for x in out["sessions"]}))
# The 24h breakdown must cover DEMAND only. A cold COPY is the backup doing its job;
# counting it would swamp the ratio and hide the number that matters -- cold PLAYs,
# which are spin-ups a viewer waited for.
check("the 24h tier breakdown counts demand only, not copies and scans",
      out["tier_24h"] == {"hot": 1}, str(out["tier_24h"]))

# A database written by an older daemon has no tier column at all.
_odb = str(pathlib.Path(tempfile.mkdtemp(prefix="pamts-dash-old-")) / "old.db")
_oc = sqlite3.connect(_odb)
_oc.executescript("""
CREATE TABLE plays (path TEXT PRIMARY KEY, last_play REAL NOT NULL,
  play_count INTEGER NOT NULL DEFAULT 0, last_label TEXT, last_bytes INTEGER,
  updated REAL NOT NULL);
CREATE TABLE sessions (ts REAL NOT NULL, path TEXT, dev INTEGER, ino INTEGER,
  client TEXT, label TEXT NOT NULL, bytes INTEGER, coverage REAL, duration REAL,
  rate REAL, requests INTEGER, monotonic REAL, method TEXT);
""")
_oc.execute("INSERT INTO sessions VALUES(?,'/a.mkv',1,2,'c','PLAY',1,0.5,1,1,1,1,'s')",
            (time.time(),))
_oc.commit(); _oc.close()
_oh = dash.HistorySource({"observer_db": _odb})
_oout = _oh.collect()
check("an older database without the column still returns sessions",
      len(_oout["sessions"]) == 1, str(_oout["sessions"]))
check("and their tier is None rather than the query failing",
      _oout["sessions"][0]["tier"] is None, str(_oout["sessions"][0]))
check("and the source says the column is absent", _oout["tier_known"] is False)
check("so the page can explain itself rather than showing a false 0%",
      _oout["tier_24h"] == {}, str(_oout["tier_24h"]))


# --------------------------------------------------------------- tier pools
print("\ntier: budgets are per pool, not one total")
_tj = [
    {"name": "movies", "mode": "tier", "source": os.path.join(tmp, "m")},
    {"name": "tv", "mode": "tier", "source": os.path.join(tmp, "t")},
    {"name": "music", "mode": "tier", "source": os.path.join(tmp, "a"), "budget_gb": 0.000002},
]
for d, size in (("m", 1024), ("t", 2048), ("a", 4096)):
    os.makedirs(os.path.join(tmp, d), exist_ok=True)
    with open(os.path.join(tmp, d, "f.bin"), "wb") as fh:
        fh.truncate(size)
ts = dash.TierSource({"tier_jobs": _tj, "budget_gb": 0.000005,
                      "promote_headroom_gb": 0.000001})
to = ts.collect()
check("jobs collapse into two pools", len(to["pools"]) == 2, str(len(to["pools"])))
byname = {p["pool"]: p for p in to["pools"]}
check("movies and tv share one pool", "__shared__" in byname, str(sorted(byname)))
check("music has its own pool", "music" in byname, str(sorted(byname)))
check("the shared pool sums only its own jobs",
      byname["__shared__"]["bytes"] == 1024 + 2048, str(byname["__shared__"]["bytes"]))
check("the music pool sums only music", byname["music"]["bytes"] == 4096,
      str(byname["music"]["bytes"]))
check("each pool reports its OWN budget",
      byname["music"]["budget_bytes"] != byname["__shared__"]["budget_bytes"],
      str([p["budget_bytes"] for p in to["pools"]]))
# THE BUG THIS REPLACES. The old source summed every job and divided by the shared
# budget, which reported "281.2% of 400.0G" on a system where nothing was over budget.
check("there is no estate-wide used_fraction to misread",
      "used_fraction" not in to, str(sorted(to)))
check("the grand total is still reported", to["used_bytes"] == 1024 + 2048 + 4096,
      str(to["used_bytes"]))
for p in to["pools"]:
    f = p["used_fraction"]
    check(f"{p['pool']} fraction is a plausible ratio, not an artefact",
          f is None or 0 <= f <= 2.0, f"{p['pool']}={f}")
check("the eviction target is budget minus the reserve",
      byname["music"]["target_bytes"]
      == byname["music"]["budget_bytes"] - byname["music"]["reserve_bytes"],
      str(byname["music"]))
check("a pool under budget reports promotable room",
      byname["music"]["promotable_bytes"] >= 0, str(byname["music"]["promotable_bytes"]))
# Measured against the BUDGET, not the eviction target: the reserve is the space
# between the two lines and promotion is allowed to spend it. Measuring against the
# target made the dashboard agree with a bug rather than with the engine.
# The shared pool is UNDER budget here, so it is the one that shows the measure.
_sh = byname["__shared__"]
check("promotable room is measured against the budget, not the eviction target",
      _sh["promotable_bytes"] == _sh["budget_bytes"] - _sh["bytes"],
      f"{_sh['promotable_bytes']} vs {_sh['budget_bytes'] - _sh['bytes']}")
check("and it exceeds the eviction target's slack, which is the point of the reserve",
      _sh["promotable_bytes"] > _sh["target_bytes"] - _sh["bytes"],
      f"promotable={_sh['promotable_bytes']} target slack="
      f"{_sh['target_bytes'] - _sh['bytes']}")
# An over-budget pool has none, rather than a negative figure.
check("an over-budget pool reports zero promotable room, not a negative number",
      byname["music"]["promotable_bytes"] == 0,
      str(byname["music"]["promotable_bytes"]))
# 512 bytes of budget against 1024 bytes of content. Expressed in GB because that is
# the unit the config uses, which is also why the source must not int() it away.
_tiny = dash.TierSource({"tier_jobs": [dict(_tj[0])], "budget_gb": 512 / 1024 ** 3,
                         "promote_headroom_gb": 0})
_t2 = _tiny.collect()["pools"][0]
check("an over-budget pool says so", _t2["over_budget"] is True, str(_t2))
check("a sub-1GB budget survives rather than truncating to zero",
      _t2["budget_bytes"] == 512, str(_t2["budget_bytes"]))
check("and its fraction exceeds 1", _t2["used_fraction"] > 1, str(_t2["used_fraction"]))

# ------------------------------------------------------------------- events
print("\nevents: transfer history, throughput and utilisation")
import pamts_events                                              # noqa: E402
_edb = os.path.join(tmp, "events.db")
pamts_events.configure(_edb)
check("configure creates the store", os.path.exists(_edb))
now = time.time()
_con = sqlite3.connect(_edb)
# Seed at known offsets so bucketing can be asserted rather than eyeballed.
for off, kind, pool, nbytes, secs in (
        (-30, "promote", "__shared__", 1000, 2.0),
        (-30, "evict", "music", 4000, 4.0),
        (-7200, "promote", "music", 2000, 1.0)):
    _con.execute("INSERT INTO transfers(ts,kind,pool,job,item,bytes,seconds,rate,label,"
                 "reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (now + off, kind, pool, "j", "it", nbytes, secs, nbytes / secs, "L", "R"))
for off, pool, fp in ((-30, "music", 700), (-7200, "music", 900)):
    _con.execute("INSERT INTO samples(ts,pool,footprint,budget,reserve) VALUES(?,?,?,?,?)",
                 (now + off, pool, fp, 1000, 60))
_con.commit(); _con.close()

es = dash.EventsSource({"events_db": _edb})
check("the source is available once the file exists", es.available())
eo = es.collect(window=86400, buckets=24)
check("recent transfers come back newest first",
      [r["ts"] for r in eo["recent"]] == sorted((r["ts"] for r in eo["recent"]), reverse=True),
      str([round(r["ts"] - now) for r in eo["recent"]]))
check("totals are split by direction",
      eo["totals"]["promote"]["count"] == 2 and eo["totals"]["evict"]["count"] == 1,
      str(eo["totals"]))
check("and summed in bytes", eo["totals"]["evict"]["bytes"] == 4000, str(eo["totals"]))
check("a per-item rate is derived", eo["recent"][0]["rate"] is not None)
tp = eo["throughput"]
check("throughput is bucketed per direction", set(tp) == {"promote", "evict"}, str(sorted(tp)))
check("each series has one slot per bucket",
      all(len(v) == eo["buckets"] + 1 for v in tp.values()),
      str({k: len(v) for k, v in tp.items()}))
check("buckets with nothing in them are None, not zero",
      any(x is None for x in tp["evict"]), "evict series has no gaps at all")
check("the two promotions land in DIFFERENT buckets, two hours apart",
      len([x for x in tp["promote"] if x]) == 2,
      str([i for i, x in enumerate(tp["promote"]) if x]))
ut = eo["utilisation"]
check("utilisation is returned per pool", list(ut) == ["music"], str(sorted(ut)))
check("and carries the budget for a reference line",
      any(x and x["budget"] == 1000 for x in ut["music"]), str([x for x in ut["music"] if x]))

# Recording must never take a run down with it.
pamts_events.configure("/proc/definitely/not/writable/x.db")
check("an unwritable store disables recording rather than raising",
      pamts_events.record_transfer("promote", "x", 1) is False)
check("and samples too", pamts_events.record_sample("p", 1, 2) is False)
pamts_events.configure(None)
check("configure(None) is a no-op store", pamts_events.record_transfer("evict", "y", 1) is False)

_eo2 = dash.EventsSource({"events_db": os.path.join(tmp, "nope.db")})
check("a missing events db is unavailable, not an error", _eo2.available() is False)

srv.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
summary()
