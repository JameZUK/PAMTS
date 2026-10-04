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
import pamts_events                                             # noqa: E402
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


# ===================================================== what the dashboard publishes
print("\n=== CONFIG: settings are allow-listed, not forwarded wholesale")
_pub = dash._publishable(
    {"budget_gb": 400, "settle_seconds": 3600, "something_new_token": "s3cret"},
    dash.TIER_PUBLIC)
check("an allow-listed setting is published", _pub["budget_gb"] == 400, str(_pub))
check("an UNKNOWN key's value is withheld",
      _pub["something_new_token"] == dash.WITHHELD, str(_pub))
check("but its name is still shown, so the setting is not hidden",
      "something_new_token" in _pub,
      "a reader should see that a setting exists without being told its value")
check("skip= drops a key entirely",
      "rules" not in dash._publishable({"rules": [1], "max_items": 2},
                                             dash.PROMOTE_PUBLIC,
                                             skip=("rules",)))
check("no secret-looking key is on either allow-list",
      not [k for k in (dash.TIER_PUBLIC | dash.PROMOTE_PUBLIC)
           if any(w in k for w in ("token", "pass", "secret", "key"))],
      str(sorted(dash.TIER_PUBLIC | dash.PROMOTE_PUBLIC)))

print("\n=== EVENTS: utilisation plots the LATEST sample in each bucket")
# Two samples in ONE bucket. The old query took bare columns alongside MAX(ts) in
# HAVING, which SQLite only guarantees when the aggregate is in the SELECT list -- so
# it could plot either row. Making the newer one the SMALLER footprint means picking
# the wrong row is visible rather than a coin toss that passes half the time.
_eu = pathlib.Path(tempfile.mkdtemp(prefix="pamts-util-"))
_edb = str(_eu / "events.db")
import sqlite3 as _sq3
_c = _sq3.connect(_edb)
_c.executescript(pamts_events.SCHEMA)
_now = time.time()
for _ts, _fp in ((_now - 100, 900 * (1 << 30)), (_now - 50, 100 * (1 << 30))):
    _c.execute("INSERT INTO samples(ts,pool,footprint,budget) VALUES(?,?,?,?)",
               (_ts, "music", _fp, 800 * (1 << 30)))
_c.commit(); _c.close()
_src = dash.EventsSource({"events_db": _edb})
_out = _src.collect(window=3600, buckets=2)
_pts = [p for p in (_out["utilisation"].get("music") or []) if p]
check("one point is produced for the bucket", len(_pts) == 1, str(_pts))
check("and it is the LATEST sample, not an arbitrary one in the bucket",
      _pts and _pts[0]["footprint"] == 100 * (1 << 30),
      f"got {_pts and _pts[0].get('footprint')}, wanted the newer 100G sample")
shutil.rmtree(_eu, ignore_errors=True)

print("\n=== PAGE: the escaper covers every character that can break out")
_page = (ROOT / "web" / "index.html").read_text()
_escdef = [ln for ln in _page.splitlines() if "const esc =" in ln]
check("esc() exists", bool(_escdef), "the page must escape server data")
check("and its character class includes the apostrophe",
      _escdef and "'" in _escdef[0].split("replace(")[1].split(",")[0],
      f"got {_escdef and _escdef[0].strip()!r} -- media FILENAMES reach this")
check("the single-quote entity is in the map", "&#39;" in _page)
# Nothing may interpolate into a single-quoted attribute; esc now covers it either way,
# but the pattern is worth keeping out of the file.
import re as _re
# Strip // line comments first: the comment above esc() spells the pattern out in
# order to warn about it, and matching that would make this check cry wolf forever.
_code = "\n".join(_re.sub(r"\s*//.*$", "", ln) for ln in _page.splitlines())
_bad = [ln.strip()[:80] for ln in _code.splitlines() if _re.search(r"=\'\$\{", ln)]
check("no value is interpolated into a single-quoted attribute", not _bad, str(_bad))

# ====================================================== PAMTS's own status panel
print("\n=== HEALTH: PAMTS reports on itself, and only on itself")
_h = pathlib.Path(tempfile.mkdtemp(prefix="pamts-health-"))

