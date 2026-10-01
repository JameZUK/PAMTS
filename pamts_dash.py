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
        # A legitimate lag is bounded by the ring buffer (128 MB / 72 bytes is about
        # 1.8M events). Anything beyond this is the consumer genuinely falling behind.
        self.max_lag = int(self.cfg.get("max_lag", 2_000_000))

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
        #
        # kernel_dropped is the AUTHORITATIVE loss signal: it counts ring-buffer
        # reserve failures, which are unrecoverable. The emitted-vs-records gap is
        # not loss -- it is in-flight buffering, bounded by the ring buffer, and the
        # two counters are read non-atomically so records can even run one AHEAD.
        # Treating that as unhealthy made the indicator cry wolf on a +1 race, which
        # is worse than having no indicator at all.
        emitted, records = stats.get("kernel_emitted"), stats.get("records")
        dropped = stats.get("kernel_dropped")
        lag = None if emitted is None or records is None else emitted - records
        healthy = (dropped in (None, 0)) and (lag is None or lag <= self.max_lag)
        return {
            "stats": stats,
            "sessions": sessions,
            "playing": [s for s in sessions if s.get("label") == "PLAY"],
            "keeping_up": healthy,
            "dropped": dropped,
            "lag": lag,
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
        promote = raw.get("promote") or {}
        # Everything a reader might otherwise have to shell in and read the TOML for.
        # Deliberately whole: budget_gb, grace and exclude_from are exactly the fields
        # that explain WHY the system did what it did, and omitting them was why the
        # config view could not answer anything useful.
        jobs = [{"name": j.get("name"), "mode": j.get("mode"),
                 "source": j.get("source"), "dest": j.get("dest"),
                 "depth": j.get("depth"), "max_delete": j.get("max_delete"),
                 "budget_gb": j.get("budget_gb"), "grace": j.get("grace"),
                 "exclude_from": j.get("exclude_from")}
                for j in (raw.get("jobs") or [])]
        return {
            "path": self.path,
            "budget_gb": tier.get("budget_gb"),
            "next_up_max_items": tier.get("next_up_max_items"),
            "next_up_max_gb": tier.get("next_up_max_gb"),
            "jobs": jobs,
            "tier_jobs": sum(1 for j in jobs if j["mode"] == "tier"),
            "backup_jobs": sum(1 for j in jobs if j["mode"] == "backup"),
            "players": [{"name": p.get("name"), "kind": p.get("kind")}
                        for p in (raw.get("players") or [])],
            "player_names": [p.get("name") or p.get("kind")
                             for p in (raw.get("players") or [])],
            "promote_rules": promote.get("rules") or [],
            "tier_settings": {k: v for k, v in tier.items()},
            "promote_settings": {k: v for k, v in promote.items() if k != "rules"},
            "roots": len(raw.get("roots") or []),
            "root_map": [{"player_path": r.get("player_path"), "fast": r.get("fast"),
                          "slow": r.get("slow")} for r in (raw.get("roots") or [])],
        }


class TierSource(Source):
    """How full the fast tier is, PER BUDGET POOL.

    Scanning is the expensive thing this dashboard does, hence the long TTL. It is
    also the number people actually want, so it is worth the cost.

    Pools, not one total. A tier job may carve out its own `budget_gb`; everything else
    shares the global one. Summing every job's footprint and dividing by the shared
    budget produced "281.2% of 400.0G budget" -- 1.07 TB of video AND music measured
    against the 400 GB video allowance. That number was not merely ugly, it was
    meaningless: nothing was over budget at all.
    """
    name = "tier"
    ttl = 120.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.jobs = self.cfg.get("tier_jobs") or []
        self.budget_gb = self.cfg.get("budget_gb")
        self.reserve_gb = self.cfg.get("promote_headroom_gb") or 0

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
        GB = 1024 ** 3
        # float(), not int(): int() truncates a sub-1 GB budget to zero, which then
        # reads as "no budget configured" and silently hides every derived figure.
        shared = int(float(self.budget_gb) * GB) if self.budget_gb else None
        reserve = int(float(self.reserve_gb) * GB)
        out, pools = [], {}
        for j in self.jobs:
            own = j.get("budget_gb")
            key = j.get("name") if own else "__shared__"
            pool = pools.setdefault(key, {
                "pool": key,
                "label": j.get("name") if own else "movies + tv (shared)",
                "budget_bytes": int(float(own) * GB) if own else shared,
                "own_budget": bool(own), "bytes": 0, "files": 0, "jobs": []})
            src = j.get("source")
            if not src or not os.path.isdir(src):
                out.append({"name": j.get("name"), "pool": key, "bytes": None,
                            "files": None, "error": "not a directory"})
                continue
            b, f = self._usage(src)
            pool["bytes"] += b
            pool["files"] += f
            pool["jobs"].append(j.get("name"))
            out.append({"name": j.get("name"), "pool": key, "source": src,
                        "bytes": b, "files": f})
        for pool in pools.values():
            bud = pool["budget_bytes"]
            pool["reserve_bytes"] = reserve
            # Eviction evicts down to budget - reserve, so that is the line that
            # actually governs, and the one worth showing people.
            pool["target_bytes"] = max(0, bud - reserve) if bud else None
            pool["used_fraction"] = (pool["bytes"] / bud) if bud else None
            pool["free_bytes"] = (bud - pool["bytes"]) if bud else None
            pool["over_budget"] = bool(bud and pool["bytes"] > bud)
            pool["promotable_bytes"] = (max(0, pool["target_bytes"] - pool["bytes"])
                                        if pool["target_bytes"] is not None else None)
        ordered = sorted(pools.values(), key=lambda p: -(p["bytes"] or 0))
        return {
            "pools": ordered,
            "jobs": out,
            # A grand total is still useful ("how much is on the fast tier"), but it is
            # deliberately NOT divided by any budget: there is no single budget to
            # divide it by.
            "used_bytes": sum(p["bytes"] for p in pools.values()),
            "total_budget_bytes": sum(p["budget_bytes"] or 0 for p in pools.values()),
        }


#: What counts as someone actually wanting the file. Must match DEMAND in the
#: observer daemon: PROBE, COPY, BULK and IMPORT are the system declining to treat a
#: scan as demand, and they outnumber real plays by roughly 600 to 1 -- so an
#: unfiltered history view shows essentially no plays at all.
DEMAND_LABELS = ("PLAY", "FETCH")


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

    def collect(self, since=0.0, limit=None, labels=None):
        """`labels` restricts which verdicts are returned.

        Filtering here rather than in the page is not an optimisation, it is the
        difference between the view working and not: copies and scans outnumber plays
        by hundreds to one, so a client-side filter on the most recent N rows would
        discard almost all of them and show nothing.
        """
        limit = int(limit or self.limit)
        con = self._conn()
        try:
            where, params = "ts > ?", [since]
            if labels:
                labels = [str(l) for l in labels]
                where += " AND label IN (%s)" % ",".join("?" * len(labels))
                params += labels
            # `tier` was added later, so a database written by an older daemon does
            # not have the column. Ask once rather than letting every query fail.
            cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            tier_col = "tier" if "tier" in cols else "NULL AS tier"
            sessions = [
                {"ts": r[0], "label": r[1], "client": r[2], "bytes": r[3],
                 "coverage": r[4], "duration": r[5], "rate": r[6], "path": r[7],
                 "tier": r[8]}
                for r in con.execute(
                    f"SELECT ts,label,client,bytes,coverage,duration,rate,path,{tier_col} "
                    f"FROM sessions WHERE {where} ORDER BY ts DESC LIMIT ?",
                    (*params, limit))]
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
            # Offer the labels that actually exist rather than a hardcoded list, so
            # a verdict added later appears in the UI without the page changing.
            known = [r[0] for r in con.execute(
                "SELECT DISTINCT label FROM sessions WHERE ts > ? ORDER BY label",
                (time.time() - 7 * 86400,)) if r[0]]
            # Which tier served the demand reads over 24h. Restricted to PLAY/FETCH
            # because that is the number worth watching: a cold PLAY is a spin-up a
            # viewer waited for, while a cold COPY is just the backup doing its job
            # and would swamp the ratio.
            tier_counts = {}
            if "tier" in cols:
                qs = ",".join("?" * len(DEMAND_LABELS))
                for t, n in con.execute(
                        "SELECT COALESCE(tier,'unknown'), COUNT(*) FROM sessions "
                        f"WHERE ts > ? AND label IN ({qs}) GROUP BY 1",
                        (time.time() - 86400, *DEMAND_LABELS)):
                    tier_counts[t] = n
            return {"sessions": sessions, "plays": plays, "labels_24h": counts,
                    "available_labels": known, "demand_labels": list(DEMAND_LABELS),
                    "tier_24h": tier_counts, "tier_known": "tier" in cols,
                    "filtered_by": list(labels) if labels else None}
        finally:
            con.close()


#: Registry. Adding a source is one line here, exactly as with player adapters.
class EventsSource(Source):
    """Promotion and demotion history, throughput, and utilisation over time.

    Reads the append-only store pamts_events writes. Opened READ-ONLY so a tiering run
    recording a transfer is never blocked by someone refreshing the page.

    Throughput is bucketed server-side. A night of eviction is ten thousand rows; the
    page wants a line, and shipping ten thousand points to draw a few hundred pixels
    would make the response larger than the rest of the page put together.
    """
    name = "events"
    ttl = 5.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.path = self.cfg.get("events_db")

    def available(self):
        return bool(self.path) and os.path.exists(self.path)

    def _conn(self):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)

    def collect(self, since=None, limit=200, buckets=48, window=86400 * 7):
        now = time.time()
        since = now - window if since is None else since
        con = self._conn()
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(transfers)")}
            if not cols:
                return {"available": False}

            recent = [
                {"ts": r[0], "kind": r[1], "pool": r[2], "job": r[3], "item": r[4],
                 "bytes": r[5], "seconds": r[6], "rate": r[7], "label": r[8],
                 "reason": r[9]}
                for r in con.execute(
                    "SELECT ts,kind,pool,job,item,bytes,seconds,rate,label,reason "
                    "FROM transfers ORDER BY ts DESC LIMIT ?", (limit,))]

            totals = {}
            for kind, n, b in con.execute(
                    "SELECT kind, COUNT(*), COALESCE(SUM(bytes),0) FROM transfers "
                    "WHERE ts > ? GROUP BY kind", (since,)):
                totals[kind] = {"count": n, "bytes": b}

            # Throughput: bytes moved per bucket, split by direction. Width is derived
            # from the window so the same code serves an hour and a month.
            width = max(1.0, (now - since) / max(1, buckets))
            series = {}
            for kind, bucket, b, secs in con.execute(
                    "SELECT kind, CAST((ts - ?) / ? AS INTEGER), "
                    "COALESCE(SUM(bytes),0), COALESCE(SUM(seconds),0) "
                    "FROM transfers WHERE ts > ? GROUP BY 1, 2",
                    (since, width, since)):
                row = series.setdefault(kind, [None] * (buckets + 1))
                idx = min(int(bucket), buckets)
                row[idx] = {"bytes": b, "seconds": secs,
                            "rate": (b / secs) if secs else None}

            # Utilisation: the latest sample in each bucket, per pool. Latest rather
            # than averaged, because it is a level, not a flow.
            util = {}
            for pool, bucket, fp, bud in con.execute(
                    "SELECT pool, CAST((ts - ?) / ? AS INTEGER), footprint, budget "
                    "FROM samples WHERE ts > ? "
                    "GROUP BY 1, 2 HAVING ts = MAX(ts)", (since, width, since)):
                row = util.setdefault(pool, [None] * (buckets + 1))
                row[min(int(bucket), buckets)] = {"footprint": fp, "budget": bud}

            return {"available": True, "recent": recent, "totals": totals,
                    "window_s": now - since, "bucket_s": width, "buckets": buckets,
                    "since": since, "throughput": series, "utilisation": util}
        finally:
            con.close()


SOURCES = {
    ObserverSource.name: ObserverSource,
    StateSource.name: StateSource,
    ConfigSource.name: ConfigSource,
    TierSource.name: TierSource,
    EventsSource.name: EventsSource,
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
