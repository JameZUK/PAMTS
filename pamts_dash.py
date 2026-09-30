#!/usr/bin/env python3
"""Data sources for the PAMTS dashboard.

The dashboard deliberately knows nothing about where its data comes from. Each
SOURCE answers for one thing -- the access observer, the promotion state file, the
config, the tier footprint -- and the page renders whatever it is given.

Adding a source is one class and one registry line, which is the same shape as
adding a player adapter (see pamts_players.ADAPTERS). Nothing else changes: the
API merges every source's output and the page iterates over it.

A source that fails does not break the page. Its error is reported alongside the
data that did arrive, because a dashboard that shows nothing when one component is
down is worse than one that shows most of the truth and says what is missing.

Standard library only, in keeping with the rest of PAMTS.
"""
import abc
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request


class Source(abc.ABC):
    """One provider of dashboard data.

    `collect()` returns a JSON-serialisable dict. Raise anything on failure: the
    caller isolates it and reports it as that source's error.
    """
    name = "abstract"
    #: How stale this source's data may be before it is collected again. Scanning
    #: a filesystem is expensive; reading an HTTP endpoint is not.
    ttl = 0.0

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self._cache = None
        self._cached_at = 0.0

    def available(self):
        """Is this source usable at all? Cheap check, no data fetched."""
        return True

    @abc.abstractmethod
    def collect(self):
        """-> dict of this source's current data."""

    def get(self, force=False):
        now = time.monotonic()
        if not force and self._cache is not None and now - self._cached_at < self.ttl:
            return self._cache
        data = self.collect()
        self._cache, self._cached_at = data, now
        return data


def _http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class ObserverSource(Source):
    """The access observer daemon: what is being read right now, and its health."""
    name = "observer"
    ttl = 2.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.url = str(self.cfg.get("url", "http://127.0.0.1:8621")).rstrip("/")

    def available(self):
        try:
            _http_json(self.url + "/health", timeout=3)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def collect(self):
        stats = _http_json(self.url + "/stats")
        sessions = _http_json(self.url + "/sessions").get("sessions", [])
        # A tap that has stopped looks exactly like an idle estate from outside, so
        # surface the one number that distinguishes them rather than making whoever
        # is reading the page work it out.
        emitted, records = stats.get("kernel_emitted"), stats.get("records")
        healthy = emitted is None or records is None or emitted == records
        return {
            "stats": stats,
            "sessions": sessions,
            "playing": [s for s in sessions if s.get("label") == "PLAY"],
            "keeping_up": healthy,
            "lost_events": (None if emitted is None or records is None
                            else emitted - records),
        }


class StateSource(Source):
    """PAMTS's own state: recent promotions and the measured throughput."""
    name = "state"
    ttl = 5.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.path = self.cfg.get("state_file", "/var/lib/pamts/state.json")

    def available(self):
        return os.path.exists(self.path)

    def collect(self):
        with open(self.path) as f:
            s = json.load(f)
        proms = s.get("promotions") or {}
        recent = sorted(
            ({"dir": k,
              "at": v.get("at"),
              "label": v.get("label"),
              "last_rel": v.get("last_rel")}
             for k, v in proms.items() if isinstance(v, dict)),
            key=lambda p: p.get("at") or 0, reverse=True)
        return {
            "promotions": len(proms),
            "recent_promotions": recent[:20],
            "rate_bps": s.get("rate_bps"),
            "observed_plays": len(s.get("observed") or {}),
        }


class ConfigSource(Source):
    """What PAMTS has been told to manage: budget, jobs, roots."""
    name = "config"
    ttl = 30.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.path = self.cfg.get("config_file")

    def available(self):
        return bool(self.path) and os.path.exists(self.path)

    def collect(self):
        import tomllib                                          # noqa: PLC0415
        with open(self.path, "rb") as f:
            raw = tomllib.load(f)
        tier = raw.get("tier") or {}
        return {
            "budget_gb": tier.get("budget_gb"),
            "next_up_max_items": tier.get("next_up_max_items"),
            "next_up_max_gb": tier.get("next_up_max_gb"),
            "jobs": [{"name": j.get("name"), "mode": j.get("mode"),
                      "source": j.get("source"), "dest": j.get("dest"),
                      "depth": j.get("depth"), "max_delete": j.get("max_delete")}
                     for j in (raw.get("jobs") or [])],
            "players": [{"name": p.get("name"), "kind": p.get("kind")}
                        for p in (raw.get("players") or [])],
            "roots": len(raw.get("roots") or []),
        }