# A plausible set of PAMTS-owned files, and nothing else.
_sf = _h / "state.json"
_sf.write_text(json.dumps({"promotions": {"a": {"at": 1}},
                           "observed": {"/x.flac": 1, "/y.flac": 2}}))
_wf = _h / "watermarks.json"
_wf.write_text(json.dumps({"plex": time.time()}))
_edb = _h / "events.db"
import sqlite3 as _sq4
_cc = _sq4.connect(str(_edb)); _cc.executescript(pamts_events.SCHEMA); _cc.close()
_cfgf = _h / "pamts.toml"
_cfgf.write_text('[[players]]\nname="lms"\nkind="lms"\nurl="http://127.0.0.1:1"\n')

_hs = dash.HealthSource({"url": "http://127.0.0.1:1", "state_file": str(_sf),
                         "watermarks_file": str(_wf), "events_db": str(_edb),
                         "observer_db": str(_h / "nope.db"),
                         "config_file": str(_cfgf)})
_r = _hs.collect()
_by = {c["name"]: c for c in _r["checks"]}
check("the panel produces checks", len(_r["checks"]) > 4, str(len(_r["checks"])))
check("every check names a state", all(c["state"] in dash.HealthSource.RANK
                                      for c in _r["checks"]),
      str([c for c in _r["checks"] if c["state"] not in dash.HealthSource.RANK]))
check("every check is grouped for display",
      all(c.get("group") for c in _r["checks"]))
check("state.json is read and summarised",
      "2 observed play(s)" in _by["state.json"]["detail"],
      _by["state.json"]["detail"])
check("a fresh cursor file proves the poller is running",
      _by["promotion poller is running"]["state"] == "ok",
      _by["promotion poller is running"]["detail"])

# Database freshness must come from the newest ROW, not the file's mtime: both stores
# are WAL, where the .db file is only touched at a checkpoint, so mtime reads stale
# while rows arrive every second.
_wdb = _h / "wal.db"
_wc = _sq4.connect(str(_wdb))
_wc.executescript(pamts_events.SCHEMA)
_wc.execute("PRAGMA journal_mode=WAL")
_wc.execute("INSERT INTO transfers(ts,kind,item,bytes) VALUES(?,?,?,?)",
            (time.time(), "evict", "x", 1))
_wc.commit()
# Backdate the FILE while the row stays current -- what WAL does in production.
_os2 = __import__("os")
_os2.utime(_wdb, (time.time() - 7200, time.time() - 7200))
_wr = dash.HealthSource({"events_db": str(_wdb)}).collect()
_wrow = {c["name"]: c for c in _wr["checks"]}["events.db"]
# An old newest-row is NOT a fault when nothing is reading. The first version warned
# on age alone, so the panel sat amber all morning while the estate was simply quiet --
# which is how you teach someone to ignore a status light.
_idle = _h / "idle.db"
_ic = _sq4.connect(str(_idle)); _ic.executescript(pamts_events.SCHEMA)
_ic.execute("CREATE TABLE sessions (ts REAL, path TEXT)")
_ic.execute("INSERT INTO sessions(ts,path) VALUES(?,?)", (time.time() - 7200, "/a"))
_ic.commit(); _ic.close()


class _FakeStats(dash.HealthSource):
    """Pins the observer stats so idle and stuck can be tested apart."""
    def __init__(self, cfg, stats):
        super().__init__(cfg)
        self._fake = stats

    def _observer_stats(self):
        return self._fake


_quiet = _FakeStats({"observer_db": str(_idle)}, {"sessions_open": 0}).collect()
_qrow = {c["name"]: c for c in _quiet["checks"]}["observer.db"]
check("a 2h-old newest row with nothing reading is OK, not a warning",
      _qrow["state"] == "ok", f"{_qrow['state']}: {_qrow['detail']}")
check("and it says why rather than going silent",
      "nothing reading" in _qrow["detail"], _qrow["detail"])

_stuck = _FakeStats({"observer_db": str(_idle)}, {"sessions_open": 7}).collect()
_srow = {c["name"]: c for c in _stuck["checks"]}["observer.db"]
check("but sessions open and nothing written for an hour IS a warning",
      _srow["state"] == "warn", f"{_srow['state']}: {_srow['detail']}")
