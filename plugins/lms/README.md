# PAMTS play-history plugin for Lyrion Music Server

LMS records play counts and last-played times, and that data deliberately survives
library rescans — which makes it exactly the right signal for deciding what belongs on
fast storage.

**But none of it is reachable over the server's API.** Neither the `titles` nor the
`songinfo` query returns `playcount` or `lastplayed`, with any documented tag, and no
built-in plugin exposes it. Verified against LMS/Lyrion 9.x.

This plugin closes that gap the right way round. It runs in-process, where the data is
legitimately available, and publishes it as an ordinary **CLI query** — so it is
automatically available over the same `jsonrpc.js` endpoint every other query uses. No new
port, no new credentials, no new transport, and nothing outside the server needs
filesystem access or knowledge of the database schema.

Read-only: it never writes to the database.

## Install

```sh
# on the LMS host
cp -r PAMTS /var/lib/squeezeboxserver/Plugins/
chown -R squeezeboxserver: /var/lib/squeezeboxserver/Plugins/PAMTS
systemctl restart lyrionmusicserver     # or squeezeboxserver, depending on your install
```

The plugin directory name must be `PAMTS`, because `install.xml` declares the module as
`Plugins::PAMTS::Plugin`. Adjust the path if your install keeps third-party plugins
elsewhere (check Settings → Plugins, or `--pluginsdir`).

A restart is required — LMS discovers plugins at startup. Confirm it loaded:

```sh
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"id":1,"method":"slim.request","params":["",["pamts","info","?"]]}' \
  http://localhost:9000/jsonrpc.js
```

```json
{"result":{"tracks":218667,"played":35034,"newest_lastplayed":1790506009,
           "max_page":5000,"version":"0.1.0"}}
```

If `result` comes back empty, the plugin is not loaded. Check the server log for
`plugin.pamts`, and that Settings → Plugins lists *PAMTS play history* as enabled.

## Queries

### `pamts info ?`

| field | meaning |
|---|---|
| `tracks` | rows in `tracks_persistent` |
| `played` | of those, how many have ever been played |
| `newest_lastplayed` | unix timestamp of the most recent play |
| `library_tracks` | tracks currently in the library (`tracks`, excluding remote) |
| `newest_added` | unix timestamp of the most recent library addition |
| `newest_year` | highest release year present |
| `max_page` | largest page this plugin will return |
| `version` | plugin version |

### `pamts added <index> <quantity> [since:<epoch>]`

Recently **added** tracks, newest first — whether or not they have ever been played.

### `pamts released <index> <quantity> [minyear:<year>]`

Tracks by **release year**, newest first. Tracks with no year tagged are excluded, since
they would otherwise sort as the oldest possible and fill a "newest releases" page.

Both return `url`, `added`, `year`, `filesize`, `lastplayed`, `playcount`, with a `count`
of the total matching. `lastplayed` is `0` for never-played tracks, which for these two
queries is the common case — and the point of them.

**Why these exist.** "Never played" is not the same as "not wanted". Music added last week,
or released this year, is very likely to be played soon and belongs on fast storage despite
having no play history. Ranking purely on plays would send it straight to slow storage, so
the first listen would have to wake a spun-down array. These queries read `tracks` rather
than `tracks_persistent`, so never-played tracks are included.

The age thresholds are the caller's parameters (`since:`, `minyear:`), deliberately — the
policy belongs with whatever is making the tiering decision, not baked in here.

### `pamts history <index> <quantity> [since:<epoch>]`

One page of played tracks, **oldest play first**, with a `count` of the total matching.

| field | meaning |
|---|---|
| `url` | the track URL as LMS holds it — a percent-encoded `file://` URL for library files |
| `lastplayed` | unix timestamp of the most recent play |
| `playcount` | total plays |
| `filesize` | bytes, or `0` if the file is no longer in the library |
| `added` | unix timestamp the track entered the library, `0` if unknown |
| `year` | release year, `0` if not tagged |

`since:` returns only tracks played after that timestamp, which is what makes incremental
polling cheap — PAMTS uses it to ask "what changed since I last looked".

```sh
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"id":1,"method":"slim.request","params":["",["pamts","history","0","500"]]}' \
  http://localhost:9000/jsonrpc.js
```

## Notes

- **`filesize` is often `0` for old records**, and that is correct: LMS keeps a play
  record after the file leaves the library, so roughly half of a long-lived history has
  no current file to join to. PAMTS ignores those — they cannot be on either tier.
- **Non-file URLs appear too** (radio, podcasts). They are returned as-is; PAMTS skips
  anything that is not a `file://` URL.
- **Paging is stable.** Results are ordered by `lastplayed` then `id`, so rows are
  neither skipped nor repeated across pages when many tracks share a timestamp.
- **Page size is capped** at 5000. Asking for more returns 5000.

## Using it with PAMTS

Nothing to configure. The `lms` adapter probes for the plugin at startup and uses it
automatically:

```
[lms] history plugin v0.1.0 present: 35034 played of 218667 track(s)
[lms] 34612 played track(s) from the plugin
```

Without it, the adapter reports no play history (promotion still works, ranking falls back
to PAMTS's observed plays) unless you point `history_db` at `persist.db` — which the
plugin exists to make unnecessary.
