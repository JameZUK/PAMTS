# The access observer

A player adapter tells you what one *player* knows. That is per-user, per-service,
and some services will not tell you at all. The filesystem sees every consumer
equally, so if your media is served over NFS from a single host, one observer on
that host is worth more than one adapter per service.

This document is the design, and — more usefully — the list of things that are
not obvious until you have measured them.

## What it is for

Two jobs, and they pull in opposite directions:

- **promotion** wants to know "is this being played *now*", quickly
- **eviction** wants to know "when was this last genuinely wanted"

and the founding constraint of the whole system sits underneath both: **a library
scan must never move data.** Any signal that cannot tell a scan from a play is
worse than no signal, because it will cheerfully promote your entire library
every time your media server re-indexes.

## What it cannot do

- **No per-user history.** Exports using `all_squash` erase user identity at the
  server; every request arrives as the anonymous uid. You get *file-level
  demand*. That is the right signal for tiering and a poor substitute for
  scrobbles.
- **No client attribution from ftrace alone.** The nfsd read tracepoints carry
  `xid` and `fh_hash` but no client address, so two clients reading one file at
  once merge into a single session. Recovering the client needs eBPF and
  `svc_rqst`.
- **Co-tenanted services are indistinguishable.** If two music servers run on
  one host, the NFS mount is per-host, so even with the client address you cannot
  say which of them read the file. This does not hurt tiering — a play is a play.
- **Local readers are invisible**, because they never become NFS requests. This
  is usually a *feature*: your own backup and maintenance jobs run on the storage
  host and so cannot be mistaken for demand.

## The read lifecycle

Every nfsd read tracepoint carries the same four fields — `xid`, `fh_hash`,
`offset`, `len` — which makes the whole request joinable on `xid`:

```
read_start(len = requested)
    -> read_splice | read_vector | read_direct        (the method)
    -> read_done(len = delivered)  |  read_err(status)
```

**Use both halves.** `read_done` is the authoritative byte count, and summing
`len` across several tracepoints double-counts every single read — on a real
capture there were exactly 15,716 `read_done` and 15,716 `read_splice` events,
so naive summing reports precisely 2x the traffic and concludes that everything
is playback. But *which* method fired is a feature worth keeping, not noise to
discard, so the method events are retained and only `read_done` is counted.

### EOF is where coverage comes from

`read_done.len < read_start.len` means the client reached end of file, so
`offset + len` reveals the **file size**. Coverage — bytes read divided by file
size — is therefore computable **without ever resolving `fh_hash` to a path**,
and coverage is the strongest play-versus-scan discriminator available.

This matters because path resolution is the expensive part of this problem:
`fh_hash` is a *hash* of the NFS filehandle, not an inode and not a name.
Reproducing the kernel's internal hash to build a reverse table is fragile across
kernel versions. Getting coverage for free sidesteps it for classification, and
you only need real paths at the point where you actually act.

`read_start` is not required: if only the method and completion tracepoints are
enabled, the method event supplies the requested length and short reads are still
detected.

## Signature

Per file, per session (sessions split on `idle_gap` seconds of silence):

| feature | why |
|---|---|
| `bytes` | `read_done` only |
| `filesize`, `coverage` | from EOF sightings; may be unknown |
| `monotonic` | fraction of reads advancing; streaming walks forward |
| `rate` | **`None`** when no time elapsed — see gotchas |
| `duration`, `requests` | pacing |
| `regions` | discontinuity count — see gotchas |
| `frac_splice/vector/direct` | the method mix |
| `short_reads`, `errors` | EOF and failures |

## Classes

Only **PLAY** should ever drive promotion.

- **PROBE** — a sip of the head, sometimes head *and* tail. Tag reads, container
  probes, thumbnailers. Never act on these.
- **PLAY** — progressive, paced, and sustained long enough for the pacing to mean
  something.
- **COPY** — the whole file, flat out. A copy, an analysis pass, a fingerprinter.
- **FETCH** — the whole file, fast, but small. See below; this class exists
  because the honest answer is "cannot tell".
- **BULK** — whole-file reads arriving in a crowd. Decided across files, not
  within one.
- **SEEK**, **ERROR**, **UNKNOWN**.

### FETCH, and an ambiguity with no clean answer

A player buffering a whole 5 MB track in 0.3 s and a fingerprinter reading that
same track in full produce **identical** per-file signatures. Both are
whole-file, both are fast. No amount of cleverness inside one file's read pattern
separates them.

What separates them is whether the *neighbours* were read too. A player reads one
track then waits roughly its playing time; a sweep reads hundreds back to back.
So the per-file classifier says `FETCH` and declines to guess, and `detect_bulk`
decides across files. Policy then chooses whether a lone `FETCH` counts as
demand — for tiering it reasonably does, since something read the file in full.