class TierSource(Source):
    """How full the fast tier is, per tier job.

    Scanning is the expensive thing this dashboard does, hence the long TTL. It is
    also the number people actually want, so it is worth the cost.
    """
    name = "tier"
    ttl = 120.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.jobs = self.cfg.get("tier_jobs") or []
        self.budget_gb = self.cfg.get("budget_gb")

    def available(self):
        return bool(self.jobs)

    @staticmethod
    def _usage(path):
        total = files = 0
        for root, _dirs, names in os.walk(path):
            for n in names:
                try:
                    total += os.lstat(os.path.join(root, n)).st_size
                    files += 1
                except OSError:
                    continue
        return total, files

    def collect(self):
        out, grand = [], 0
        for j in self.jobs:
            src = j.get("source")
            if not src or not os.path.isdir(src):
                out.append({"name": j.get("name"), "bytes": None, "files": None,
                            "error": "not a directory"})
                continue
            b, f = self._usage(src)
            grand += b
            out.append({"name": j.get("name"), "source": src,
                        "bytes": b, "files": f})
        budget = int(self.budget_gb) * 1024 ** 3 if self.budget_gb else None
        return {
            "jobs": out,
            "used_bytes": grand,
            "budget_bytes": budget,
            "used_fraction": (grand / budget) if budget else None,
        }


class HistorySource(Source):
    """Recent sessions and play history, read straight from the observer's store.

    This reads the SQLite file rather than the HTTP API because the API exposes
    play history but not the classified session log, and the session log is what
    makes the history view worth looking at -- it shows what was REJECTED as well
    as what counted.
    """
    name = "history"
    ttl = 3.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.path = self.cfg.get("observer_db", "/var/lib/pamts/observer.db")
        self.limit = int(self.cfg.get("history_limit", 200))

    def available(self):
        return os.path.exists(self.path)

    def _conn(self):
        # Read-only, so a running daemon is never disturbed.
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)

    def collect(self, since=0.0, limit=None):
        limit = int(limit or self.limit)
        con = self._conn()
        try:
            sessions = [
                {"ts": r[0], "label": r[1], "client": r[2], "bytes": r[3],
                 "coverage": r[4], "duration": r[5], "rate": r[6], "path": r[7]}
                for r in con.execute(
                    "SELECT ts,label,client,bytes,coverage,duration,rate,path "
                    "FROM sessions WHERE ts > ? ORDER BY ts DESC LIMIT ?",
                    (since, limit))]
            plays = [
                {"path": r[0], "last_play": r[1], "play_count": r[2], "label": r[3]}
                for r in con.execute(
                    "SELECT path,last_play,play_count,last_label FROM plays "
                    "WHERE last_play > ? ORDER BY last_play DESC LIMIT ?",
                    (since, limit))]
            counts = {}
            for lab, n in con.execute(
                    "SELECT label, COUNT(*) FROM sessions WHERE ts > ? GROUP BY label",
                    (time.time() - 86400,)):
                counts[lab] = n
            return {"sessions": sessions, "plays": plays, "labels_24h": counts}
        finally:
            con.close()


#: Registry. Adding a source is one line here, exactly as with player adapters.
SOURCES = {
    ObserverSource.name: ObserverSource,
    StateSource.name: StateSource,
    ConfigSource.name: ConfigSource,
    TierSource.name: TierSource,
    HistorySource.name: HistorySource,
}


def build(names=None, cfg=None):
    """Instantiate the named sources (all of them by default)."""
    cfg = cfg or {}
    chosen = names or list(SOURCES)
    out = []
    for n in chosen:
        cls = SOURCES.get(n)
        if cls is None:
            raise ValueError(f"unknown source {n!r}; have: {', '.join(sorted(SOURCES))}")
        out.append(cls(cfg.get(n) or cfg))
    return out


def collect_all(sources, force=False):
    """-> (data, errors). One failing source never costs you the others."""
    data, errors = {}, {}
    for s in sources:
        try:
            if not s.available():
                errors[s.name] = "unavailable"
                continue
            data[s.name] = s.get(force=force)
        except Exception as e:                                  # noqa: BLE001
            errors[s.name] = f"{type(e).__name__}: {e}"
    return data, errors
