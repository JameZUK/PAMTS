# Writing a player adapter

A player adapter is the only part of PAMTS that knows about a specific media server.
Everything else — ranking, budgets, pinning, copying, locking — is generic.

Plex is implemented in `pamts_players.py`. Adding another is one class and one line in
the `PLAYERS` registry.

## What an adapter must answer

| method | used by | must return |
|---|---|---|
| `available()` | both | `True` if configured and reachable; log why not |
| `library_items()` | tiering | every item, with last-played time — or `None` on failure |
| `now_playing()` | promotion | `[PlayEvent]` for what is playing now |
| `locality_group(event)` | promotion | `[Candidate]` likely to follow, in play order |

`simulate_recent(count)` is optional and returns `[]` by default. Implementing it makes
`--simulate-recent` work for your player, which is the easiest way to validate a new
adapter against a real library.

## The third question is the interesting one

`locality_group()` is why adapters exist rather than a single hard-coded API client. The
rule differs per medium:

| playing | promote next |
|---|---|
| a TV episode | the rest of the season, then the first items of the next season |
| a music track | the rest of the album |
| a film | **nothing** — see [DESIGN.md](DESIGN.md#films-are-not-promoted) |

Return `[]` when there is no meaningful "next". That is a correct answer, not a failure.

## Rules that matter

**Report *played*, never *read*.** Whatever your server calls it — `lastViewedAt`, a play
count, a scrobble — use the record written when a human plays something. Do **not** use
file access times or filesystem events: a library scan reads every file and is
indistinguishable from playback at that level, so a scan would promote your whole library
and flatten every ranking. This is the single most important rule here; see
[DESIGN.md](DESIGN.md#playback-is-the-only-trigger).

**`library_items()` must return `None` on failure, never `[]`.** An empty list means
"nothing has ever been played", which is a legitimate state. `None` means "I could not
find out", and callers treat that as a reason to do nothing at all. Conflating them would
make a network blip look like an empty library and evict everything.

**Map paths through `pamts.split_root()`.** Items whose path is not under a configured
root are not PAMTS's to manage — skip them silently.

**Report duration and position if you can.** `PlayEvent.remaining_s` is how PAMTS knows
how long it has to get the next item onto fast storage before the current one ends.
Leave it `0.0` if unknown and no time limit is applied.

## Skeleton

```python
class MyPlayer(Player):
    kind = "myplayer"                      # the [player] kind value in the config

    # Ceilings only. The engine scales down from these by available space and time.
    # An album is not a season: pick numbers that suit your medium.
    caps = Caps(max_items=24, max_bytes=60 * pamts.GB)

    def available(self):
        if not self.url:
            logging.error(f"[{self.kind}] no url configured")
            return False
        try:
            self._token = open(self.token_file).read().strip()
        except OSError as e:
            logging.error(f"[{self.kind}] cannot read {self.token_file}: {e}")
            return False
        return bool(self._token)

    def library_items(self):
        if not self.available():
            return None
        out = []
        try:
            for it in self._fetch_everything():          # your API calls
                m = pamts.split_root(it["file"])
                if not m:
                    continue                             # not under a configured root
                rel, fast_root, slow_root = m
                out.append({
                    "kind": "episode",                   # or "movie" / "track"
                    "show": it["series_title"],
                    "show_key": it["series_id"],         # stable series identifier
                    "season": it["season_number"],
                    "episode": it["episode_number"],
                    "title": it["title"],
                    "rel": rel,
                    "fast": os.path.join(fast_root, rel),
                    "slow": os.path.join(slow_root, rel),
                    "size": it["bytes"],
                    "last_viewed": it["last_played_epoch"] or 0,   # 0 = never played
                    "added": it["added_epoch"] or 0,
                })
        except Exception as e:
            logging.error(f"[{self.kind}] library sweep failed: {e}")
            return None                                  # None, NOT []
        return out

    def now_playing(self):
        out = []
        for s in self._fetch_sessions():
            remaining = max(0.0, (s["duration_ms"] - s["position_ms"]) / 1000.0)
            out.append(PlayEvent(
                source=self.kind, kind="episode", path=s["file"],
                remaining_s=remaining,
                label=f"{s['series_title']} S{s['season']}E{s['episode']}",
                group={"series": s["series_id"], "season": s["season"],
                       "episode": s["episode"]}))
        return out

    def locality_group(self, event):
        if event.kind != "episode" or not event.group:
            return []                                    # films: nothing to prefetch
        g = event.group
        out = []
        for ep in self._season_items(g["series"], g["season"]):
            if ep["episode_number"] <= g["episode"]:
                continue
            out.append(Candidate(path=ep["file"],
                                 label=f"S{g['season']:02d}E{ep['episode_number']:02d}",
                                 size=ep["bytes"],
                                 order=g["season"] * 1000 + ep["episode_number"]))
        # Worth carrying into the next season: finishing one and starting the next is an
        # ordinary binge, and the boundary is exactly where promotion stops helping.
        return out
```

Then register it:

```python
PLAYERS = {
    PlexPlayer.kind: PlexPlayer,
    MyPlayer.kind: MyPlayer,
}
```

and set `kind = "myplayer"` in `[player]`.

## Ordering

`Candidate.order` must be ascending in true play order, and PAMTS sorts by it. For
episodes a composite like `season * 1000 + episode` keeps a cross-season list correctly
ordered. Order matters because the caps truncate the list — the *first* few entries are
what actually get promoted, so they must be the ones most likely to be played next.

## Testing a new adapter

`tests/test_promote.py` contains a `FakePlayer` showing the minimum an adapter must
satisfy. Copy that pattern for yours — no network, no real storage.

Then against a real server, read-only:

```sh
pamts-promote.py --dry-run --simulate-recent --simulate-count 5
pamts-tier.py --dry-run --only tier
```

The first replays recently played items through your `locality_group()`. The second
exercises `library_items()` and shows the pins it derives. Both change nothing.

## Music: a note

Music is a natural fit — the locality unit is the album, and play counts are usually
tracked — but it has a wrinkle worth planning for. Music is often served by *several*
things at once (a music server, a media server, a mobile streaming app). If each consumer
resolves paths differently, a per-client union filesystem has to be configured on every
one of them, and any consumer you miss sees the split tiers.

Where that applies, putting the union on the *server* and exporting the merged view is
usually the better arrangement.