This is the main reason the classifier must not be tuned on video alone. Video
files are large enough that playback is always visibly paced; music files are
small enough that they often are not.

## Gotchas

1. **`read_done` + `read_splice` double counting.** They are the same read. 1:1
   on real traffic, so the error is exactly 2x and uniform, which makes it easy
   to miss.
2. **`xid` wraps.** It is a 32-bit RPC transaction id, so joins must be scoped to
   a short time window. Joining globally fuses unrelated reads of different
   offsets into one nonsensical session.
3. **Never fabricate a rate.** With one read there is no elapsed time and so no
   measurable rate. Substituting the byte count made a single 0.4 MB read score
   as paced playback. `rate` is `None`, and `PLAY` requires a real duration and a
   minimum byte count.
4. **`regions` measures readahead, not seeking.** A real 26 Mbps video stream
   scored **6,574 regions** at a 4 MB gap threshold, because clients read ahead in
   interleaved bursts. Only trust it alongside a low `monotonic` score.
5. **Calibrate on the traffic you have.** Thresholds separating "paced" from
   "flat out" are bandwidth- and client-dependent. Capture first, classify
   afterwards.
6. **A scan that reads whole files is not a PROBE.** Loudness analysis and
   acoustic fingerprinting read everything. `BULK` catches these, not `PROBE`.

## Measured, on a 10-minute capture

37 distinct files, 15,716 reads, zero lost events:

| session | reads | bytes | coverage | duration | rate | label |
|---|---|---|---|---|---|---|
| video stream | 15,369 | 1,936 MB | 0.21 | 592 s | 3.27 MB/s | PLAY |
| music track | 57 | 7.1 MB | 1.00 | 105 s | 0.07 MB/s | PLAY |
| whole small file | 44 | 5.4 MB | 1.00 | 0.3 s | 17.1 MB/s | FETCH |
| head/tail probes (x21) | 5 | 0.4 MB | 0.01–0.02 | 0.3 s | ~1.7 MB/s | PROBE |

The 21 probes sat at 1–2% coverage across exactly **two regions** — head and
tail — and are cleanly separated from the two plays. That separation is the whole
premise, and it holds.

## Capturing

An isolated ftrace instance, so nothing global is disturbed and cleanup is a
directory removal:

```sh
D=/sys/kernel/tracing; I=$D/instances/pamts
mkdir -p $I
echo 32768 > $I/buffer_size_kb
echo 1 > $I/events/nfsd/nfsd_read_done/enable
echo 1 > $I/events/nfsd/nfsd_read_splice/enable
: > $I/trace
# ... wait ...
echo 0 > $I/events/nfsd/nfsd_read_done/enable
echo 0 > $I/events/nfsd/nfsd_read_splice/enable
cp $I/trace /tmp/capture.txt
rmdir $I
```

Check the capture for lost events before trusting it. Then:

```python
import pamts_observer as obs
for sig, label in obs.analyse(open("/tmp/capture.txt", errors="replace")):
    print(label, sig["bytes"], sig["coverage"])
```

ftrace is the right tool for *calibration*: no compiler, no kernel module,
reverted by removing a directory. It is the wrong tool for the permanent
collector, because `fh_hash` is a hash and there is no client address. For that,
use the eBPF collector.

## The eBPF collector

`observer/pamts_nfsd.bpf.c` attaches to the nfsd read tracepoints and reports
what ftrace cannot: a real **inode**, the **device**, and the **client address**.

### Build once, run anywhere

Only the build host needs `clang` and `bpftool`. The host that RUNS the collector
needs `libbpf.so.1` and nothing else, because `observer/pamts_bpf.py` loads the
object through ctypes. That matters when the machine doing the serving is one you
would rather not install a toolchain on.

```sh
ssh storage-host cat /sys/kernel/btf/vmlinux > vmlinux.btf
make -C observer vmlinux.h BTF=$PWD/vmlinux.btf
make -C observer
# ship observer/pamts_nfsd.bpf.o plus the Python. No compiler on the target.
```

`vmlinux.h` must come from the **target** kernel's BTF, not the build host's.

### Why raw tracepoints

Attachment is by raw tracepoint *name*, which needs no `tracefs` or `debugfs`.
Those are usually absent inside a container, and mounting them means editing the
container config and restarting it — and restarting an NFS server takes down
every guest that mounts it. Raw tracepoint attach avoids the problem entirely.

It does need `CAP_BPF` and `CAP_PERFMON` (or `CAP_SYS_ADMIN`), which an
unprivileged container does not hold in the initial user namespace. A privileged
container does.

### nfsd is a MODULE, and this is the trap

