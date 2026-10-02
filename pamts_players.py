"""PAMTS player adapters.

A player adapter is the only part of PAMTS that knows about a specific media server.
Everything else -- ranking, budgets, copying, locking -- is generic.

An adapter answers three questions:

    library_items()        every item the player knows about, with its last-played
                           time. Tiering ranks on this and derives the pins from it.
    now_playing()          what is playing right now. Promotion reacts to this.
    locality_group(event)  what is likely to be played NEXT after `event`.

The third is the interesting one, and it is why adapters exist rather than a single
hard-coded API client: the locality rule is different for every kind of media.

    a TV episode  -> the rest of the season, then the next season
    a music track -> the rest of the album
    a film        -> nothing. A film is watched once, and by the time a session is
                     visible it is already streaming. There is nothing to prefetch.

To add a player, see docs/PLAYERS.md. It is one class and one line in PLAYERS.

Why playback and not the filesystem
-----------------------------------
It is tempting to trigger on reads -- atime, inotify, or a FUSE layer. Do not. A
library scan reads every file in the library, and at that level a scanner's read is
indistinguishable from someone watching something, so a nightly scan would promote
the entire library and reset every ranking. Adapters therefore report what the player
considers *played*, which scans do not touch.
"""

import abc
import base64
import datetime
import sqlite3
import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pamts
import pamts_observer


@dataclass
class PlayEvent:
    """Something is being played right now."""
    source: str                 # which adapter produced this
    kind: str                   # "episode" | "movie" | "track"
    label: str                  # human description, for logs
    path: str                   # the file being played, as the player reports it
    group: object = None        # opaque handle the adapter uses to find the group
    remaining_s: float = 0.0    # seconds until this item finishes. This is the
                                # deadline for getting the NEXT item onto fast
                                # storage. 0 means unknown -> no time limit applied.


@dataclass
class Candidate:
    """An item likely to be played next."""
    path: str                   # as the player reports it
    label: str                  # e.g. "S01E07"
    size: int = 0               # the player's idea of size; verified on disk
    order: int = 0              # ascending play order


@dataclass
class Caps:
    """Per-adapter ceilings. A season and an album need very different numbers.

    These are CEILINGS. The promotion engine scales down from them by the space and
    the time actually available, so a nearly-empty tier can take a whole season while
    a nearly-full one takes a couple of items.
    """
    max_items: int
    max_bytes: int


class Player(abc.ABC):
    kind = "abstract"
    caps = Caps(max_items=24, max_bytes=60 * pamts.GB)

    # Not every server exposes everything. An adapter declares what it can do, and
    # the engine works with whatever is available rather than assuming.
    #
    # provides_history  can library_items() return real play history? Lyrion/LMS
    #                   cannot: its API reports no play counts at all. Such an
    #                   adapter still drives promotion, and PAMTS's own observed-play
    #                   history (see pamts.observe_plays) covers ranking over time.
    # provides_sessions can now_playing() report what is playing?
    provides_history = True
    provides_sessions = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg.get("name") or self.kind
        self.url = str(cfg.get("url", "")).rstrip("/")
        self.token_file = cfg.get("token_file")
        self._token = None

    def available(self):
        """Can this player be reached and is it configured? Log why not."""
        return True

    @abc.abstractmethod
    def library_items(self):
        """-> [item dict] or None on failure.

        Each item dict carries:
            kind        "episode" | "movie" | "track"
            show        series/album title, for logs         (may be None)
            show_key    stable series identifier             (may be None)
            season      season/disc number                   (may be None)
            episode     episode/track number                 (may be None)
            title       item title
            rel         path relative to its configured root
            fast/slow   absolute paths on each tier, via pamts.split_root
            size        bytes
            last_viewed epoch of last play, 0 if never played
            added       epoch the item was added, 0 if unknown

        Returning None means "could not determine" and callers MUST treat that as a
        reason to do nothing, never as "nothing has been played".
        """

    @abc.abstractmethod
    def now_playing(self):
        """-> [PlayEvent]"""

    @abc.abstractmethod
    def locality_group(self, event):
        """-> [Candidate] likely to follow `event`, in play order."""

    def simulate_recent(self, count=1):
        """-> [PlayEvent] for recently PLAYED items, as if they were playing.

        Validation aid only (--simulate-recent, which requires --dry-run). Lets the
        selection logic be exercised against a real library without waiting for
        someone to press play. Adapters that cannot do this return [].
        """
        return []

    def recent_plays(self, since):
        """-> [PlayEvent] for items played since `since` (epoch). Default: none.

        An alternative promotion trigger for servers whose play records are per-user
        while their session API may not be. Reading "what was played since we last
        looked" from the server's own records covers EVERY user automatically, needs no
        privileged account, and is authoritative -- it is what the server actually
        recorded, not what it happens to be streaming at the instant we ask.

        The trade-off against a live session API is precision: a play is recorded at or
        near the end of a track, so the remaining time is unknown and no time budget can
        be computed. For short items like music tracks that hardly matters, and the time
        budget has a floor, so it degrades rather than breaking.
        """
        return []

    # ---------------------------------------------------------- optional history_db
    # Some servers simply do not expose play history over their API. Where the data
    # exists in a local database, an adapter may read it -- READ ONLY -- if the
    # operator opts in by setting `history_db`.
    #
    # This is a deliberate, narrow exception to "use the API". It is not a shortcut
    # around a working API: for LMS the API reports no play data at all, and for
    # Subsonic servers the API reports only the CALLING USER's plays, so neither can
    # answer "what has anyone played" however politely you ask. The trade-offs are
    # real and the operator should know them:
    #   * it needs filesystem access to the database
    #   * it depends on a schema the upstream project may change
    #   * it is the only way to get history that predates PAMTS, or history belonging
    #     to other users
    # Opening immutable means a live server is never disturbed and no lock is taken.
    def _read_db(self, sql, params=()):
        path = self.cfg.get("history_db")
        if not path:
            return None
        if not os.path.exists(path):
            logging.error(f"[{self.name}] history_db {path} does not exist")
            return None
        try:
            con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            logging.error(f"[{self.name}] history_db read failed ({path}): {e}. The "
                          "schema may have changed upstream; history will be skipped.")
            return None


