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

### Filtering, and why it is server-side

Scans and copies outnumber plays enormously. On a real estate, the 200 most recent
sessions were **200 `COPY` and zero plays** — the unfiltered view was not merely
cluttered, it showed no media activity at all.

So the filter is applied **in SQL**, not in the page. A client-side filter on the most
recent 200 rows would have discarded 200 of them and shown nothing.

`Media only` is the default and uses the observer's own notion of demand
(`PLAY`, `FETCH`); `All` shows every verdict; the individual chips toggle one at a
time, with 24-hour counts beside each. The chip list is built from the labels **present
in the data**, so a verdict added later appears without touching the page. The
selection is remembered per browser and applies to the live list too, so "media only"
means the same thing in both views.

    GET /api/history?labels=PLAY,FETCH
    GET /api/history?labels=demand        # whatever the observer counts as demand

Labels arrive from a query string and are passed as SQL parameters; there is a test
asserting a label containing SQL is treated as data.

## Views

**Now** — one card per source, plus a per-pool utilisation panel and what is being read
right now. **Transfers** — every promotion and demotion, filterable by direction.
**Graphs** — throughput, utilisation over time, and reads by verdict. **Sessions** — the
classified session log. **Config** — what PAMTS is actually configured to do, so nobody
has to shell in and read the TOML.

## Budgets are per pool, and so are the percentages

A tier job may carve out its own `budget_gb`; the rest share the global one. The tier
card and the utilisation panel therefore report **one bar per pool**, each against its
own budget.

This used to be one total divided by the shared budget, which printed
**"281.2% of 400.0G budget"** on a system where nothing was over budget at all — 1.07 TB
of video *and* music measured against the 400 GB video allowance. There is deliberately
no estate-wide percentage now, because there is no single budget to divide by.

Each bar also carries a tick at the **eviction target**: `budget - [promote] headroom_gb`.
That is the line eviction actually works to, and showing only the budget hides why
eviction stops where it does.

## History and graphs

`pamts_events` records, append-only, to `[paths] events_db`:

* **transfers** — one row per item moved, either direction, with bytes and seconds. This
  is both the promotion/demotion history and the source of the throughput chart.
* **samples** — one row per pool per run. Sampled rather than derived from transfers,
  because content also arrives and leaves by other means and a derived figure would
  drift from reality.

Recording is **fail-safe**: if the store cannot be opened or written, it becomes a no-op.
Losing a graph is a nuisance; aborting an eviction half way through because a telemetry
table was locked is a real problem.

Bucketing happens in SQL, not the browser: a night of eviction is ten thousand rows and
the chart is a few hundred pixels wide. `GET /api/events?window=&buckets=&limit=`.

Charts are hand-drawn inline SVG with **no charting library**, because the page makes
zero external requests — it has to work on a storage box with no route to the internet.
Lines **break at gaps** rather than interpolating: a missing sample means PAMTS did not
run, and a straight line across it would invent history.

## Tier: which tier served each read

Every session records whether the read was answered by the fast tier or the slow one.
The history table shows a `HOT` / `COLD` badge per row, and the heading carries the
24-hour ratio over **demand reads only** (`PLAY`, `FETCH`) — a cold `PLAY` is a spin-up
somebody waited for, whereas a cold `COPY` is just the backup doing its job and would
swamp the number.

Two things make this harder than it looks.

**It cannot be derived later.** A read arriving through a mergerfs union reports the
*union's* device to nfsd, not the branch's, so nothing in the event says which tier
answered. And by the time anyone looks, the file may have moved — an eviction run
relocates thousands of items. So the tier is resolved and stored at access time.

**Resolving it must not wake the array.** The union's search policy is `ff` and the fast
branch is first, so a file present on the fast branch is necessarily the one being read.
One `lexists` on the fast branch settles it. The slow branch is **never** stat'd:
confirming a miss there would spin the disks up to answer a question about a read that
has already been served, which is the exact cost this system exists to avoid. Absent
from fast means cold.

Configure it on the observer, one mapping per union:

```
--tier-map /media/library/tv=/media/media-cache/tv
--tier-map /media/library/movies=/media/media-cache/movies
--tier-map /media/library/music=/media/media-cache/music
```

Without any `--tier-map` the column stays `NULL` and the page says the tier was not
recorded rather than showing a misleading 0%. A database written before this existed
gains the column on first open; its old rows keep `NULL`, because their tier genuinely
was not recorded and guessing would be worse than admitting it.

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