`btf_trace_nfsd_read_done` and `struct svc_fh` are **not in vmlinux BTF**. They
live in `/sys/kernel/btf/nfsd`, split BTF layered on vmlinux. Consequences:

- looking for the tracepoint signatures in vmlinux BTF finds nothing, and it is
  easy to conclude they do not exist and start guessing instead
- `bpftool btf dump file nfsd` fails without `-B /sys/kernel/btf/vmlinux`
- `struct svc_fh` cannot come from `vmlinux.h`, so the program declares only the
  field it needs as a CO-RE *flavor* (`struct svc_fh___pamts`, the `___suffix`
  being stripped when matching) and libbpf relocates it against the module BTF

Verified TP_PROTO on the target kernel rather than assumed:

| tracepoint | args (after the implicit ctx) |
|---|---|
| `read_start`, `read_done`, `read_splice`, `read_vector`, `read_direct` | `(struct svc_rqst *, struct svc_fh *, u64 offset, u32 len)` |
| `read_err` | `(struct svc_rqst *, struct svc_fh *, loff_t offset, int status)` |

As raw tracepoint arguments these are `ctx->args[0..3]`. BTF encodes a leading
`void *` for the tracepoint typedef; raw_tp args do not include it.

### Two more traps

- **`rq_xid` is `__be32`.** The kernel's own tracepoints byte-swap before
  printing, so the collector must too, or captures disagree with ftrace.
- **Kernel `dev_t` is not Python's `st_dev`.** `super_block.s_dev` packs
  `(major << 20) | minor`; glibc uses a different 64-bit layout. Comparing them
  directly never matches, and every path lookup then fails while looking merely
  like a cold index. `kdev()` in the daemon converts.

### Reporting a play while it is still playing

A session is only fully known once it ends, but a 45-minute episode reported only
at the end is useless for promotion, which has to fetch the next item *during*
playback. `checkpoint_after` (default 60 s) reports an in-progress session once it
already classifies as PLAY; the final report then refreshes it without counting
the play twice.

`/sessions` answers "what is playing now"; `/history` answers "what has been
played". Promotion should consult both.

### Players read in bursts, and one viewing is not one session

This is the finding that most changed the design, and it only appeared once the
daemon was left running against real traffic.

A media server does not read steadily while you watch. It reads far ahead, goes
**silent** while the client drains its buffer, then refills in a burst faster than
playback. Measured on one 2160p episode:

```
19:06  206 MB   60s  3.4 MB/s   checkpoint
19:10  952 MB  294s  3.2 MB/s   closed
19:11  199 MB   61s  3.3 MB/s   checkpoint
19:28 3369 MB 1071s  3.1 MB/s   closed
19:40  553 MB   58s  9.6 MB/s   checkpoint   <- after ELEVEN MINUTES of silence
```

Every row is the same file, one continuous viewing. With a 30-second `idle_gap`
that became several sessions and **three plays**.

Widening `idle_gap` enough to absorb an eleven-minute gap would make a pause
indistinguishable from starting the next episode, so the fix is elsewhere:
`replay_gap` (default 1800 s) treats the same file seen again within that window as
the **same viewing**. `last_play` advances; `play_count` does not. A genuine rewatch
the next day still counts.

Two things follow from the same behaviour:

- **Coverage is often unknown per session**, because a fragment rarely contains the
  read that hits EOF. Do not rely on coverage being present; the classifier already
  treats `None` as "unknown" rather than "zero".
- **Rate is measured over the burst, not the viewing.** That 9.6 MB/s row is three
  times the playback rate, and on a faster link a refill burst could approach the
  COPY threshold. If plays start being labelled COPY, this is why.

### The start of playback does not look like playback

A second finding from the same live run. When you press play, the server grabs a
large chunk fast before settling to the playback rate. Measured:

```
21:21:07  207.8 MB  10s  20.7 MB/s  mono=1.00  cov=0.143   -> UNKNOWN
```

It missed `play_min_coverage` (0.15) by 0.007 and `play_long_duration` (30 s) by
20 s, so it matched nothing. That is worse than a cosmetic mislabel, because
`now_playing()` filters on `PLAY`: **promotion ignored the exact moment an episode
started**, which is when fetching the next one matters most. It recovered at the
60 s checkpoint, but on a faster link bursts get shorter and faster, and a viewing
made entirely of short bursts would never enter history at all.

So `play_min_bytes_abs` (default 64 MB) qualifies a session on volume alone: a
*sequential* read of hundreds of megabytes of one file is playback whatever fraction
of the file it represents, because nothing else reads that much in order and stops.
The monotonic requirement still gates it — the same volume read scattered is `SEEK`,
and a whole file at wire speed is still `COPY`.

### Coverage can exceed 1.0