class PlexPlayer(Player):
    """Plex Media Server, over its HTTP API.

    Needs an X-Plex-Token in a file (see docs/INSTALL.md). Ranking uses lastViewedAt,
    which is a view record: a library scan does not write it.
    """
    kind = "plex"

    def available(self):
        if not self.url:
            logging.error(f"[{self.kind}] no url configured")
            return False
        try:
            self._token = open(self.token_file).read().strip()
        except OSError as e:
            logging.error(f"[{self.kind}] cannot read token file {self.token_file}: {e}")
            return False
        if not self._token:
            logging.error(f"[{self.kind}] token file {self.token_file} is empty")
            return False
        return True

    # ---------------------------------------------------------------- transport
    def _get(self, path, **params):
        params["X-Plex-Token"] = self._token
        sep = "&" if "?" in path else "?"
        u = f"{self.url}{path}{sep}{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(u, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("MediaContainer", {})

    @staticmethod
    def _part(item):
        for m in item.get("Media", []):
            for p in m.get("Part", []):
                return p.get("file"), (p.get("size") or 0)
        return None, 0

    def _sections(self, types=("show", "movie")):
        return [s for s in self._get("/library/sections").get("Directory", [])
                if s.get("type") in types]

    def _all(self, section_key, item_type, page=500):
        start, total = 0, None
        while True:
            mc = self._get(f"/library/sections/{section_key}/all", type=item_type,
                           **{"X-Plex-Container-Start": start,
                              "X-Plex-Container-Size": page})
            items = mc.get("Metadata", [])
            if total is None:
                total = mc.get("totalSize", len(items))
            for it in items:
                yield it
            start += page
            if not items or start >= total:
                return

    # ------------------------------------------------------------------- sweeps
    def library_items(self):
        if not self.available():
            return None
        out = []
        try:
            for sec in self._sections():
                is_show = sec.get("type") == "show"
                for it in self._all(sec["key"], 4 if is_show else 1):
                    f, sz = self._part(it)
                    m = pamts.split_root(f)
                    if not m:
                        continue      # not under a configured root; not ours to manage
                    rel, fast_root, slow_root = m
                    out.append({
                        "kind": "episode" if is_show else "movie",
                        "show": it.get("grandparentTitle"),
                        "show_key": it.get("grandparentRatingKey"),
                        "season": it.get("parentIndex"),
                        "episode": it.get("index"),
                        "title": it.get("title"),
                        "rel": rel,
                        "fast": os.path.join(fast_root, rel),
                        "slow": os.path.join(slow_root, rel),
                        "size": sz,
                        "last_viewed": it.get("lastViewedAt") or 0,
                        "play_count": it.get("viewCount") or 0,
                        "added": it.get("addedAt") or 0,
                    })
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            logging.error(f"[{self.kind}] library sweep failed: {e}")
            return None
        return out

    def now_playing(self):
        out = []
        for it in self._get("/status/sessions").get("Metadata", []):
            kind = it.get("type")
            f, _sz = self._part(it)
            if not f:
                continue
            # duration/viewOffset are ms. The difference is how long we have to get
            # the next item local before this one ends.
            dur, off = it.get("duration") or 0, it.get("viewOffset") or 0
            remaining = max(0.0, (dur - off) / 1000.0) if dur else 0.0
            if kind == "episode":
                out.append(PlayEvent(
                    source=self.kind, kind=kind, path=f, remaining_s=remaining,
                    label=f"{it.get('grandparentTitle')} "
                          f"S{it.get('parentIndex')}E{it.get('index')}"
                          + (f" [{remaining / 60:.0f} min left]" if remaining else ""),
                    group={"season": it.get("parentRatingKey"),
                           "show": it.get("grandparentRatingKey"),
                           "season_index": it.get("parentIndex"),
                           "index": it.get("index")}))
            elif kind == "movie":
                # Recorded for the log only; locality_group returns nothing for films.
                out.append(PlayEvent(source=self.kind, kind=kind, path=f,
                                     remaining_s=remaining,
                                     label=str(it.get("title")), group=None))
        return out

    def simulate_recent(self, count=1):
        out = []
        for sec in self._sections(types=("show",)):
            d = self._get(f"/library/sections/{sec['key']}/all", type=4,
                          sort="lastViewedAt:desc",
                          **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": count})
            for it in d.get("Metadata", []):
                f, _sz = self._part(it)
                if not f:
                    continue
                out.append(PlayEvent(
                    source=self.kind, kind="episode", path=f,
                    label=f"{it.get('grandparentTitle')} "
                          f"S{it.get('parentIndex')}E{it.get('index')} (SIMULATED)",
                    group={"season": it.get("parentRatingKey"),
                           "show": it.get("grandparentRatingKey"),
                           "season_index": it.get("parentIndex"),
                           "index": it.get("index")}))
        return out

    # ----------------------------------------------------------------- locality
    def _season_eps(self, season_key):
        eps = []
        for it in self._get(f"/library/metadata/{season_key}/children").get("Metadata", []):
            f, sz = self._part(it)
            idx = it.get("index")
            if f and idx is not None:
                eps.append((idx, f, sz))
        return sorted(eps)

    def _next_season(self, show_key, cur_season_index):
        """(key, index) of the season after this one, or None.

        Finishing a season and starting the next is an ordinary binge, and without
        this the season boundary is exactly where promotion would stop helping.
        Season 0 is specials and is never treated as "next".
        """
        if show_key is None or cur_season_index is None:
            return None
        best = None
        for it in self._get(f"/library/metadata/{show_key}/children").get("Metadata", []):
            idx = it.get("index")
            if idx is None or idx <= cur_season_index or idx == 0:
                continue
            if best is None or idx < best[1]:
                best = (it.get("ratingKey"), idx)
        return best

    def locality_group(self, event):
        if event.kind != "episode" or not event.group:
            return []       # films: nothing to prefetch. See the module docstring.
        g = event.group
        cur, si = g.get("index"), g.get("season_index")
        lbl = (lambda i: f"S{si:02d}E{i:02d}") if si is not None else (lambda i: f"E{i:02d}")
        # Composite order keeps a cross-season list in true play order.
        out = [Candidate(path=f, label=lbl(idx), size=sz, order=(si or 0) * 1000 + idx)
               for idx, f, sz in self._season_eps(g["season"])
               if cur is None or idx > cur]
        if len(out) < self.caps.max_items:
            nxt = self._next_season(g.get("show"), si)
            if nxt:
                nkey, nidx = nxt
                for idx, f, sz in self._season_eps(nkey):
                    out.append(Candidate(path=f, label=f"S{nidx:02d}E{idx:02d}",
                                         size=sz, order=nidx * 1000 + idx))
        return out


class LmsPlayer(Player):
    """Lyrion Music Server (formerly Logitech Media Server), over JSON-RPC.

    Endpoint is POST {url}/jsonrpc.js with {"method":"slim.request",
    "params":[<playerid>, [<command>...]]}. Optional HTTP basic auth.

    IMPORTANT: LMS exposes NO play history through this API. Play counts and last-played
    times live in its private persist.db, and neither `titles` nor `songinfo` reports
    them (verified against 9.x). So provides_history is False: this adapter drives
    promotion, and ranking comes from PAMTS's own observed-play history, which fills in
    as it polls. See docs/PLAYERS.md.

    Locality unit is the album, taken in track order.
    """
    kind = "lms"
    provides_history = False
    # An album, not a season: many small files rather than a few large ones.
    caps = Caps(max_items=40, max_bytes=3 * pamts.GB)

    def __init__(self, cfg):
        super().__init__(cfg)
        self._plugin = False
        self._page = 500
        # Optimistic until available() has looked: history may come from the companion
        # plugin (preferred) or, failing that, from persist.db.
        self.provides_history = True

    def available(self):
        if not self.url:
            logging.error(f"[{self.name}] no url configured")
            return False

        # Is the companion plugin installed? It publishes play history as an ordinary
        # CLI query, which is far better than reading the database from outside: no
        # filesystem access, no schema coupling, no snapshot to keep fresh. See
        # plugins/lms/ in this repo.
        self._plugin = False
        try:
            info = self._rpc("", ["pamts", "info", "?"]) or {}
            if "played" in info:
                self._plugin = True
                self._page = max(1, min(int(info.get("max_page") or 500), 5000))
                logging.info(f"[{self.name}] history plugin v{info.get('version')} "
                             f"present: {info.get('played')} played of "
                             f"{info.get('tracks')} track(s)")
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass          # absence is normal, not an error

        if not self._plugin and self.cfg.get("history_db"):
            logging.info(f"[{self.name}] no history plugin; falling back to history_db")
        self.provides_history = bool(self._plugin or self.cfg.get("history_db"))
        if not self.provides_history:
            logging.info(f"[{self.name}] no play history available (install the "
                         "companion plugin, or set history_db) - promotion still works")
        return True

    def _item(self, path, last_played, size=0, play_count=0):
        m = pamts.split_root(path)
        if not m:
            return None
        rel, fast_root, slow_root = m
        return {"kind": "track", "show": None, "show_key": None,
                "season": None, "episode": None,
                "title": os.path.basename(path), "rel": rel,
                "fast": os.path.join(fast_root, rel),
                "slow": os.path.join(slow_root, rel),
                "size": int(size or 0),
                "last_viewed": int(last_played or 0),
                "play_count": int(play_count or 0), "added": 0}

    def _items_from_plugin(self):
        """Play history from the companion plugin, paged."""
        out, offset = [], 0
        while True:
            try:
                r = self._rpc("", ["pamts", "history", str(offset), str(self._page)])
            except (urllib.error.URLError, OSError, ValueError) as e:
                logging.error(f"[{self.name}] history query failed at offset "
                              f"{offset}: {e}")
                return None
            rows = r.get("history_loop") or []
            for h in rows:
                path = self._path_from_url(h.get("url"))
                if not path:
                    continue          # a stream or podcast, not a library file
                it = self._item(path, h.get("lastplayed"), h.get("filesize"),
                                h.get("playcount"))
                if it:
                    out.append(it)
            if len(rows) < self._page:
                break
            offset += self._page
        logging.info(f"[{self.name}] {len(out)} played track(s) from the plugin")
        return out

    def _rpc(self, player_id, command):
        body = json.dumps({"id": 1, "method": "slim.request",
                           "params": [player_id or "", command]}).encode()
        req = urllib.request.Request(f"{self.url}/jsonrpc.js", data=body,
                                     headers={"Content-Type": "application/json"})
        user, pw = self.cfg.get("username"), self.cfg.get("password")
        if user:
            token = base64.b64encode(f"{user}:{pw or ''}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("result", {})

    @staticmethod
    def _path_from_url(u):
        """LMS reports local files as percent-encoded file:// URLs."""
        if not u or not u.startswith("file://"):
            return None                      # remote stream (radio, podcast): not ours
        return urllib.parse.unquote(u[7:])

    def library_items(self):
        """Play history, from the plugin if installed, else persist.db.

        LMS keeps play data in `tracks_persistent`, which deliberately survives library
        rescans -- exactly the property PAMTS wants. With neither source this returns an
        empty list: a definite "no history to give", not a failure (None).
        """
        if self._plugin:
            return self._items_from_plugin()
        if not self.cfg.get("history_db"):
            return []
        rows = self._read_db(
            "SELECT url, lastPlayed, playCount FROM tracks_persistent "
            "WHERE lastPlayed IS NOT NULL AND lastPlayed > 0")
        if rows is None:
            return None
        out = []
        for url, last_played, plays in rows:
            path = self._path_from_url(url)
            if not path:
                continue
            m = pamts.split_root(path)
            if not m:
                continue
            rel, fast_root, slow_root = m
            out.append({
                "kind": "track", "show": None, "show_key": None,
                "season": None, "episode": None,
                "title": os.path.basename(path), "rel": rel,
                "fast": os.path.join(fast_root, rel),
                "slow": os.path.join(slow_root, rel),
                "size": 0,
                "last_viewed": int(last_played or 0),
                "play_count": int(plays or 0),
                "added": 0,
            })
        logging.info(f"[{self.name}] {len(out)} played track(s) from persist.db")
        return out

    def now_playing(self):
        out = []
        try:
            players = self._rpc("", ["players", "0", "50"]).get("players_loop", [])
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] cannot list players: {e}")
            return []
        for pl in players:
            pid = pl.get("playerid")
            if not pid or not pl.get("connected"):
                continue
            try:
                st = self._rpc(pid, ["status", "-", "1", "tags:u"])
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if st.get("mode") != "play":
                continue
            tracks = st.get("playlist_loop") or []
            if not tracks:
                continue
            t = tracks[0]
            path = self._path_from_url(t.get("url"))
            if not path:
                continue
            dur = float(t.get("duration") or st.get("duration") or 0)
            elapsed = float(st.get("time") or 0)
            remaining = max(0.0, dur - elapsed) if dur else 0.0
            out.append(PlayEvent(
                source=self.name, kind="track", path=path, remaining_s=remaining,
                label=f"{pl.get('name')}: {t.get('title')}"
                      + (f" [{remaining / 60:.0f} min left]" if remaining else ""),
                group={"track_id": t.get("id")}))
        return out

    def recent_plays(self, since):
        """Tracks played since `since`.

        Complements now_playing(): a play is recorded even if PAMTS was not polling at
        the moment it happened, so nothing is missed between runs. The plugin supports
        this natively via its `since:` parameter, so only the new rows cross the wire.
        """
        if self._plugin:
            try:
                r = self._rpc("", ["pamts", "history", "0", str(self._page),
                                   f"since:{int(since)}"])
            except (urllib.error.URLError, OSError, ValueError) as e:
                logging.error(f"[{self.name}] recent-play query failed: {e}")
                return []
            out = []
            for h in r.get("history_loop") or []:
                path = self._path_from_url(h.get("url"))
                if not path or not pamts.split_root(path):
                    continue
                out.append(PlayEvent(
                    source=self.name, kind="track", path=path, remaining_s=0.0,
                    label=f"{os.path.basename(path)} (played "
                          f"{int(h.get('lastplayed') or 0)})",
                    group={"url": h.get("url")}))
            return out
        if not self.cfg.get("history_db"):
            return []
        rows = self._read_db(
            "SELECT url, lastPlayed FROM tracks_persistent "
            "WHERE lastPlayed IS NOT NULL AND lastPlayed > ? ORDER BY lastPlayed",
            (int(since),))
        if not rows:
            return []
        out = []
        for url, played in rows:
            path = self._path_from_url(url)
            if not path or not pamts.split_root(path):
                continue
            # persist.db has no album id; locality is resolved from the path's album via
            # the API, so carry the URL and let locality_group look it up by track id.
            out.append(PlayEvent(
                source=self.name, kind="track", path=path, remaining_s=0.0,
                label=f"{os.path.basename(path)} (played {int(played)})",
                group={"url": url}))
        return out

    def locality_group(self, event):
        """The rest of the album, in track order."""
        if event.kind != "track" or not event.group:
            return []
        tid = event.group.get("track_id")
        if tid is None:
            # recent_plays events carry a URL rather than a track id; resolve it.
            url = event.group.get("url")
            if not url:
                return []
            try:
                r = self._rpc("", ["titles", "0", "1", f"search:{os.path.basename(url)}",
                                   "tags:u"])
                for t in r.get("titles_loop", []):
                    if t.get("url") == url:
                        tid = t.get("id")
                        break
            except (urllib.error.URLError, OSError, ValueError):
                return []
            if tid is None:
                return []
        try:
            # status' playlist_loop does not carry album_id, so resolve it per track.
            info = self._rpc("", ["songinfo", "0", "50", f"track_id:{tid}", "tags:e"])
            album_id = None
            for e in info.get("songinfo_loop", []):
                album_id = e.get("album_id", album_id)
            if album_id is None:
                return []           # a single/remote track with no album: no locality
            tracks = self._rpc("", ["titles", "0", "500", f"album_id:{album_id}",
                                    "tags:uf", "sort:tracknum"]).get("titles_loop", [])
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] cannot resolve album: {e}")
            return []

        def num(t):
            try:
                return int(t.get("tracknum") or 0)
            except (TypeError, ValueError):
                return 0

        # Find where we are by matching the track ID within the album listing. songinfo
        # does not reliably report a track number (the documented tag for `titles` is
        # not honoured there), and matching on the id uses only fields both calls agree
        # on. If the id is not found, fall back to promoting the whole album rather than
        # nothing -- being slightly wasteful beats doing nothing useful.
        cur_n = 0
        for t in tracks:
            if str(t.get("id")) == str(tid):
                cur_n = num(t)
                break
        else:
            logging.info(f"[{self.name}] current track not found in its album listing; "
                         "offering the whole album")

        out = []
        for t in tracks:
            path = self._path_from_url(t.get("url"))
            if not path:
                continue
            n = num(t)
            if n <= cur_n:
                continue
            try:
                size = int(t.get("filesize") or 0)
            except (TypeError, ValueError):
                size = 0
            out.append(Candidate(path=path, label=f"track {n:02d}", size=size, order=n))
        return out


