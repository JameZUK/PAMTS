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
import json
import logging
import os
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

    def __init__(self, cfg):
        self.cfg = cfg
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


# The registry. Adding a player means adding one line here -- see docs/PLAYERS.md.
PLAYERS = {
    PlexPlayer.kind: PlexPlayer,
}


def build(cfg):
    """Instantiate the configured player adapter."""
    kind = str(cfg.get("kind", "")).lower()
    cls = PLAYERS.get(kind)
    if cls is None:
        raise pamts.ConfigError(
            f"unknown [player] kind {kind!r}; available: {', '.join(sorted(PLAYERS))}")
    return cls(cfg)
