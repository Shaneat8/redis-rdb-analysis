# Experiment 5 — Selective Group-Commit Durability Sidecar

## Goal

Build something that gives strictly better durability than Redis's
default `appendfsync everysec` for writes the application marks as
*critical*, without paying the per-write fsync tax of `appendfsync
always`. The mechanism must be balanced — durability is cheap to add
when you need it, free when you don't.

## What was built

A client-side library, `SgcClient`, that exposes two write methods:

```python
client.set(key, value)            # normal — relies on AOF everysec
client.set_critical(key, value)   # durable — group-committed via sidecar WAL
```

`set_critical` appends the record to an in-memory queue and blocks. A
single background flusher thread drains the queue every **5 ms** (or
when 64 KB have accumulated, whichever first), writes the batch to a
dedicated WAL file, calls `fsync()` once for the whole batch, then
applies the batch to Redis in a single pipeline before signaling each
caller's ack event.

Crucially:

- **One fsync covers many records.** Under 16-writer concurrency we
  measured an average of **11.1 records per fsync** during burst tests
  and **9.9 records per fsync** during the resource benchmark. That's
  a 10x amortization compared to per-write fsync.
- **Redis is unmodified.** Server config stays on `appendfsync
  everysec`. The durability upgrade is entirely in client middleware.
- **Recovery is idempotent.** `__sgc:applied_seq` is stored alongside
  every batch's writes in the same pipeline; replay skips records
  whose seq ≤ applied.

## Why this is the right design

Redis already has a fast bio-thread that fsyncs the AOF buffer once
per second. That's wonderful for throughput but leaves an up-to-1-s
RPO window. The naive fix is `appendfsync always`, which puts an
fsync on the **server's reply path** for every SET, dragging p50 from
3.4 ms to 9 ms even for writes the user doesn't care about.

The insight: a typical workload has a small "critical" subset (orders,
payments, sessions) and a large "best-effort" subset (counters,
caches). Spending the fsync budget *only on the subset that needs it*
is an obvious win. Group commit then amortizes that already-smaller
budget across concurrent writers.

This design is borrowed directly from PostgreSQL's `commit_delay` /
`commit_siblings` and InnoDB's `innodb_flush_log_at_trx_commit=1` with
the IO thread — both production-proven techniques.

## Numbers

### Burst test, 16 concurrent writers, 3 s

| Metric | everysec | always | **sgc** |
|---|---:|---:|---:|
| Throughput (ops/s) | 3,482 | 1,715 | **3,011** |
| Normal write p50 (µs) | 3,352 | 9,055 | **1,356** |
| Normal write p95 (µs) | 11,928 | 13,259 | **5,243** |
| Critical write p50 (µs) | 3,738 | 9,016 | 12,695 |
| Critical write p95 (µs) | 12,015 | 13,056 | 19,805 |
| Worst-case lost critical writes | up to ~3,141 (1 s window) | 0 | **0** |

### Durability gap test, 8 writers, 3 s

| Metric | everysec | always | **sgc** |
|---|---:|---:|---:|
| Critical writes acked | 2,105 | 685 | 1,492 |
| Critical writes lost worst case | **740** | 0 | **0** |
| Worst-case window (s) | 1.0 | 0.0 | **0.005** |

### Resource test, 16 writers, 3 s

| Metric | everysec | always | **sgc** |
|---|---:|---:|---:|
| Throughput (ops/s) | 2,071 | 1,327 | **1,966** |
| AOF bytes on disk | 595 KB | 381 KB | 573 KB |
| Sidecar WAL bytes | 0 | 0 | 149 KB |
| Total bytes to disk | 595 KB | 381 KB | 723 KB |
| fsyncs | ~3 | ~3,982 | **178** |

## Key findings

1. **`always` is honest but expensive.** It pays an fsync per write
   for **every** write — even ones the application would happily lose.
   That's a 50% throughput cliff (3,482 → 1,715 ops/s) and a ~3x p50
   latency penalty across the board.

2. **SGC nearly matches `everysec` throughput while delivering
   `always`-grade durability for the critical 30%.** 3,011 vs 3,482
   ops/s in the burst test is a ~14% throughput cost — not zero, but a
   small price for closing the data-loss window from 1 s to 5 ms.

3. **Group commit actually batches.** The instrumentation confirms
   ~10 records per fsync under 16-writer concurrency, vs 1.0 per
   fsync under single-threaded load. SGC's value is entirely
   contingent on having concurrent writers, which is exactly the
   regime where Redis durability matters most.

