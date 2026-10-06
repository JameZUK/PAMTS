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
import collections
import json
import os
import sqlite3
import subprocess
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
    #: Does collect_all() include this source? A source with its own endpoint and its
    #: own renderer sets this False, so it is not also swept into /api/state -- where
    #: the Now tab would have no renderer for it and would fall back to dumping it as
    #: raw JSON, and where its cost would be paid on every poll of a different view.
    in_state = True

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


#: Settings the dashboard may publish verbatim. Everything here is policy -- sizes,
#: counts, windows -- and none of it is a secret. Anything NOT listed is reported as
#: present-but-withheld rather than printed, so adding a key to the config can never
#: silently publish it.
TIER_PUBLIC = frozenset((
    "budget_gb", "settle_seconds", "new_grace_days", "promote_protect_days",
    "next_up_max_items", "next_up_max_gb", "next_up_include_unstarted",
    "next_up_unstarted_days", "next_up_unstarted_max", "pin_depth_max",
    "dest_fstypes", "inprogress_suffixes", "sidecar_suffixes",
))
PROMOTE_PUBLIC = frozenset((
    "headroom_gb", "headroom_fraction", "max_items", "max_bytes_gb", "min_free_gb",
    "min_event_items", "min_event_bytes_gb", "poll_seconds", "default_rate_mbps",
    "rate_alpha", "time_safety",
))

#: What a withheld value is replaced with.
WITHHELD = "(not published)"


