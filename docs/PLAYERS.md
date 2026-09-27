# Writing a player adapter

A player adapter is the only part of PAMTS that knows about a specific media server.
Everything else — ranking, budgets, pinning, copying, locking — is generic.

Adding one is one class and one line in the `ADAPTERS` registry.

## What ships

| adapter | play history | now playing | locality unit | notes |
|---|---|---|---|---|
| `plex` | ✅ `lastViewedAt` | ✅ | season, crossing into the next | verified |
| `lms` | ❌ **not exposed by its API** | ✅ | album | verified against 9.x |
| `navidrome` | ✅ `played` — **per user** | ✅ | album | verified against 0.64.0 |

Two of those need explaining, because both caught me out.

### Getting history out of a music server

Neither music server's API can answer "what has anyone played", for different reasons.
There are three routes, and the useful answer is a combination of two:

| route | covers | cost |
|---|---|---|
| observed plays (built in) | everyone, **going forward** | nothing — it is already on |
| `history_db` | everyone, **including the past** | one read-only SQL query |
| a server plugin | everyone, going forward | a build artifact; does what route 1 already does |

A plugin is the most work for the least gain: it only helps from the moment you install
it, which polling already covers. The gap polling *cannot* fill is history that already
exists — and that is what `history_db` is for.

**`history_db` is a deliberate, narrow exception to "use the API".** It is not a shortcut
around a working API; it exists because these APIs genuinely cannot answer the question:

- LMS reports **no** play data at all (below)
- Subsonic `played`/`playCount` are **per-user**, so an API sweep sees only the calling
  account — a household listening under separate logins would look almost unplayed, and
  PAMTS would evict music people had listened to

The trade-offs are real, and PAMTS states them rather than hiding them: it needs
filesystem access to the database, it depends on a schema the upstream project may
change, and it is read-only (opened `immutable`, so a live server is never disturbed and
no lock is taken). If the schema changes, history is skipped with a clear error — never
silently wrong.

Verified against real installations: **35,034** played tracks from an LMS `persist.db`
(34,613 with mappable `file://` paths; the rest are streams and podcasts), and **1,425**
from a `navidrome.db` aggregated across all users.

```toml
[[players]]
kind = "lms"
url = "http://music.example.lan:9000"
history_db = "/var/lib/squeezeboxserver/prefs/persist.db"   # optional

[[players]]
kind = "navidrome"
url = "http://music.example.lan:4533"
username = "someone"
token_file = "/etc/pamts/navidrome-password"
music_folder = "/srv/fast/music"
history_db = "/path/to/navidrome.db"                        # optional, ALL users
```

With `history_db` set, LMS gains `provides_history`, and Navidrome's history covers every
user instead of just the caller.

### LMS exposes no play history at all

Lyrion/Logitech Media Server keeps play counts and last-played times in a private
`persist.db`, and **neither `titles` nor `songinfo` reports them** — verified against 9.x
with the full documented tag set. There is no plugin CLI query for it either.

So `LmsPlayer.provides_history = False` by default. It drives promotion perfectly well
(it reports what is playing, with duration and elapsed time), and ranking comes from
PAMTS's own **observed play history** — which starts empty and fills in as PAMTS polls, so
an LMS-only setup has no ranking data on day one. PAMTS says so rather than evicting
blindly. Set `history_db` to its `persist.db` to get the existing history immediately;
that table deliberately survives library rescans, which is exactly the property PAMTS
wants.

`LmsPlayer.library_items()` returns `[]`, not `None`, precisely because this is a
definite "I have no history to give" rather than a failure.

One wrinkle worth knowing if you modify it: LMS's `status` response does not carry
`album_id`, and `songinfo` does not reliably report a track number (the tag documented
for `titles` is not honoured there). The adapter therefore finds the current position by
matching the **track id** inside the album listing, using only fields both calls agree on.
An earlier version trusted a tag that came back empty, silently defaulted the position to
0, and offered the whole album including the track already playing.

### Navidrome annotations are per-user

Subsonic `played` / `playCount` are **per user**. A freshly created service account
reports no history however much the library has been listened to — verified: a new user
saw **0 of 5000** songs with a `played` timestamp, and `getAlbumList2` with
`type=frequent` and `type=recent` both returned nothing.

Without `history_db`, point the adapter at **the account that actually listens** — a
dedicated read-only user is the wrong instinct. The adapter warns if it sees items but no
plays at all, naming this as the likely cause.