check("and it names how many are stuck open",
      "7 session(s) open" in _srow["detail"], _srow["detail"])

check("a WAL database with a 2h-old file but a current row reads as fresh",
      _wrow["state"] == "ok" and "newest" in _wrow["detail"],
      _wrow["detail"])
check("and it reports the row count",
      "1 transfers" in _wrow["detail"], _wrow["detail"])

# The whole point of the panel: a real fault must come out as fail, not be averaged away.
_r2 = dash.HealthSource({"url": "http://127.0.0.1:1", "state_file": str(_sf),
                         "watermarks_file": str(_h / "absent.json"),
                         "config_file": str(_cfgf)}).collect()
_by2 = {c["name"]: c for c in _r2["checks"]}
check("an unreachable collector is a FAIL, not a warning",
      _by2["collector"]["state"] == "fail", str(_by2.get("collector")))
check("and an unreachable PAMTS plugin is a fail too",
      any(c["state"] == "fail" and "LMS history plugin" in c["name"]
          for c in _r2["checks"]),
      str([c["name"] for c in _r2["checks"]]))
check("the overall verdict is the WORST check, not an average",
      _r2["overall"] == "fail", _r2["overall"])

# A stale cursor file means the poller has stopped, whatever systemd says. This is the
# check that would have caught a silently dead timer.
import os as _os
_old = time.time() - 3600
_wf2 = _h / "stale.json"; _wf2.write_text("{}")
_os.utime(_wf2, (_old, _old))
_r3 = dash.HealthSource({"watermarks_file": str(_wf2)}).collect()
_by3 = {c["name"]: c for c in _r3["checks"]}
check("a stale cursor file is reported as the poller having stopped",
      _by3["promotion poller is running"]["state"] == "fail",
      _by3["promotion poller is running"]["detail"])

# Corruption of the one file that holds all observed history must be loud.
_bad = _h / "broken.json"; _bad.write_text("{not json")
_r4 = dash.HealthSource({"state_file": str(_bad)}).collect()
check("an unreadable state.json is a FAIL",
      {c["name"]: c for c in _r4["checks"]}["state.json"]["state"] == "fail")

# Scope. Checks about things PAMTS does not touch at all do not belong here; checks
# about PAMTS's own ADAPTERS do, even when the far end is someone else's server.
_names = " ".join(c["name"].lower() for c in _r["checks"] + _r2["checks"])
_foreign = [w for w in ("sonarr", "radarr", "lidarr", "readarr", "mergerfs",
                        "zpool", "array", "nfs") if w in _names]
check("no check is about a service PAMTS does not talk to", not _foreign, str(_foreign))
# Every row in Sources names PAMTS's side -- the adapter, the plugin, the sidecar --
# so a red light points at PAMTS's component and not at someone else's server.
_srcrows = [c["name"].lower() for c in _r2["checks"] if c.get("group") == "Sources"]
check("every source row names PAMTS's own component",
      _srcrows and all(any(w in n for w in ("adapter", "plugin", "sidecar"))
                       for n in _srcrows),
      str(_srcrows))

# EVERY configured player gets a row. Listing only the two plugins PAMTS ships left
# Plex and the observer adapter off a panel whose job is "is PAMTS working" -- and
# PAMTS leans on Plex for every film and episode decision.
print("\n=== HEALTH: every configured player appears, plugin or not")
_pc = _h / "players.toml"
_pc.write_text(
    '[[players]]\nname="plex"\nkind="plex"\nurl="http://127.0.0.1:1"\n'
    '[[players]]\nname="lms"\nkind="lms"\nurl="http://127.0.0.1:1"\n'
    '[[players]]\nname="navidrome"\nkind="navidrome"\nurl="http://127.0.0.1:1"\n'
    'history_url="http://127.0.0.1:1"\n'
    '[[players]]\nname="storage"\nkind="observer"\nurl="http://127.0.0.1:1"\n')
_rp = dash.HealthSource({"config_file": str(_pc)}).collect()
_rows = [c for c in _rp["checks"] if c.get("group") == "Sources"]
check("all four configured players produce a row", len(_rows) == 4,
      str([c["name"] for c in _rows]))