def _publishable(table, allowed, skip=()):
    """Copy only allow-listed keys; name the rest without their values."""
    out = {}
    for k, v in (table or {}).items():
        if k in skip:
            continue
        out[k] = v if k in allowed else WITHHELD
    return out


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
            # ALLOW-listed, not pass-through. This endpoint exists to be read by
            # other people, and forwarding whole TOML tables means any key added to
            # [tier] or [promote] later is published by default -- including one
            # holding a credential. `players` was already narrowed this way; these
            # two were not. Unknown keys are reported by NAME only, so a new setting
            # is still visible without its value being broadcast.
            "tier_settings": _publishable(tier, TIER_PUBLIC),
            "promote_settings": _publishable(promote, PROMOTE_PUBLIC, skip=("rules",)),
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
            # Promotion fills to the BUDGET; eviction's floor is budget - reserve.
            # The reserve is the space between the two, so promotable room is measured
            # against the budget, not against the eviction target.
            pool["promotable_bytes"] = max(0, bud - pool["bytes"]) if bud else None
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

    #: The uid->service map changes only when the observer restarts, so it is fetched
    #: rarely. A stale map would name a session wrongly, which is worse than naming
    #: nothing, hence a bounded lifetime rather than caching it forever.
    map_ttl = 300.0

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.path = self.cfg.get("observer_db", "/var/lib/pamts/observer.db")
        self.limit = int(self.cfg.get("history_limit", 200))
        self.url = (self.cfg.get("url") or "").rstrip("/")
        self._map, self._map_at = None, 0.0

    def _service_map(self):
        """[(client|None, uid, name), ...] from the observer, cached.

        The sessions table stores the uid, never the name, so this is what turns a
        stored 101000 back into "plex". Failure is not fatal: without it rows simply
        carry their uid and no name, which is what the dashboard showed before.
        """
        now = time.monotonic()
        if self._map is not None and now - self._map_at < self.map_ttl:
            return self._map
        got = []
        if self.url:
            try:
                st = _http_json(self.url + "/stats", timeout=5)
                for e in st.get("service_map") or []:
                    got.append((e.get("client"), e.get("uid"), e.get("name")))
            except Exception:                                   # noqa: BLE001
                got = self._map or []       # keep the last good map over nothing
        self._map, self._map_at = got, now
        return got

    def _name_for(self, client, uid):
        """Same precedence as the observer's resolver: an exact (client, uid) pair
        wins over a bare uid, because 1000 is one service on one host and somebody
        else on the next."""
        if uid is None:
            return None
        pair = bare = None
        for c, u, n in self._service_map():
            if u != uid:
                continue
            if c == client:
                pair = n
            elif c is None:
                bare = n
        return pair or bare

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
            # uid was added after tier, so guard it the same way: a database written by
            # an older daemon has neither, and one failed query would empty the view.
            uid_col = "uid" if "uid" in cols else "NULL AS uid"
            sessions = [
                {"ts": r[0], "label": r[1], "client": r[2], "bytes": r[3],
                 "coverage": r[4], "duration": r[5], "rate": r[6], "path": r[7],
                 "tier": r[8], "uid": r[9],
                 "service": self._name_for(r[2], r[9])}
                for r in con.execute(
                    f"SELECT ts,label,client,bytes,coverage,duration,rate,path,"
                    f"{tier_col},{uid_col} "
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
            #
            # Written as an explicit join on the per-bucket MAX(ts). The previous form
            # was `GROUP BY 1,2 HAVING ts = MAX(ts)` with footprint and budget as bare
            # columns -- SQLite only promises those come from the row holding the
            # extreme when the min/max is in the SELECT list, and here it was in HAVING,
            # so the chart could plot an arbitrary sample from the bucket instead of
            # the latest one.
            util = {}
            for pool, bucket, fp, bud in con.execute(
                    "SELECT s.pool, CAST((s.ts - ?) / ? AS INTEGER) AS b, "
                    "       s.footprint, s.budget "
                    "FROM samples s JOIN ("
                    "  SELECT pool, CAST((ts - ?) / ? AS INTEGER) AS b, MAX(ts) AS mts "
                    "  FROM samples WHERE ts > ? GROUP BY pool, b"
                    ") m ON m.pool = s.pool AND m.mts = s.ts "
                    "WHERE s.ts > ?",
                    (since, width, since, width, since, since)):
                row = util.setdefault(pool, [None] * (buckets + 1))
                row[min(int(bucket), buckets)] = {"footprint": fp, "budget": bud}

            return {"available": True, "recent": recent, "totals": totals,
                    "window_s": now - since, "bucket_s": width, "buckets": buckets,
                    "since": since, "throughput": series, "utilisation": util}
        finally:
            con.close()


class HealthSource(Source):
    """Is PAMTS itself working? Its own services, stores and plugins -- nothing else.

    DELIBERATELY NARROW. This does not report on Plex, Lyrion, Navidrome, the *arr apps,
    the array or the unions. Those have their own dashboards and their own failure modes,
    and folding them in here would turn "is PAMTS working" into "is the estate working",
    which is a different question with a different answer. What is checked is only what
    PAMTS owns: the units it ships, the files it writes, the collector it loads and the
    two plugins it provides.

    The checks are chosen from things that have actually gone wrong:

      * a unit that runs on a timer and exits non-zero every time, while the work it
        does partly succeeds -- invisible for 144 consecutive runs
      * the collector dropping events because userspace fell behind
      * state.json unreadable, which is where all the observed play history lives
      * a run that silently stops happening at all

    That last one is why the play cursors are checked for FRESHNESS rather than just
    existence: they advance on every poll, so a stale cursor file is direct evidence
    that the poller is not running, independent of what systemd claims.

    Every check degrades to "unknown" rather than failing the panel. A dashboard that
    cannot tell you about one thing must still tell you about the rest.
    """
    name = "health"
    ttl = 10.0
    #: Served by /api/health alone. See Source.in_state.
    in_state = False

    #: The plugin probes get their OWN, much longer interval. The LMS query runs six
    #: COUNT(*)s over a couple of hundred thousand rows, on a server that is
    #: single-threaded and is also playing music; at the panel's 10-second TTL that
    #: would be six aggregate scans every ten seconds, for ever, to answer a question
    #: whose answer changes slowly. The last result is reused in between and carries the
    #: age it was taken at, so nothing is silently stale.
    plugin_ttl = 300.0

    #: state ranking, worst wins when rolling up to an overall verdict
    RANK = {"ok": 0, "unknown": 1, "warn": 2, "fail": 3}

    #: PAMTS's own units. (unit, kind, expected interval in seconds or None)
    UNITS = (
        ("pamts-observer.service", "daemon", None),
        ("pamts-web.service", "daemon", None),
        ("pamts-promote.timer", "timer", 60),
        ("pamts-nightly.timer", "timer", 86400),
    )

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.observer_url = (self.cfg.get("url") or "").rstrip("/")
        self.state_file = self.cfg.get("state_file")
        self.events_db = self.cfg.get("events_db")
        self.observer_db = self.cfg.get("observer_db")
        self.config_file = self.cfg.get("config_file")
        self.watermarks_file = self.cfg.get("watermarks_file")
        self.units = self.cfg.get("health_units") or [u[0] for u in self.UNITS]
        if self.cfg.get("plugin_ttl"):
            self.plugin_ttl = float(self.cfg["plugin_ttl"])
        self._plugins = None
        self._plugins_at = 0.0

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _check(name, state, detail, value=None, group="PAMTS"):
        return {"name": name, "state": state, "detail": detail, "value": value,
                "group": group}

    @staticmethod
    def _age(path):
        try:
            return time.time() - os.path.getmtime(path)
        except OSError:
            return None

    def _systemctl(self, unit, props):
        """-> {prop: value}, or None when systemd cannot be consulted."""
        try:
            out = subprocess.run(
                ["systemctl", "show", "--no-pager",
                 "-p", ",".join(props), unit],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0 and not out.stdout.strip():
            return None
        got = {}
        for line in out.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                got[k] = v
        return got or None

    # -- the checks --------------------------------------------------------
    def _unit_checks(self):
        out = []
        for unit, kind, interval in self.UNITS:
            if unit not in self.units:
                continue
            if kind == "timer":
                svc = unit[:-len("timer")] + "service"
                info = self._systemctl(unit, ("ActiveState", "LastTriggerUSec",
                                              "NextElapseUSecRealtime"))
                if info is None:
                    out.append(self._check(unit, "unknown",
                                           "systemd could not be consulted"))
                    continue
                active = info.get("ActiveState")
                if active != "active":
                    out.append(self._check(unit, "fail",
                                           f"timer is {active or 'unknown'}, so the "
                                           "work it schedules is not happening"))
                    continue
                # How long since it last fired, against how often it should.
                last = info.get("LastTriggerUSec") or ""
                detail = "scheduled"
                state = "ok"
                svc_info = self._systemctl(svc, ("Result", "ExecMainStatus",
                                                 "ExecMainExitTimestamp"))
                if svc_info:
                    status = svc_info.get("ExecMainStatus")
                    result = svc_info.get("Result")
                    # The failure mode this check exists for: a timer firing happily
                    # while the service it starts exits non-zero every single time.
                    if status not in (None, "", "0") or (result and result != "success"):
                        state = "fail"
                        detail = (f"last run FAILED: result={result or '?'} "
                                  f"exit={status or '?'} at "
                                  f"{svc_info.get('ExecMainExitTimestamp') or '?'}")
                    else:
                        # Kept on one line on purpose: a newline inside an f-string
                        # expression needs Python 3.12 (PEP 701), and this runs on 3.11.
                        when = svc_info.get("ExecMainExitTimestamp") or "no recorded exit"
                        detail = f"last run ok ({when})"
                out.append(self._check(unit, state, detail,
                                       {"last_trigger": last,
                                        "next_elapse": info.get("NextElapseUSecRealtime"),
                                        "interval_s": interval}))
            else:
                info = self._systemctl(unit, ("ActiveState", "SubState",
                                              "ActiveEnterTimestamp", "NRestarts"))
                if info is None:
                    out.append(self._check(unit, "unknown",
                                           "systemd could not be consulted"))
                    continue
                active = info.get("ActiveState")
                sub = info.get("SubState")
                restarts = info.get("NRestarts")
                if active == "active":
                    detail = f"running since {info.get('ActiveEnterTimestamp') or '?'}"
                    state = "ok"
                    if restarts and restarts.isdigit() and int(restarts) > 0:
                        detail += f", {restarts} restart(s)"
                else:
                    state, detail = "fail", f"{active or '?'}/{sub or '?'}"
                out.append(self._check(unit, state, detail, {"restarts": restarts}))
        return out

    def _collector_checks(self):
        out, group = [], "Collector"
        if not self.observer_url:
            return [self._check("collector", "unknown", "no observer url configured",
                                group=group)]
        st = getattr(self, "_stats", None)
        if st is None:
            return [self._check("collector", "fail",
                                "the observer API did not answer", group=group)]
        kd = st.get("kernel_dropped")
        ud = st.get("userspace_dropped")
        pend = st.get("userspace_pending")
        out.append(self._check(
            "events captured", "ok",
            f"{st.get('records', 0):,} records in {(st.get('uptime_s') or 0) / 3600:.1f}h, "
            f"{st.get('sessions_closed', 0):,} sessions closed",
            {"records": st.get("records"), "uptime_s": st.get("uptime_s")}, group))
        # Drops are the collector's own honesty check: it counts what it could not keep.
        if kd in (None, 0) and ud in (None, 0):
            out.append(self._check("no events dropped", "ok",
                                   "kernel and userspace both kept up",
                                   {"kernel": kd, "userspace": ud}, group))
        else:
            out.append(self._check(
                "events dropped", "warn",
                f"kernel dropped {kd or 0:,}, userspace dropped {ud or 0:,} "
                f"({pend or 0:,} pending now) - ranking is working from an "
                "incomplete view",
                {"kernel": kd, "userspace": ud, "pending": pend}, group))
        # The index is how an inode becomes a path; a stale one means reads cannot be
        # attributed to files at all.
        files = st.get("index_files") or 0
        unres = st.get("unresolved_paths") or 0
        if not files:
            out.append(self._check("path index", "fail",
                                   "the index is empty, so no read can be named",
                                   group=group))
        else:
            ratio = unres / max(1, st.get("records") or 1)
            out.append(self._check(
                "path index", "warn" if ratio > 0.01 else "ok",
                f"{files:,} files indexed, {unres:,} read(s) unresolved "
                f"(built in {st.get('index_build_s')}s)",
                {"files": files, "unresolved": unres}, group))
        folded = st.get("folded_requests")
        if folded:
            out.append(self._check(
                "held requests folded", "ok",
                f"{folded:,} request(s) folded out of open sessions to bound memory - "
                "something read one file very hard; byte counts and rates stay exact",
                {"folded": folded}, group))
        svc = st.get("services") or {}
        unmapped = {k: v for k, v in svc.items() if str(k).startswith("unmapped:")}
        if svc:
            out.append(self._check(
                "read attribution", "warn" if unmapped else "ok",
                (f"{len(unmapped)} client/uid pair(s) not named by --service"
                 if unmapped else
                 "every reader seen so far resolves to a named service"),
                svc, group))
        return out

    def _store_checks(self):
        out, group = [], "Stores"
        st = getattr(self, "_stats", None)
        # state.json: the only copy of the observed play history.
        if self.state_file:
            age = self._age(self.state_file)
            corrupt = os.path.exists(self.state_file + ".corrupt")
            if age is None:
                out.append(self._check("state.json", "warn",
                                       "not written yet", group=group))
            else:
                try:
                    with open(self.state_file) as f:
                        st = json.load(f)
                    obs = len(st.get("observed") or {})
                    pro = len(st.get("promotions") or {})
                    detail = (f"{obs:,} observed play(s), {pro} protected item(s), "
                              f"written {_ago(age)} ago")
                    state = "ok"
                    if corrupt:
                        state = "warn"
                        detail += " - a .corrupt copy exists from an earlier failure"
                    out.append(self._check("state.json", state, detail,
                                           {"observed": obs, "promotions": pro,
                                            "age_s": age}, group))
                except (OSError, ValueError) as e:
                    out.append(self._check(
                        "state.json", "fail",
                        f"UNREADABLE ({e}). This file holds all observed play history; "
                        "eviction will refuse any pool it leaves uncovered",
                        group=group))
        # The cursors advance every poll, so their age is independent evidence that
        # the poller is actually running -- not just that systemd thinks it is.
        if self.watermarks_file:
            age = self._age(self.watermarks_file)
            if age is None:
                out.append(self._check(
                    "promotion poller is running", "unknown",
                    "no cursor file yet; it appears after the first pass", group=group))
            else:
                limit = 5 * 60
                out.append(self._check(
                    "promotion poller is running",
                    "ok" if age < limit else "fail",
                    (f"play cursors advanced {_ago(age)} ago"
                     if age < limit else
                     f"play cursors last advanced {_ago(age)} ago - the poller has "
                     "stopped running, whatever systemd reports"),
                    {"age_s": age}, group))
        # Age is taken from the NEWEST ROW, not the file's mtime. Both databases are in
        # WAL mode, where the .db file is only touched at a checkpoint -- so mtime can
        # read half an hour stale while rows are arriving every second, which is exactly
        # the wrong way round for a liveness check.
        for label, path, table, stale, why in (
                ("events.db", self.events_db, "transfers", None,
                 "transfer and utilisation history for the graphs"),
                ("observer.db", self.observer_db, "sessions", 3600,
                 "sessions and play history")):
            if not path:
                continue
            if not os.path.exists(path):
                out.append(self._check(label, "warn", f"absent ({why})", group=group))
                continue
            try:
                size = os.path.getsize(path)
                for suffix in ("-wal", "-shm"):
                    if os.path.exists(path + suffix):
                        size += os.path.getsize(path + suffix)
                con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
                try:
                    rows, newest = con.execute(
                        "SELECT COUNT(*), MAX(ts) FROM %s" % table).fetchone()
                finally:
                    con.close()
            except (OSError, sqlite3.Error) as e:
                out.append(self._check(label, "warn", f"cannot be read: {e}",
                                       group=group))
                continue
            age = (time.time() - newest) if newest else None
            state, detail = "ok", f"{size / 1e6:.1f} MB, {rows:,} {table}"
            detail += (f", newest {_ago(age)} ago" if age is not None
                       else ", none recorded yet")
            # Age ALONE is not a fault. The first version of this warned whenever the
            # estate was simply quiet -- nothing had been read since 06:00, so the
            # panel sat amber all morning for correct behaviour, which is the fastest
            # way to teach someone to ignore a status light.
            #
            # The real fault is sessions OPEN but never being written: reads arriving
            # and nothing coming out the other end. With nothing open, an old newest
            # row means the estate is idle, which is not PAMTS's problem to report.
            if stale and age is not None and age > stale:
                open_now = (st or {}).get("sessions_open")
                if open_now:
                    state = "warn"
                    detail += (f" - {open_now} session(s) open but nothing written "
                               "for over an hour")
                else:
                    detail += " - nothing reading at the moment"
            elif not rows:
                state = "warn"
            out.append(self._check(label, state, detail,
                                   {"bytes": size, "rows": rows, "age_s": age}, group))
        return out

    def _plugin_checks(self):
        """Cached wrapper: probe the plugins at plugin_ttl, not at the panel's ttl."""
        now = time.monotonic()
        if self._plugins is not None and now - self._plugins_at < self.plugin_ttl:
            age = time.time() - self._plugins_age
            return [dict(c, detail=(c["detail"] + f" · checked {_ago(age)} ago")
                         if c.get("detail") else c["detail"])
                    for c in self._plugins]
        self._plugins = self._probe_plugins()
        self._plugins_at, self._plugins_age = now, time.time()
        return self._plugins

    def _probe_plugins(self):
        """Can PAMTS get what it needs from each configured source?

        ONE ROW PER CONFIGURED PLAYER, whether or not PAMTS ships a plugin for it.
        An earlier version listed only the two plugins PAMTS provides, which left Plex
        and the observer adapter missing from a panel whose whole job is "is PAMTS
        working" -- and PAMTS depending on Plex for every film and episode decision is
        very much part of whether PAMTS is working.

        What is reported is still PAMTS's SIDE of each integration: can this adapter
        authenticate, reach its endpoint, and get the data PAMTS asks for. It is not a
        health check for Plex, Lyrion or Navidrome. Where PAMTS ships the component --
        the LMS plugin and the Navidrome sidecar -- the row names that component, so a
        failure points at PAMTS's code rather than someone else's server.
        """
        out, group = [], "Sources"
        players = self._players()
        for p in players:
            kind = (p.get("kind") or "").lower()
            if kind == "plex":
                out.append(self._plex_check(p, group))
            elif kind == "observer":
                out.append(self._observer_adapter_check(p, group))
            elif kind == "lms":
                url = (p.get("url") or "").rstrip("/")
                if not url:
                    continue
                try:
                    body = json.dumps({"id": 1, "method": "slim.request",
                                       "params": ["", ["pamts", "info", "?"]]}).encode()
                    req = urllib.request.Request(
                        url + "/jsonrpc.js", data=body,
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        res = (json.loads(r.read().decode("utf-8", "replace"))
                               .get("result") or {})
                except Exception as e:                          # noqa: BLE001
                    out.append(self._check(
                        f"LMS history plugin ({p.get('name')})", "fail",
                        f"the PAMTS CLI query did not answer: {e}. Without it LMS "
                        "supplies no play history", group=group))
                    continue
                if "played" not in res:
                    out.append(self._check(
                        f"LMS history plugin ({p.get('name')})", "fail",
                        "the server answered but does not know the `pamts` query - "
                        "the plugin is not loaded (check for a stray copy in the "
                        "Plugins directory)", group=group))
                    continue
                out.append(self._check(
                    f"LMS history plugin ({p.get('name')})", "ok",
                    f"v{res.get('version')} - {res.get('played'):,} played of "
                    f"{res.get('tracks'):,} track(s)",
                    {"version": res.get("version"), "played": res.get("played")}, group))
            elif kind == "navidrome" and p.get("history_url"):
                base = p["history_url"].rstrip("/")
                try:
                    info = _http_json(base + "/info", timeout=8)
                except Exception as e:                          # noqa: BLE001
                    out.append(self._check(
                        f"Navidrome history sidecar ({p.get('name')})", "fail",
                        f"did not answer at {base}: {e}. Without it PAMTS sees no "
                        "Navidrome plays", group=group))
                    continue
                users = info.get("users_with_plays")
                state = "ok"
                detail = (f"v{info.get('version')} - {info.get('played'):,} played "
                          f"track(s) across {users} user(s)")
                if users == 1:
                    # The sidecar exists specifically to cover every listener, so one
                    # user is worth pointing out without calling it broken.
                    detail += " (only one listener has plays recorded)"
                out.append(self._check(
                    f"Navidrome history sidecar ({p.get('name')})", state, detail,
                    {"version": info.get("version"), "played": info.get("played"),
                     "users": users}, group))
        if not out:
            out.append(self._check("sources", "unknown",
                                   "no players are configured", group=group))
        return out

    def _plex_check(self, p, group):
        """PAMTS reads Plex over its HTTP API with a token -- there is no PAMTS plugin
        for Plex, which is why this row names the ADAPTER. The probe is the same request
        the adapter makes, including the token, so it fails if authentication is what is
        broken rather than only if the server is down."""
        name = f"Plex adapter ({p.get('name')})"
        url = (p.get("url") or "").rstrip("/")
        if not url:
            return self._check(name, "fail", "no url configured", group=group)
        token = None
        tf = p.get("token_file")
        if tf:
            try:
                with open(tf) as f:
                    token = f.read().strip()
            except OSError as e:
                return self._check(
                    name, "fail",
                    f"cannot read the token at {tf} ({e}) - PAMTS cannot ask Plex "
                    "anything without it", group=group)
            if not token:
                return self._check(name, "fail", f"the token file {tf} is empty",
                                   group=group)
        try:
            # The token goes in a header, never the query string: a query parameter
            # ends up in Plex's access log and any proxy's.
            req = urllib.request.Request(
                url + "/library/sections",
                headers={"Accept": "application/json",
                         **({"X-Plex-Token": token} if token else {})})
            with urllib.request.urlopen(req, timeout=8) as r:
                doc = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return self._check(
                    name, "fail",
                    f"Plex refused the token (HTTP {e.code}) - PAMTS is getting no "
                    "play history or sessions for film and TV", group=group)
            return self._check(name, "fail", f"HTTP {e.code} from {url}", group=group)
        except Exception as e:                                  # noqa: BLE001
            return self._check(
                name, "fail",
                f"could not reach {url}: {e} - PAMTS is getting no play history or "
                "sessions for film and TV", group=group)
        secs = ((doc.get("MediaContainer") or {}).get("Directory")) or []
        titles = [d.get("title") for d in secs if d.get("title")]
        return self._check(
            name, "ok" if titles else "warn",
            (f"authenticated, {len(titles)} library section(s): "
             + ", ".join(titles[:4]) + ("..." if len(titles) > 4 else ""))
            if titles else "authenticated, but Plex reports no library sections",
            {"sections": titles}, group)

    def _observer_adapter_check(self, p, group):
        """The `observer` player is PAMTS reading its OWN collector, so this row says
        whether the adapter's endpoint answers and has play history to give. The
        collector's internals are covered in the Collector group; this is the adapter
        in front of it."""
        name = f"Observer adapter ({p.get('name')})"
        url = (p.get("url") or "").rstrip("/")
        if not url:
            return self._check(name, "fail", "no url configured", group=group)
        try:
            st = _http_json(url + "/stats", timeout=5)
        except Exception as e:                                  # noqa: BLE001
            return self._check(name, "fail",
                               f"PAMTS's own collector API did not answer: {e}",
                               group=group)
        plays = st.get("plays")
        return self._check(
            name, "ok" if plays else "warn",
            (f"{plays:,} observed play(s) available to ranking"
             if plays else
             "reachable, but no observed plays recorded yet - this builds up as "
             "things are played"),
            {"plays": plays}, group)

    def _players(self):
        if not self.config_file or not os.path.exists(self.config_file):
            return []
        try:
            import tomllib                                      # noqa: PLC0415
            with open(self.config_file, "rb") as f:
                return tomllib.load(f).get("players") or []
        except Exception:                                       # noqa: BLE001
            return []

    def _observer_stats(self):
        """One fetch per panel refresh, shared by the collector and store checks."""
        if not self.observer_url:
            return None
        try:
            return _http_json(self.observer_url + "/stats", timeout=5)
        except Exception:                                       # noqa: BLE001
            return None

    def _capacity_checks(self):
        """Can PAMTS still DO its job, per budget pool?

        The panel answered "all good" while film and TV promotion was completely inert:
        the shared pool sat 11 G over a 400 G budget, so promotable headroom was zero and
        every pass logged "promoted 0B". Services were up, stores were written, plugins
        answered -- and one of the two core functions was dead for half the estate.
        Liveness is not usefulness, and this is the check that tells them apart.

        A pool over budget is NOT automatically a fault. It is the ordinary state after a
        burst of downloads, and eviction is what resolves it on the next run. What matters
        is whether there is room to promote INTO: at zero, playback-triggered promotion
        and next-up fetching cannot run at all.
        """
        out, group = [], "Capacity"
        pools = None
        for src in (self.cfg.get("_tier_source"),):
            if src is not None:
                try:
                    pools = (src.get() or {}).get("pools")
                except Exception:                               # noqa: BLE001
                    pools = None
        if pools is None:
            return [self._check("budget headroom", "unknown",
                                "the tier source is not available to this panel",
                                group=group)]
        for p in pools:
            name = p.get("label") or p.get("pool") or "?"
            used = p.get("bytes") or 0
            budget = p.get("budget_bytes") or 0
            promotable = p.get("promotable_bytes")
            frac = (used / budget) if budget else None
            if not budget:
                out.append(self._check(f"headroom: {name}", "unknown",
                                       "no budget configured", group=group))
                continue
            detail = (f"{used / 2**30:.1f} G of {budget / 2**30:.1f} G "
                      f"({frac * 100:.0f}%), {(promotable or 0) / 2**30:.1f} G promotable")
            if not promotable:
                state = "warn"
                detail += (" - NOTHING can be promoted into this pool, so "
                           "playback-triggered promotion and next-up fetching are "
                           "inert for it until eviction frees space")
            elif frac is not None and frac > 1.0:
                state = "warn"
                detail += " - over budget; eviction should resolve it on the next run"
            else:
                state = "ok"
            out.append(self._check(f"headroom: {name}", state, detail,
                                   {"bytes": used, "budget_bytes": budget,
                                    "promotable_bytes": promotable}, group))
        if not out:
            out.append(self._check("budget headroom", "unknown",
                                   "no tier pools configured", group=group))
        return out

    def collect(self):
        checks = []
        self._stats = self._observer_stats()
        for fn in (self._unit_checks, self._collector_checks, self._store_checks,
                   self._capacity_checks, self._plugin_checks):
            try:
                checks.extend(fn())
            except Exception as e:                              # noqa: BLE001
                # One broken check must not take the panel with it.
                checks.append(self._check(fn.__name__, "unknown",
                                          f"this check itself failed: {e}"))
        worst = max((self.RANK.get(c["state"], 1) for c in checks), default=0)
        overall = {v: k for k, v in self.RANK.items()}[worst]
        counts = collections.Counter(c["state"] for c in checks)
        return {"generated": time.time(), "overall": overall,
                "counts": dict(counts), "checks": checks}


def _ago(seconds):
    """Short human duration, for check details."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


SOURCES = {
    HealthSource.name: HealthSource,
    ObserverSource.name: ObserverSource,
    StateSource.name: StateSource,
    ConfigSource.name: ConfigSource,
    TierSource.name: TierSource,
    EventsSource.name: EventsSource,
    HistorySource.name: HistorySource,
}


def build(names=None, cfg=None):
    """Instantiate the named sources (all of them by default).

    The health source is handed the TierSource INSTANCE rather than its own copy:
    scanning the fast tier is the most expensive thing this dashboard does, and the
    headroom check wants the same numbers the Now tab already shows. Sharing the
    instance shares its cache, so the panel costs nothing extra.
    """
    cfg = cfg or {}
    chosen = names or list(SOURCES)
    out = []
    for n in chosen:
        cls = SOURCES.get(n)
        if cls is None:
            raise ValueError(f"unknown source {n!r}; have: {', '.join(sorted(SOURCES))}")
        out.append(cls(cfg.get(n) or cfg))
    # Hand the health source the live TierSource so the headroom check reads the same
    # cached scan the rest of the page does. Built after the loop because the order of
    # `chosen` is the caller's, not ours.
    tier = next((s for s in out if s.name == TierSource.name), None)
    for s in out:
        if s.name == HealthSource.name and tier is not None:
            s.cfg = dict(s.cfg, _tier_source=tier)
    return out


def collect_all(sources, force=False):
    """-> (data, errors). One failing source never costs you the others."""
    data, errors = {}, {}
    for s in sources:
        if not s.in_state:
            continue
        try:
            if not s.available():
                errors[s.name] = "unavailable"
                continue
            data[s.name] = s.get(force=force)
        except Exception as e:                                  # noqa: BLE001
            errors[s.name] = f"{type(e).__name__}: {e}"
    return data, errors