class NavidromePlayer(Player):
    """Navidrome, over the Subsonic API.

Verified against Navidrome 0.64.0.

    Configuration needs `username`, and `token_file` holding that user's PASSWORD
    (Subsonic derives a per-request token from it; the password is never sent).

    **Use the account that actually listens.** Subsonic play annotations -- `played`
    and `playCount` -- are PER USER. A freshly created service account reports no play
    history at all, however much the library has been listened to: verified against
    0.64.0, where a new user saw 0 of 5000 songs with a `played` timestamp and
    getAlbumList2 type=frequent/recent returned nothing. So a dedicated read-only user
    is exactly the wrong choice here.

    If several people listen under separate accounts, configure this adapter once per
    account with distinct `name` values. History is merged across players by maximum,
    so everyone's listening counts.

    Subsonic reports song `path` RELATIVE to the music folder, so `music_folder` must
    be set to the absolute path the server serves from -- otherwise PAMTS cannot map a
    song onto your tiers.

    Locality unit is the album, taken in track order.
    """
    kind = "navidrome"
    provides_history = True
    caps = Caps(max_items=40, max_bytes=3 * pamts.GB)

    API_VERSION = "1.16.1"

    def __init__(self, cfg):
        super().__init__(cfg)
        # With history_db set, history comes from the database and covers EVERY user.
        # Without it, the API is used and covers only the calling user.
        # History covering EVERY user comes from either the sidecar or a local
        # database read; the Subsonic API can only ever report the calling account.
        self.history_url = str(cfg.get("history_url") or "").rstrip("/")
        # A sidecar that HANGS rather than refusing would otherwise stall the nightly
        # sweep for a minute per page. Bounded, and configurable for slow links.
        self.history_timeout = float(cfg.get("history_timeout") or 30)
        self.all_users = bool(cfg.get("history_db") or self.history_url)

    def available(self):
        if not self.url:
            logging.error(f"[{self.name}] no url configured")
            return False
        if not self.cfg.get("username"):
            if self.history_url:
                # History-only mode. Sessions and locality need Subsonic, so they are
                # switched off rather than failing on every call; the access observer
                # already supplies now-playing for music.
                self.provides_sessions = False
                logging.info(f"[{self.name}] history-only via {self.history_url} "
                             "(no credentials configured, so no now-playing)")
                return True
            logging.error(f"[{self.name}] 'username' is required")
            return False
        if not self.cfg.get("music_folder"):
            logging.error(f"[{self.name}] 'music_folder' is required: Subsonic reports "
                          "song paths relative to it, so PAMTS cannot map them without it")
            return False
        try:
            self._token = open(self.token_file).read().strip()
        except OSError as e:
            logging.error(f"[{self.name}] cannot read {self.token_file}: {e}")
            return False
        return bool(self._token)

    def _get(self, endpoint, **params):
        """Subsonic token auth: send salt + md5(password + salt), never the password."""
        salt = secrets.token_hex(8)
        params.update({
            "u": self.cfg["username"],
            "t": hashlib.md5((self._token + salt).encode()).hexdigest(),
            "s": salt, "v": self.API_VERSION, "c": "pamts", "f": "json",
        })
        u = f"{self.url}/rest/{endpoint}?{urllib.parse.urlencode(params, doseq=True)}"
        with urllib.request.urlopen(urllib.request.Request(u), timeout=60) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        resp = body.get("subsonic-response", {})
        if resp.get("status") != "ok":
            err = resp.get("error", {})
            raise OSError(f"subsonic error {err.get('code')}: {err.get('message')}")
        return resp

    @staticmethod
    def _epoch(iso):
        """Subsonic timestamps are ISO8601; 0 if absent or unparseable."""
        if not iso:
            return 0
        try:
            return int(datetime.datetime.fromisoformat(
                str(iso).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return 0

    def _song_item(self, s):
        rel_to_folder = s.get("path")
        if not rel_to_folder:
            return None
        full = os.path.join(str(self.cfg["music_folder"]).rstrip("/"), rel_to_folder)
        m = pamts.split_root(full)
        if not m:
            return None
        rel, fast_root, slow_root = m
        return {
            "kind": "track",
            "show": s.get("album"),
            "show_key": s.get("albumId"),
            "season": s.get("discNumber") or 1,
            "episode": s.get("track") or 0,
            "title": s.get("title"),
            "rel": rel,
            "fast": os.path.join(fast_root, rel),
            "slow": os.path.join(slow_root, rel),
            "size": int(s.get("size") or 0),
            "last_viewed": self._epoch(s.get("played")),
            "play_count": int(s.get("playCount") or 0),
            "added": self._epoch(s.get("created")),
            "_path": full,
        }

    def library_items(self):
        if self.history_url:
            return self._items_from_sidecar()
        if self.cfg.get("history_db"):
            return self._items_from_db()
        if not self.available():
            return None
        out, offset, page = [], 0, 500
        try:
            while True:
                r = self._get("search3", query="", songCount=page, songOffset=offset,
                              artistCount=0, albumCount=0)
                songs = (r.get("searchResult3") or {}).get("song") or []
                for s in songs:
                    it = self._song_item(s)
                    if it:
                        out.append(it)
                if len(songs) < page:
                    break
                offset += page
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            logging.error(f"[{self.name}] library sweep failed: {e}")
            return None
        # Annotations are per user. Items but no plays at all almost always means we are
        # authenticated as the wrong account, not that nothing was ever played -- and
        # silently ranking on an empty history would be worse than saying so.
        if out and not any(i["last_viewed"] for i in out):
            logging.warning(
                f"[{self.name}] {len(out)} item(s) but NONE has a play timestamp. "
                f"Subsonic annotations are per-user: check that '{self.cfg.get('username')}' "
                "is the account that actually listens, not a fresh service account.")
        return out

    def _items_from_sidecar(self):
        """Play history for EVERY user, from the read-only sidecar.

        Same answer as _items_from_db, fetched over HTTP instead. The sidecar runs
        where navidrome.db actually is, which is the point: PAMTS need not reach across
        hosts, need not hold any listener's password, and need not know Navidrome's
        schema. See scripts/mediastream/navidrome-history in the homelab repo.
        """
        out, index, page = [], 0, 5000
        while True:
            url = f"{self.history_url}/history?index={index}&quantity={page}"
            try:
                with urllib.request.urlopen(url, timeout=self.history_timeout) as r:
                    doc = json.loads(r.read().decode())
            except (urllib.error.URLError, OSError, ValueError) as e:
                logging.error(f"[{self.name}] history sidecar failed at index "
                              f"{index}: {e}")
                return None
            rows = doc.get("history") or []
            for h in rows:
                full = h.get("path")
                if not full:
                    continue
                m = pamts.split_root(full)
                if not m:
                    continue      # not under a configured root; not ours to manage
                rel, fast_root, slow_root = m
                out.append({
                    "kind": "track", "show": None, "show_key": h.get("album_id"),
                    "season": h.get("disc") or 1, "episode": h.get("track") or 0,
                    "title": h.get("title"), "rel": rel,
                    "fast": os.path.join(fast_root, rel),
                    "slow": os.path.join(slow_root, rel),
                    "size": int(h.get("size") or 0),
                    "last_viewed": int(h.get("last_played") or 0),
                    "play_count": int(h.get("play_count") or 0),
                    "added": 0,
                })
            if len(rows) < page:
                break
            index += page
        logging.info(f"[{self.name}] {len(out)} played track(s) from the sidecar "
                     "(all users)")
        return out

    def _items_from_db(self):
        """Play history for EVERY user, from navidrome.db.

        The API cannot do this. Subsonic `played`/`playCount` are per-user annotations,
        so an API sweep reports only the calling account -- which means a household
        where several people listen under separate logins would look almost unplayed,
        and PAMTS would evict music that had been listened to. The `annotation` table
        holds one row per (user, item); taking MAX(play_date) per file is the answer to
        "has anyone played this".
        """
        folder = str(self.cfg.get("music_folder") or "").rstrip("/")
        if not folder:
            logging.error(f"[{self.name}] history_db needs 'music_folder' too: the "
                          "database stores paths relative to it")
            return None
        # play_count is SUMMED across users and play_date MAXed: two people each
        # playing an album ten times is twenty plays of demand for it. (Across
        # different SERVICES, sweep() merges by maximum instead, because there the same
        # single listen is reported twice.)
        rows = self._read_db(
            "SELECT mf.path, mf.size, mf.album_id, mf.track_number, mf.disc_number, "
            "       mf.title, MAX(a.play_date), SUM(a.play_count) "
            "FROM annotation a JOIN media_file mf ON mf.id = a.item_id "
            "WHERE a.item_type = 'media_file' AND a.play_date IS NOT NULL "
            "GROUP BY mf.id")
        if rows is None:
            return None
        out = []
        for path, size, album_id, track, disc, title, played, plays in rows:
            if not path:
                continue
            full = path if path.startswith("/") else os.path.join(folder, path)
            m = pamts.split_root(full)
            if not m:
                continue
            rel, fast_root, slow_root = m
            out.append({
                "kind": "track", "show": None, "show_key": album_id,
                "season": disc or 1, "episode": track or 0, "title": title,
                "rel": rel,
                "fast": os.path.join(fast_root, rel),
                "slow": os.path.join(slow_root, rel),
                "size": int(size or 0),
                "last_viewed": self._epoch(played),
                "play_count": int(plays or 0),
                "added": 0,
            })
        logging.info(f"[{self.name}] {len(out)} played file(s) from navidrome.db "
                     "(all users)")
        return out

    def now_playing(self):
        try:
            r = self._get("getNowPlaying")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] getNowPlaying failed: {e}")
            return []
        out = []
        # Unlike the per-user annotations, getNowPlaying is NOT user-scoped: the
        # Subsonic API returns every user's active session, each carrying a `username`.
        # So a single polling account sees everyone's plays, and PAMTS's observed-play
        # history accumulates them all.
        for s in (r.get("nowPlaying") or {}).get("entry") or []:
            it = self._song_item(s)
            if not it:
                continue
            who = s.get("username")
            if who:
                logging.debug(f"[{self.name}] session belongs to {who}")
            # getNowPlaying reports how long ago the play STARTED, in whole minutes --
            # coarse, so the remaining time is an estimate. It only feeds the time
            # budget, which has a floor, so a rough number is acceptable.
            dur = float(s.get("duration") or 0)
            elapsed = float(s.get("minutesAgo") or 0) * 60.0
            remaining = max(0.0, dur - elapsed) if dur else 0.0
            out.append(PlayEvent(
                source=self.name, kind="track", path=it["_path"],
                remaining_s=remaining,
                label=f"{s.get('artist')} - {s.get('title')}"
                      + (f" [{remaining / 60:.0f} min left]" if remaining else ""),
                group={"album_id": s.get("albumId"), "track": s.get("track") or 0}))
        return out

    def recent_plays(self, since):
        """Plays by ANY user since `since`, from the annotation table.

        This is the multi-user answer. Subsonic's per-user annotations cannot be read
        across accounts through the API, and whether a session API exposes other users'
        sessions is server- and role-dependent. The annotation table has one row per
        (user, item) and is written on every play, so asking it "what changed since we
        last looked" covers everybody with no special privileges.
        """
        if not self.cfg.get("history_db"):
            return []
        folder = str(self.cfg.get("music_folder") or "").rstrip("/")
        if not folder:
            return []
        iso = datetime.datetime.fromtimestamp(
            since, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._read_db(
            "SELECT mf.path, mf.size, mf.album_id, mf.track_number, mf.title, "
            "       MAX(a.play_date) "
            "FROM annotation a JOIN media_file mf ON mf.id = a.item_id "
            "WHERE a.item_type = 'media_file' AND a.play_date IS NOT NULL "
            "  AND a.play_date > ? "
            "GROUP BY mf.id ORDER BY MAX(a.play_date)", (iso,))
        if not rows:
            return []
        out = []
        for path, _size, album_id, track, title, played in rows:
            full = path if str(path).startswith("/") else os.path.join(folder, path)
            if not pamts.split_root(full):
                continue
            out.append(PlayEvent(
                source=self.name, kind="track", path=full, remaining_s=0.0,
                label=f"{title} (played {played})",
                group={"album_id": album_id, "track": track or 0}))
        return out

    def locality_group(self, event):
        """The rest of the album, in track order."""
        if event.kind != "track" or not event.group:
            return []
        album_id = event.group.get("album_id")
        if not album_id:
            return []
        try:
            r = self._get("getAlbum", id=album_id)
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] getAlbum failed: {e}")
            return []
        cur = int(event.group.get("track") or 0)
        out = []
        for s in (r.get("album") or {}).get("song") or []:
            it = self._song_item(s)
            if not it:
                continue
            n = int(s.get("track") or 0)
            if n <= cur:
                continue
            out.append(Candidate(path=it["_path"], label=f"track {n:02d}",
                                 size=it["size"], order=n))
        return out


# The registry. Adding a player means adding one line here -- see docs/PLAYERS.md.

class ObserverPlayer(Player):
    """The access observer (observer/pamts-observerd.py), over its HTTP API.

    This adapter is different in kind from the others: it does not talk to a media
    server at all. It reads what the STORAGE saw. That makes it the only adapter
    that works for a service with no usable API -- Navidrome's history is per-user
    and needs every user's credentials, which is impractical, while the filesystem
    sees every user's listening equally.

    Consequences worth knowing:

    - It reports FILE-LEVEL DEMAND, not per-user history. Exports using all_squash
      erase user identity at the server. Right signal for tiering, no use for
      anything user-facing.
    - It only knows files that have been READ. It cannot enumerate a library, so
      library_items() returns a partial view. That is safe here, because eviction
      refuses to act without play data rather than treating absence as "never
      played", and sweep() merges by maximum across adapters.
    - Co-tenanted services are indistinguishable: if two music servers share a
      host, the NFS mount is per-host. Harmless for tiering -- a play is a play.

    Its real contribution beyond history is locality: the next items are the
    SIBLING FILES IN THE SAME DIRECTORY, which works for a TV season and an album
    alike without knowing anything about either.
    """
    kind = "observer"
    provides_history = True
    provides_sessions = True
    # Deliberately conservative: without a server telling us what a "season" or an
    # "album" is, locality is directory order, which can be a very large directory.
    caps = Caps(max_items=24, max_bytes=40 * pamts.GB)

    # Shared with the observer daemon so both agree on what is tierable.
    MEDIA_EXT = pamts_observer.MEDIA_EXT

    def __init__(self, cfg):
        super().__init__(cfg)
        self.url = str(cfg.get("url", "http://127.0.0.1:8621")).rstrip("/")
        self.timeout = float(cfg.get("timeout", 10))
        # Only these labels count as demand. PROBE and COPY must never promote --
        # that is the founding constraint: a scan must not move data.
        self.demand = set(cfg.get("demand_labels", ("PLAY", "FETCH")))

    # -- transport ---------------------------------------------------------
    def _get(self, path):
        req = urllib.request.Request(self.url + path,
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def available(self):
        if not self.url:
            logging.error(f"[{self.name}] no url configured")
            return False
        try:
            self._get("/health")
            return True
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] observer unreachable at {self.url}: {e}")
            return False

    # -- path helpers ------------------------------------------------------
    def _item(self, path, last_viewed, kind_hint=None):
        m = pamts.split_root(path)
        if not m:
            return None
        rel, fast_root, slow_root = m
        parent = os.path.dirname(rel)
        return {
            "kind": kind_hint or self._kind_for(path),
            # The observer has no notion of a series, so the containing directory
            # stands in for one. It is the right granularity for both a season
            # folder and an album folder.
            "show": os.path.basename(parent) or None,
            "show_key": parent or None,
            "season": None, "episode": None,
            "title": os.path.basename(path), "rel": rel,
            "fast": os.path.join(fast_root, rel),
            "slow": os.path.join(slow_root, rel),
            "size": 0,
            "last_viewed": int(last_viewed or 0),
            "added": 0,
        }

    AUDIO_EXT = (".flac", ".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".wav",
                 ".wma", ".aac", ".alac", ".ape", ".dsf", ".dff", ".aiff", ".aif")

    def _kind_for(self, path):
        return "track" if path.lower().endswith(self.AUDIO_EXT) else "episode"

    def _siblings(self, path):
        """Names in this file's directory, ACROSS BOTH TIERS, sorted.

        Both tiers must be listed: the whole point of tiering is that the next
        episode is probably on the slow one, so a single-tier listing would
        usually fail to find exactly the item we want to promote.
        """
        m = pamts.split_root(path)
        if not m:
            return []
        rel, fast_root, slow_root = m
        parent = os.path.dirname(rel)
        names = set()
        for root in (fast_root, slow_root):
            d = os.path.join(root, parent)
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_file(follow_symlinks=False) and \
                                e.name.lower().endswith(self.MEDIA_EXT):
                            names.add(e.name)
            except OSError:
                continue
        return sorted(names)

    # -- Player interface --------------------------------------------------
    def library_items(self):
        since = float(self.cfg.get("history_since", 0))
        try:
            data = self._get(f"/history?since={since:.0f}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] history unavailable: {e}")
            return None                  # None means "do not know", never "none"
        out = []
        for row in data.get("history", []):
            if row.get("label") not in self.demand:
                continue
            it = self._item(row.get("path") or "", row.get("last_play"))
            if it:
                out.append(it)
        logging.info(f"[{self.name}] {len(out)} item(s) with observed plays")
        return out

    def now_playing(self):
        try:
            data = self._get("/sessions")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] sessions unavailable: {e}")
            return []
        out = []
        for s in data.get("sessions", []):
            if s.get("label") != "PLAY":
                continue
            path = s.get("path")
            if not path or not pamts.split_root(path):
                continue
            out.append(PlayEvent(
                source=self.name, kind=self._kind_for(path),
                label=f"{os.path.basename(path)} ({s.get('client') or 'unknown'})",
                path=path, group=os.path.dirname(path),
                remaining_s=self._remaining(path, s)))
        return out

    def _remaining(self, path, session):
        """Estimate seconds left, from bytes still unread over observed read rate.

        The observer has no idea how long a file PLAYS for -- only how fast it is
        being read. For paced playback the read rate tracks the bitrate closely
        enough to schedule against, and 0 (unknown) is returned whenever it does
        not, so the promotion engine simply applies no deadline.
        """
        rate = session.get("rate") or 0
        read = session.get("bytes") or 0
        if rate <= 0:
            return 0.0
        size = 0
        m = pamts.split_root(path)
        if m:
            rel, fast_root, slow_root = m
            for root in (fast_root, slow_root):
                try:
                    size = os.path.getsize(os.path.join(root, rel))
                    break
                except OSError:
                    continue
        if size <= read:
            return 0.0
        return max(0.0, (size - read) / rate)

    def locality_group(self, event):
        names = self._siblings(event.path)
        if not names:
            return []
        cur = os.path.basename(event.path)
        try:
            i = names.index(cur)
        except ValueError:
            return []
        out = []
        for order, name in enumerate(names[i + 1:]):
            out.append(Candidate(path=os.path.join(os.path.dirname(event.path), name),
                                 label=name, size=0, order=order))
        return out

    def recent_plays(self, since):
        """Recently played items, straight from the observer's own history."""
        try:
            data = self._get(f"/history?since={float(since):.0f}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] history unavailable: {e}")
            return []
        out = []
        for row in data.get("history", []):
            if row.get("label") not in self.demand:
                continue
            path = row.get("path")
            if not path or not pamts.split_root(path):
                continue
            out.append(PlayEvent(source=self.name, kind=self._kind_for(path),
                                 label=os.path.basename(path), path=path,
                                 group=os.path.dirname(path), remaining_s=0.0))
        return out