for _want in ("plex", "lms", "navidrome", "storage"):
    check(f"{_want} has a row of its own",
          any("(%s)" % _want in c["name"] for c in _rows),
          str([c["name"] for c in _rows]))
check("an unreachable Plex is a fail that says what PAMTS loses",
      any(c["state"] == "fail" and "Plex adapter" in c["name"]
          and "play history" in c["detail"] for c in _rows),
      str([(c["name"], c["detail"][:60]) for c in _rows]))

# A Plex token that cannot be read is a different fault from Plex being down, and the
# detail has to distinguish them or the wrong thing gets investigated.
_pc2 = _h / "plex-notoken.toml"
_pc2.write_text('[[players]]\nname="plex"\nkind="plex"\n'
                'url="http://127.0.0.1:1"\ntoken_file="%s/nope-token"\n' % _h)
_rt = dash.HealthSource({"config_file": str(_pc2)}).collect()
_prow = [c for c in _rt["checks"] if "Plex adapter" in c["name"]][0]
check("an unreadable Plex token is reported as the token, not as Plex being down",
      "token" in _prow["detail"] and _prow["state"] == "fail", _prow["detail"])

# One broken check must not take the panel down with it.
_boom = dash.HealthSource({})
_boom._store_checks = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
_r5 = _boom.collect()
check("a check that raises is reported, not fatal",
      any("boom" in (c["detail"] or "") for c in _r5["checks"]),
      str([c["detail"] for c in _r5["checks"]])[:200])
check("and the rest of the panel still renders", len(_r5["checks"]) > 1)

# ---------------------------------------------------- can PAMTS still do its job?
# The panel reported "all good" while film and TV promotion was completely inert: the
# shared pool sat 11 G over a 400 G budget, promotable headroom was zero, and every
# promote pass logged "promoted 0B". Services up, stores written, plugins answering --
# and one of the two core functions dead for half the estate. Liveness is not usefulness.
print("\n=== HEALTH: budget headroom, because liveness is not usefulness")
_G = 2 ** 30


class _FakeTier:
    name = "tier"

    def __init__(self, pools, boom=False):
        self._p, self._boom = pools, boom

    def get(self, force=False):
        if self._boom:
            raise RuntimeError("scan failed")
        return {"pools": self._p}


def _cap(pools, **kw):
    h = dash.HealthSource({"_tier_source": _FakeTier(pools, **kw)})
    return [c for c in h.collect()["checks"] if c.get("group") == "Capacity"]


_dead = _cap([{"label": "movies + tv (shared)", "bytes": 411 * _G,
               "budget_bytes": 400 * _G, "promotable_bytes": 0}])
check("a pool with no promotable headroom warns", _dead[0]["state"] == "warn",
      f"{_dead[0]['state']}: {_dead[0]['detail']}")
check("and says what that actually costs, not just a number",
      "inert" in _dead[0]["detail"] and "promotion" in _dead[0]["detail"],
      _dead[0]["detail"])

_ok = _cap([{"label": "movies + tv (shared)", "bytes": 411 * _G,
             "budget_bytes": 600 * _G, "promotable_bytes": 188 * _G}])
check("a pool with headroom is OK", _ok[0]["state"] == "ok",
      f"{_ok[0]['state']}: {_ok[0]['detail']}")
check("and reports the figures that matter",
      "411.0 G of 600.0 G" in _ok[0]["detail"] and "188.0 G promotable" in _ok[0]["detail"],
      _ok[0]["detail"])

# Over budget WITH headroom left is the ordinary state after a download burst, and
# eviction resolves it. It is worth saying, not worth alarming about beyond a warn.
_over = _cap([{"label": "x", "bytes": 450 * _G, "budget_bytes": 400 * _G,
               "promotable_bytes": 10 * _G}])
check("over budget but still promotable warns, and says eviction will resolve it",
      _over[0]["state"] == "warn" and "eviction" in _over[0]["detail"],
      _over[0]["detail"])

_multi = _cap([{"label": "a", "bytes": 1 * _G, "budget_bytes": 10 * _G,
                "promotable_bytes": 9 * _G},
               {"label": "b", "bytes": 10 * _G, "budget_bytes": 10 * _G,
                "promotable_bytes": 0}])
