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

    def available(self):
        if not self.url:
            logging.error(f"[{self.name}] no url configured")
            return False
        return True

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
        # Deliberately empty rather than None: None means "could not determine", and
        # this is a definite "this adapter has no history to give".
        return []

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

    def locality_group(self, event):
        """The rest of the album, in track order."""
        if event.kind != "track" or not event.group:
            return []
        tid = event.group.get("track_id")
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

    def available(self):
        if not self.url:
            logging.error(f"[{self.name}] no url configured")
            return False
        if not self.cfg.get("username"):
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
            "added": self._epoch(s.get("created")),
            "_path": full,
        }

    def library_items(self):
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

    def now_playing(self):
        try:
            r = self._get("getNowPlaying")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.error(f"[{self.name}] getNowPlaying failed: {e}")
            return []
        out = []
        for s in (r.get("nowPlaying") or {}).get("entry") or []:
            it = self._song_item(s)
            if not it:
                continue
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
ADAPTERS = {
    PlexPlayer.kind: PlexPlayer,
    LmsPlayer.kind: LmsPlayer,
    NavidromePlayer.kind: NavidromePlayer,
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
        if not p.provides_history:
            logging.info(f"[{p.name}] provides no play history via its API - ranking "
                         "will rely on observed plays")
            continue
        if not p.available():
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
            elif (it.get("last_viewed") or 0) > (prev.get("last_viewed") or 0):
                prev["last_viewed"] = it["last_viewed"]
            if it.get("last_viewed"):
                played += 1
        logging.info(f"[{p.name}] {len(items)} item(s), {played} played")
    return list(merged.values()), ok