ADAPTERS = {
    PlexPlayer.kind: PlexPlayer,
    LmsPlayer.kind: LmsPlayer,
    NavidromePlayer.kind: NavidromePlayer,
    ObserverPlayer.kind: ObserverPlayer,
}


def build(cfg):
    """Instantiate one configured player adapter."""
    kind = str(cfg.get("kind", "")).lower()
    cls = ADAPTERS.get(kind)
    if cls is None:
        raise pamts.ConfigError(
            f"unknown player kind {kind!r}; available: {', '.join(sorted(ADAPTERS))}")
    return cls(cfg)


def build_all(cfg_list):
    """Instantiate every configured adapter, in order."""
    return [build(c) for c in cfg_list]


def sweep(players):
    """Sweep every adapter that can supply history. -> (items, ok).

    `items` is the merged library, DE-DUPLICATED by fast-tier path, with last_viewed
    taken as the MAXIMUM across adapters. `ok` is True if at least one adapter that
    claims to provide history actually did.

    Merging by maximum is the point. A music library is commonly served by two or three
    things at once, and an album played in one app looks untouched to the others. Ranking
    on a single app's view would evict content that *was* played and simply was not seen
    there.

    De-duplicating by fast path matters too: different servers identify the same album
    by different ids, so without it the same album would be pinned twice under two
    different series keys and consume the pin budget twice.
    """
    merged, ok = {}, False
    for p in players:
        # available() first: an adapter may only discover what it can do by asking the
        # server (e.g. whether a history plugin is installed), so its capability flags
        # are not reliable until then.
        if not p.available():
            continue
        if not p.provides_history:
            logging.info(f"[{p.name}] provides no play history - ranking will rely on "
                         "observed plays")
            continue
        items = p.library_items()
        if items is None:
            logging.warning(f"[{p.name}] history unavailable this run")
            continue
        ok = True
        played = 0
        for it in items:
            key = it["fast"]
            prev = merged.get(key)
            if prev is None:
                merged[key] = dict(it)
            else:
                if (it.get("last_viewed") or 0) > (prev.get("last_viewed") or 0):
                    prev["last_viewed"] = it["last_viewed"]
                # Play COUNT merges by maximum for the same reason recency does: an
                # album played fifty times in one app and never opened in another is
                # not an album played zero times. Summing instead would double-count
                # the same listen when two servers both saw it.
                if (it.get("play_count") or 0) > (prev.get("play_count") or 0):
                    prev["play_count"] = it["play_count"]
            if it.get("last_viewed"):
                played += 1
        logging.info(f"[{p.name}] {len(items)} item(s), {played} played")
    return list(merged.values()), ok