Seeking and re-reads mean bytes read can exceed the file size, so treat coverage as
"how much was read relative to the file", not a fraction bounded at 1. A measured
episode reported `cov=1.011`. It is harmless for `PLAY`, but be aware that an
inflated coverage combined with a fast burst is the route by which a genuine play
could be labelled `COPY`.

### A library scan put 1,350 JPEGs into play history

The single most important thing an overnight run revealed, and the closest this
came to violating its own founding constraint.

In fourteen hours the observer classified 91,725 sessions. **70,756 were `PROBE`
and correctly excluded** — the classifier did its main job well. But the `plays`
table ended up with **1,490 rows, of which 1,350 were `.jpg`**, 66 `.m3u` and 32
`.png`. Only about 39 were media.

Two independent causes, both now fixed:

**1. `detect_bulk` was never wired into the streaming path.** It only ran in
`analyse()`, the offline batch helper. The daemon calls `classify()` directly, so
the guard written specifically to catch sweeps was inert. `SessionTracker` now
applies the same decision from a rolling window, counting **distinct files per
client** — distinct files, not sessions, so one viewer re-reading one file in
bursts never looks like a sweep.

**2. Artwork and playlists were treated as tierable.** A 40 KB cover read whole at
1 MB/s is, by every measure the classifier has, a `FETCH` — and `FETCH` counts as
demand. `is_media()` now restricts play history to media extensions, and the check
lives inside `Store.record` rather than in its caller, so no code path can bypass
it by forgetting.

**A limitation to know about.** A rolling window cannot recognise a sweep until
enough of it has arrived, so the first `bulk_min_files` files still record as
demand. For artwork that no longer matters — the extension filter catches all of
it — but a music scan that reads whole tracks can still leak its first two dozen.
Recording demand only after the window has closed would remove that entirely, at
the cost of history lagging by `bulk_window`; promotion reads `/sessions`, not
`/history`, so that latency would be harmless.

### Ten tracks at once is not listening

Measured at 04:46:01: ten tracks from one album, each read 1.4 MB over exactly 27
seconds, all beginning in the same second, from one client. Each scored `PLAY` —
coverage 0.18–0.37 and monotonic 0.91 satisfy every per-file test.

Nothing about one file's read pattern can reject this. It is only recognisable
against its neighbours, which is the bulk guard's job, and it is why that guard
counts **all** of a client's recent sessions rather than only whole-file ones. Seen
in isolation, ten files is under the default threshold and still classifies as
`PLAY`; seen inside the scan wave it actually occurred in, all ten are suppressed.

### Reads on datasets you did not ask about

The tap sees **every** NFS read the server handles, including exports that have
nothing to do with the media you are tiering. In a first live run, 11 of 12
sessions were small whole-file reads of a different dataset: correctly classified
`COPY` and correctly kept out of history, but they would swamp a day's log.

So the daemon records which devices its `--root` paths actually live on and drops
records from any other device before they become sessions. `foreign_device_records`
in `/stats` counts them. `--all-devices` disables the filter, which is the thing to
reach for when something you expect to see is not appearing.

### Shutdown

Do not rely on `KeyboardInterrupt` propagating out of a ctypes call blocked inside
libbpf. In testing, SIGTERM killed the process while SIGINT left it running with
the programs still attached. The collector has an explicit `stop()` which the
daemon wires to SIGINT and SIGTERM, and `--duration` bounds a run without needing
signals at all. BPF links are fd-backed, so they do detach when the process dies.

## Measured live

On a real NFS server: ~180,000 files indexed, 150-second run.

```
PLAY   10.0.0.20   407.1 MB  dur=145s  2.8 MB/s   reqs=3257  splice
  .../Some Show (2024)/Season 2/Some Show - S02E04 ... .mkv
PLAY   10.0.0.20   136.1 MB  dur= 47s  2.9 MB/s   reqs=1091  splice  <- checkpoint
COPY   10.0.0.31     0.3 MB  dur=  0s  94.7 MB/s  reqs=3     splice
```

Two session rows for one viewing — the 47 s checkpoint and the 145 s final — with
`play_count = 1`. The small COPYs came from a host reading a dataset that was not
indexed, so their paths did not resolve and they never reached history: the right
outcome, for both reasons independently.

## Using it from PAMTS

`ObserverPlayer` (kind `observer`) reads this daemon like any other player:

```toml
[[players]]
kind = "observer"
name = "storage"
url  = "http://127.0.0.1:8621"
```

Only `PLAY` and `FETCH` count as demand (`demand_labels`). Its locality unit is
**sibling files in the same directory**, listed across *both* tiers — the next
episode is usually the one on the slow tier, which is exactly the file promotion
exists to fetch. See `docs/PLAYERS.md`.
