# Dashboard

A small web view of what PAMTS is doing now and what it has done. Standard library
only, one page, no build step.

```sh
pamts-web.py --config /etc/pamts/pamts.toml --listen 127.0.0.1:8622
```

Then open it. Two views: **Now** and **History**.

## What it shows

**Now** — one card per data source:

- **Access observer** — how many files are being read this second, events seen, and
  whether the tap is *keeping up*. That last one matters: a tap that has silently
  stopped looks exactly like an idle estate, so the page derives it from
  `kernel_emitted` versus `records` rather than making you compare two numbers.
- **Fast tier** — bytes used against the budget, per tier job, with the bar turning
  amber past 85% and red past 95%.
- **Promotion** — how many items are protected from eviction, the measured
  throughput, and the most recent promotions.
- **Configuration** — budget, pin limits, jobs, players.
- **Last 24 hours** — session counts per verdict, which is the quickest way to see
  whether a scan has been running.

Below the cards, **Reading right now** lists every in-flight session with its
verdict, so you can see a scan being rejected as easily as a play being counted.

**History** — the classified session log, newest first: time, verdict, client, size,
coverage, rate, file. This is the view worth having, because it shows what was
*rejected* as well as what counted. `PROBE` and `COPY` rows are the system declining
to treat a scan as demand.

## Adding a source

This is the extension point. A source is one class and one registry line — the same
shape as adding a player adapter.

```python
class MySource(pamts_dash.Source):
    name = "mine"
    ttl = 10.0                      # seconds before it is collected again

    def available(self):            # cheap check; no data fetched
        return os.path.exists("/some/thing")

    def collect(self):              # -> any JSON-serialisable dict
        return {"answer": 42}

pamts_dash.SOURCES["mine"] = MySource
```

That is all. `/api/state` picks it up, and **the page renders it without being
changed** — an unrecognised source is displayed as raw JSON rather than hidden. Give
it a nicer card by adding one function to `RENDER` in `web/index.html`, keyed by the
source name; until you do, the data is still visible.

Two rules the core relies on:

- **A failing source must not cost you the others.** `collect_all()` isolates each
  one and reports its error beside the data that did arrive. A dashboard that goes
  blank because one component is down is worse than one that shows most of the truth
  and names what is missing.
- **Respect `ttl`.** The page polls every five seconds. Scanning a filesystem on
  every poll is not acceptable, which is why `TierSource` has a two-minute ttl while
  `ObserverSource` has two seconds.

## API

| | |
|---|---|
| `GET /` | the page |
| `GET /api/state` | `{generated, data: {source: …}, errors: {source: …}}` |
| `GET /api/history?since=&limit=` | sessions, plays, and 24-hour verdict counts |
| `GET /api/sources` | what is registered, and whether it is available |
| `GET /health` | liveness |

`?force=1` on `/api/state` bypasses the ttl caches.

The page fetches **relative** URLs, so it works unchanged behind a reverse proxy on a
sub-path.

## Where it reads from

Nothing is written. The observer's SQLite store is opened **read-only** (`mode=ro`)
so a running daemon is never disturbed.

| source | reads |
|---|---|
| `observer` | the observer daemon's HTTP API |
| `history` | the observer's SQLite store, read-only |
| `state` | `state_file` — promotions and measured rate |
| `config` | your `pamts.toml` |
| `tier` | walks each tier job's source directory |

`history` reads the database directly rather than the HTTP API because the API
exposes play history but not the classified session log, and the session log is what
makes the history view worth looking at.

## Access

There is **no authentication**. It is read-only — no endpoint changes anything — but
the data includes file paths and client addresses.

The default binds to `127.0.0.1`. `--listen 0.0.0.0:8622` exposes it to your network,
which is reasonable on a trusted LAN and is the point of a dashboard you want to open
from another machine. Beyond that, put a reverse proxy with authentication in front
of it, or bind localhost and tunnel.