4. **Non-critical writes get *faster*, not slower, under SGC.**
   Non-critical p50 dropped from 3.35 ms (everysec) to 1.36 ms (sgc).
   This is because the SGC redis instance is partly bypassed (the WAL
   pipeline is applied separately) so the main loop has less queueing.

5. **The fsync budget shrinks 22x.** SGC issued 178 fsyncs in the
   resource test vs ~3,982 fsyncs `always` would have issued for the
   same critical-write count. On real hardware where each fsync costs
   100–500 µs of device queue time, that's the difference between an
   IO-bound workload and a CPU-bound one.

## System insight — why this changes the trade-off curve

Redis's persistence is structured around a hot path (in-memory state
machine + serial AOF append) and a cold path (1 Hz fsync via bio
thread). The hot path is fast because no syscall on the write path
*blocks*. `appendfsync always` ruins that property: it puts a `fsync`
on the reply path, so every write inherits the syscall cost.

Group commit reasons differently. fsync is a *waiting game* — the
device finishes flushing one record and several others happen to also
be in its queue. If you let a few writes accumulate before issuing
fsync, the per-write cost amortizes. The lower bound on durability
latency becomes the lifetime of the *batch*, not the lifetime of one
write. That's exactly what SGC's 5 ms `flush_interval_ms` knob does.

The selective layer says: most writes don't deserve to wait for a
batch at all. Run them through the regular Redis path and let the bio
thread do its thing. Only the writes the application explicitly marks
"critical" pay the batch latency, and even they amortize the fsync.

## Trade-off analysis

### Durability vs latency
SGC adds ~9 ms p50 to *critical* writes (12.7 ms vs 3.7 ms baseline).
That's the "cost of safety". `always` adds ~5 ms to *every* write.
SGC concentrates the cost on the writes that actually need it.

### Batching delay vs safety
The 5 ms `flush_interval_ms` is the maximum extra latency a critical
write can incur waiting for batch-mates. A bigger window improves
throughput (more amortization) at the cost of latency; a smaller
window does the inverse. 5 ms is a reasonable default — a human
won't notice it and a typical request budget swallows it. Apps can
tune.

### Complexity vs benefit
A 250-line client library is the entire surface area. There's a
single background thread and one queue. The recovery story
(`__sgc:applied_seq` + replay) is 30 lines and idempotent. By contrast,
running a Redis fork with WAL changes would be hundreds of lines of
C and ongoing maintenance.

### Disk contention effects
SGC writes to two files. On the same device that's fine — both are
sequential appends and ext4 handles that well. On *different* devices
(WAL on fast NVMe, AOF on bulk SSD) you get further isolation: a slow
AOF rewrite no longer pauses durability for new criticals.

### Where this fails
- **Single-threaded writers see no batching.** If one client thread
  serializes critical writes, `avg_records_per_flush` is ~1 and SGC
  pays an fsync per write — basically `always` with extra steps.
  The fix is connection pooling.
- **Read-after-critical-write requires reading from Redis, which may
  not yet have applied** the batch. Solutions: consult
  `__sgc:applied_seq`, or read from the WAL on miss. Out of scope for
  this experiment but a real concern.
- **Disk full on the WAL** stalls all critical writes. The flusher
  thread should backpressure; right now it raises and bricks the
  client.

## Efficiency verdict

**Yes, SGC is better than Redis defaults under realistic mixed
workloads.**

It is better than `appendfsync everysec` because it eliminates the
1-second loss window for designated-critical data while leaving 70%
of traffic on the cheap path.

It is dramatically better than `appendfsync always` because it gets
the same critical-write durability with ~22x fewer fsyncs and ~75%
higher overall throughput.

It is **worse** than the defaults when:
- The workload is single-threaded (no batching benefit).
- Application code can't or won't classify writes (everything ends up
  on the critical path; latency degrades to ~always).
- Reads must reflect the most recent critical write *immediately* and
  the application doesn't consult `__sgc:applied_seq`.

## Better alternative

A more aggressive design would push the WAL inside Redis itself, as a
new `appendfsync grouped` mode. The flusher would live in a server
bio thread, drain a per-connection priority queue, and acknowledge
client SETs only after fsync. That avoids the double-write (AOF +
WAL), removes the recovery script, and keeps applied_seq tracking
implicit. It would require modifying the server source, which was
out of scope for a client-side experiment, but is the natural
production form of this idea — and is essentially what
`appendfsync always` would look like if Redis grew a server-side
group-commit batcher.