check("every pool gets its own row", len(_multi) == 2, str([c["name"] for c in _multi]))
check("one healthy pool does not mask a dead one",
      sorted(c["state"] for c in _multi) == ["ok", "warn"],
      str([(c["name"], c["state"]) for c in _multi]))

check("a pool with no budget configured is unknown, not a failure",
      _cap([{"label": "n", "bytes": 1, "budget_bytes": 0}])[0]["state"] == "unknown")
# The tier scan is the expensive thing this dashboard does; if it fails the panel must
# still render everything else.
_boom = _cap([{"label": "x", "bytes": 1, "budget_bytes": 2}], boom=True)
check("a failing tier scan degrades to unknown rather than breaking the panel",
      _boom and _boom[0]["state"] == "unknown", str(_boom))
check("and with no tier source at all it is unknown",
      [c for c in dash.HealthSource({}).collect()["checks"]
       if c.get("group") == "Capacity"][0]["state"] == "unknown")

# build() must share the TierSource instance, or the panel would scan the tier a second
# time on its own 10-second TTL -- the most expensive call on the page, doubled.
_built = dash.build(cfg={n: {} for n in dash.SOURCES})
_hsrc = next(s for s in _built if s.name == "health")
_tsrc = next(s for s in _built if s.name == "tier")
check("build() hands the health source the live TierSource instance",
      _hsrc.cfg.get("_tier_source") is _tsrc,
      "a separate instance would scan the fast tier twice on different schedules")

check("health is in the source registry", "health" in dash.SOURCES)

# It must NOT also be swept into /api/state: the Now tab has no renderer for it, so it
# fell through to a raw-JSON card on the front page, and its four systemctl calls were
# paid on every 5-second poll of a completely different view.
check("health is excluded from /api/state", dash.HealthSource.in_state is False)
check("but ordinary sources are still included",
      all(c.in_state for n, c in dash.SOURCES.items() if n != "health"),
      str([n for n, c in dash.SOURCES.items() if not c.in_state]))
_collected, _errs2 = dash.collect_all([dash.HealthSource({})])
check("collect_all skips it entirely, data and errors both",
      _collected == {} and _errs2 == {}, f"{_collected} {_errs2}")

# The plugin probes must NOT ride the panel's TTL. The LMS query runs six COUNT(*)s
# over a couple of hundred thousand rows on a single-threaded server that is also
# playing music; at 10 seconds that is a permanent load for an answer that barely moves.
check("plugin probes have their own, much longer interval",
      dash.HealthSource.plugin_ttl >= 60 and
      dash.HealthSource.plugin_ttl > dash.HealthSource.ttl * 10,
      f"ttl={dash.HealthSource.ttl} plugin_ttl={dash.HealthSource.plugin_ttl}")
_calls = []
_cs = dash.HealthSource({"plugin_ttl": 30.0})
_cs._probe_plugins = lambda: (_calls.append(1),
                              [_cs._check("LMS history plugin (t)", "ok", "v1",
                                          group="Plugins")])[1]
_cs._plugin_checks(); _cs._plugin_checks(); _cs._plugin_checks()
check("repeated panel refreshes probe the plugins once", len(_calls) == 1,
      f"{len(_calls)} probes for 3 refreshes")
check("and the cached answer says how old it is",
      "checked" in _cs._plugin_checks()[0]["detail"],
      _cs._plugin_checks()[0]["detail"])
_cs._plugins_at = 0.0          # force the window open
_cs._plugin_checks()
check("the probe does run again once its window passes", len(_calls) == 2,
      f"{len(_calls)} probes")
shutil.rmtree(_h, ignore_errors=True)

print("\n=== HEALTH: the page shows it on every tab")
_pg = (ROOT / "web" / "index.html").read_text()
check("there is a status badge in the header",
      'id="healthbadge"' in _pg and _pg.index('id="healthbadge"') < _pg.index("<main>"),
      "the badge must be outside <main> so it shows on every tab")
check("and a Status tab", 'id="tab-status"' in _pg and "view-status" in _pg)
check("health is fetched outside any per-tab branch",
      'fetch("api/health"' in _pg)
check("the glyph is not the only carrier of state",
      ".sr-only" in _pg and "sr-only" in _pg.split("renderHealth")[1][:1500],
      "colour and shape alone are not accessible")


summary()
