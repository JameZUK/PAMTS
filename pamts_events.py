"""Durable record of what PAMTS moved and how full each tier was.

`state.json` is a SNAPSHOT: it says what is protected right now, and every run
overwrites it. It therefore cannot answer "what was promoted last week", "how fast were
transfers overnight", or "is the music pool trending up" -- the questions a dashboard is
for. This is a small append-only store for exactly those.

Two tables, because two different shapes of question:

* `transfers` -- one row per item moved, either direction. Answers "what happened"
  (promotion and demotion history) and, through bytes/seconds, "how fast".
* `samples` -- one row per pool per run. Answers "how full, over time". Sampled rather
  than derived from transfers, because content also arrives and leaves by other means
  (a download, a manual delete) and a derived figure would drift from reality.

DELIBERATELY SEPARATE FROM THE OBSERVER'S DATABASE. That one is written continuously by
another process at a far higher rate; a tiering run must never block on it, and the
observer must never be slowed by a dashboard query.

EVERY FUNCTION HERE IS FAIL-SAFE. Recording is telemetry: if the database cannot be
opened or written, these return quietly rather than raising. Losing a graph is a
nuisance; aborting an eviction half way through because a log table was locked is a
real problem.
"""
import logging
import os
import sqlite3
import time

#: Set by configure(); until then, recording is a no-op rather than a guess at a path.
_PATH = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,          -- 'promote' | 'evict' | 'backup'
    pool    TEXT,                   -- budget pool, so graphs can separate media
    job     TEXT,                   -- tier/backup job name
    item    TEXT,                   -- path relative to the tier root
    bytes   INTEGER,
    seconds REAL,
    rate    REAL,                   -- bytes/second for THIS item
    label   TEXT,                   -- human description, e.g. "Show S01E02"
    reason  TEXT                    -- why: 'next-up', 'playing now', staleness, ...
);
CREATE INDEX IF NOT EXISTS transfers_ts ON transfers(ts);
CREATE INDEX IF NOT EXISTS transfers_kind_ts ON transfers(kind, ts);
CREATE TABLE IF NOT EXISTS samples (
    ts        REAL NOT NULL,
    pool      TEXT NOT NULL,
    footprint INTEGER,
    budget    INTEGER,
    reserve   INTEGER
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
"""


def configure(path):
    """Point the recorder at a database, creating it if need be."""
    global _PATH
    if not path:
        _PATH = None
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _connect(path) as c:
            c.executescript(SCHEMA)
        _PATH = path
    except (OSError, sqlite3.Error) as e:
        logging.warning("event store unavailable at %s (%s); history will not be "
                        "recorded", path, e)
        _PATH = None


def _connect(path=None):
    c = sqlite3.connect(path or _PATH, timeout=10.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def record_transfer(kind, item, nbytes, seconds=None, pool=None, job=None,
                    label=None, reason=None):
    """One item moved. Returns True if it was stored, for tests."""
    if not _PATH:
        return False
    rate = None
    if seconds and seconds > 0 and nbytes:
        rate = nbytes / seconds
    try:
        with _connect() as c:
            c.execute("INSERT INTO transfers(ts,kind,pool,job,item,bytes,seconds,"
                      "rate,label,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), kind, pool, job, item, nbytes, seconds, rate,
                       label, reason))
        return True
    except sqlite3.Error as e:
        logging.debug("could not record transfer: %s", e)
        return False


def record_sample(pool, footprint, budget, reserve=None):
    """How full one pool is, now."""
    if not _PATH:
        return False
    try:
        with _connect() as c:
            c.execute("INSERT INTO samples(ts,pool,footprint,budget,reserve) "
                      "VALUES(?,?,?,?,?)",
                      (time.time(), pool, footprint, budget, reserve))
        return True
    except sqlite3.Error as e:
        logging.debug("could not record sample: %s", e)
        return False


def prune(max_age_days=120):
    """Keep the store bounded. Returns rows removed."""
    if not _PATH:
        return 0
    cut = time.time() - max_age_days * 86400
    try:
        with _connect() as c:
            n = c.execute("DELETE FROM transfers WHERE ts < ?", (cut,)).rowcount
            n += c.execute("DELETE FROM samples WHERE ts < ?", (cut,)).rowcount
        return n
    except sqlite3.Error:
        return 0