With `history_db`, this stops mattering: history comes from the `annotation` table, which
holds one row per (user, item), and PAMTS takes `MAX(play_date)` per file. That is the
direct answer to "has anyone played this", for any number of users, with no extra logins.

`getNowPlaying` is worth knowing about too: unlike the annotations it is **not**
user-scoped — the Subsonic API returns every user's active session, each carrying a
`username`. So one polling account feeds observed plays for everybody. Some servers gate
parts of the API by role, so confirm your polling account really does see other users'
sessions before relying on it for that.

Subsonic also reports song `path` **relative to the music folder**, so `music_folder` is
required — it is the absolute base PAMTS prepends before mapping onto your tiers.

## Two promotion triggers, and why the second exists

`now_playing()` asks "what is streaming right now". That is ideal when it works: it gives
the remaining runtime, so PAMTS can size the copy to finish before the current item ends.

`recent_plays(since)` asks the server's own play records "what was played since I last
looked". It exists because the first question has two blind spots:

- **Other users.** Whether a session API reveals other people's sessions is server- and
  role-dependent. Play *records* are not: they are written for everybody.
- **The gaps between polls.** A session poll only sees what is playing at the instant it
  asks. Anything that started and finished in between is invisible. A recorded play is
  not.

The cost is precision: a play is recorded at or near the end of a track, so there is no
remaining time and no time budget can be computed. For music tracks that hardly matters,
and the time budget has a floor, so it degrades rather than breaking.

PAMTS uses both when both are available, de-duplicated by path, and keeps a per-player
watermark in its state file. **On first sight of a player the watermark is set to "now"**
and detection starts from there — otherwise the first run would treat the entire recorded
history as "just played" and try to promote the whole library.

This is what makes a multi-user music library work automatically, with no privileged
account: `history_db` supplies both the ranking history and the play trigger, and both
cover every user.

## Several players at once

`[[players]]` takes any number of adapters, and this is the normal case for music: a
library commonly has two or three things serving it.

Play history is merged with `sweep()`, taking the **most recent** play per file and
de-duplicating by fast-tier path. Both halves matter:

- merging by maximum — an album played in one app looks untouched to the others, so
  ranking on a single app's view would evict content that *was* played
- de-duplicating by path — different servers identify the same album by different ids, so
  without it the same album is pinned twice under two keys and consumes the pin budget
  twice

## Observed play history

PAMTS records what it sees playing, in its own state file, and merges that with whatever
the players report. That is what makes a history-less adapter like LMS usable for ranking.

It is scan-immune by construction: a library scan never appears as a *playing session*, so
it can never write a record. Bounded by `[history] max_entries` and `max_age_days`.

A 60-second poll can miss something very short, and "seen playing" is not quite "played to
completion" — someone skipping through an album marks those tracks as touched. For tiering
purposes that is the right answer anyway: a human was there.

Disable with `[history] observe = false` if every one of your players reports real
history.

## What an adapter must answer

| method | used by | must return |
|---|---|---|
| `available()` | both | `True` if configured and reachable; log why not |
| `library_items()` | tiering | every item, with last-played time — or `None` on failure |
| `now_playing()` | promotion | `[PlayEvent]` for what is playing now |
| `recent_plays(since)` | promotion | `[PlayEvent]` for plays *recorded* since then (optional) |
| `locality_group(event)` | promotion | `[Candidate]` likely to follow, in play order |

And two class attributes declaring what it can actually do:

| flag | meaning |
|---|---|
| `provides_history` | can `library_items()` return real play history? `False` is legitimate — see LMS below |
| `provides_sessions` | can `now_playing()` report what is playing? |

Declaring `provides_history = False` is not a failure mode. The engine skips that adapter
when building the ranking and leans on observed plays instead.

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
ADAPTERS = {
    PlexPlayer.kind: PlexPlayer,
    LmsPlayer.kind: LmsPlayer,
    NavidromePlayer.kind: NavidromePlayer,
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

Music is a natural fit — the locality unit is the album — but two things need planning.

**Several consumers.** Music is often served by more than one thing at once. `[[players]]`
handles the *history* side, but each consumer also has to see the unified path, so a
per-client union filesystem must be configured on every one of them and any consumer you
miss sees the split tiers. Where that applies, putting the union on the *server* and
exporting the merged view is usually the better arrangement.

**Different path shapes.** Each server reports paths its own way — LMS reports the
filesystem path it scanned, Subsonic reports a path relative to its music folder. List a
`[[roots]]` entry for whatever each one actually reports; they all resolve onto the same
two tiers, and `sweep()` de-duplicates by the resolved fast path.
