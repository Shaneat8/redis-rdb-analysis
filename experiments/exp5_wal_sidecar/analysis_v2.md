# Experiment 5 (v2) — In-tree Adaptive Group-Commit AOF

## What was built

A new `appendfsync` mode, `groupcommit`, added directly to Redis 6.2.14's
AOF code path. The mode replaces second-resolution fsync triggering with
**adaptive sub-second batching** governed by two new config knobs:

| Config | Default | Meaning |
|---|---|---|
| `aof-groupcommit-window-ms` | 20 | Minimum ms between background fsyncs |
| `aof-groupcommit-max-bytes` | 65536 | Force fsync if unsynced bytes exceed this |

Both knobs are `MODIFIABLE_CONFIG` (changeable at runtime via `CONFIG SET`).

## Why this design (and what it improves over default)

`appendfsync everysec` triggers off `server.unixtime` — a `time_t`, ticking
once per second. That granularity bakes the up-to-1-second loss window into
the design itself: even under perfect conditions, a crash can lose all the
writes since the last second-aligned tick.

`appendfsync always` solves the loss problem by fsyncing on every write —
but pays one disk syscall per command. Throughput drops to ~30% and p50
latency rises 5×.

The new mode keeps the BIO-thread fsync (so the event loop never blocks on
disk) but **swaps the trigger granularity to milliseconds and adds a byte
ceiling**. The result is a knob you can dial: at 20 ms you get 50× tighter
durability than everysec for a measured 6% throughput cost; you can push
the window down to 5 ms (200 fsyncs/s) for the latency-sensitive shard or
up to 100 ms (10 fsyncs/s) for the cold path.

## Code changes (summary)

| File | What changed |
|---|---|
| `src/server.h` | Added `AOF_FSYNC_GROUPCOMMIT 3` enum + 5 new server fields (`aof_last_fsync_ms`, `aof_groupcommit_window_ms`, `aof_groupcommit_max_bytes`, `aof_groupcommit_fsyncs`, `aof_groupcommit_byte_triggers`) |
| `src/config.c` | Registered `groupcommit` enum value, `aof-groupcommit-window-ms` (Int 1–1000), `aof-groupcommit-max-bytes` (LongLong, MEMORY_CONFIG) |
| `src/aof.c` | (1) Added GROUPCOMMIT branch in the empty-buffer early-return so idle-tail fsync still fires when window elapses. (2) Added GROUPCOMMIT branch in `try_fsync` that schedules a background fsync if `(now_ms - last_fsync_ms) >= window_ms` OR unsynced bytes exceed `max_bytes`. |
| `src/server.c` | Initialize the new fields at server startup; expose `aof_groupcommit_fsyncs` and `aof_groupcommit_byte_triggers` in `INFO persistence`. |

The fsync still runs in a BIO thread via the existing `aof_background_fsync()`
helper — there is **zero new threading code**, so the change is small,
surgical, and inherits all the proven concurrency invariants of the existing
fsync infrastructure.

## Metrics summary

See `results_v2/metrics.txt` for the full tables.

Headline numbers:

| | `everysec` | `always` | `groupcommit` (20 ms) |
|---|---|---|---|
| Throughput (16 writers, 5 s) | 8,824 ops/s | 2,407 ops/s | **8,315 ops/s** |
| p50 latency | 1.44 ms | 7.01 ms | **1.50 ms** |
| p99 latency | 6.5 ms | 11.9 ms | **7.0 ms** |
| Worst-case loss window | 1,000 ms | 0 ms | **20 ms** |
| fsyncs/s under load | ~1 | ~2,400 | **50** |
| fsyncs per 1k ops | 0.12 | 1,000 | 6.42 |

## Log difference summary

The behavioral changes show up in three places:

1. **`INFO persistence` exposes two new counters**:

   ```
   aof_groupcommit_fsyncs:251           # in groupcommit mode after 5s burst
   aof_groupcommit_byte_triggers:0      # window elapsed first; byte path was a safety net
   ```

   Default modes report `:0` for both, allowing seamless monitoring across
   modes.

2. **`CONFIG GET appendfsync` accepts a fourth value**:

   ```
   redis-cli CONFIG GET appendfsync
   1) "appendfsync"
   2) "groupcommit"
   ```

3. **`CONFIG GET aof-groupcommit-window-ms` and `aof-groupcommit-max-bytes`**
   are visible/settable at runtime.

No existing log line is removed or changed — the patch is additive.

## Internal insight

The patch lives in `flushAppendOnlyFile()`, which is called from
`beforeSleep()` once per event-loop iteration. Under any non-trivial write
load, beforeSleep runs many times per millisecond, so the windowed
adaptive trigger fires within microseconds of its scheduled deadline. The
`mstime()` helper Redis already uses internally provides the sub-second
clock; nothing new was added for timing.

The byte-ceiling trigger handles the pathological case where a single
client hammers the server with tiny writes — it's possible to accumulate
many MB of unsynced data inside one window if writes are large. Bounding
the unsynced byte volume bounds **page cache exposure** independently of
elapsed wall-clock time.

The fsync itself is dispatched to a BIO thread via the existing
`aof_background_fsync()`, so the event loop is never blocked on disk —
this is the same property that makes `everysec` non-blocking, just at
50× finer granularity.

## Tradeoff analysis

**What improved:**
- Worst-case durability gap dropped 50× (1,000 ms → 20 ms) for a 6%
  throughput cost vs `everysec`.
- Throughput is 3.45× higher than `appendfsync always` and p50 latency is
  4.7× lower, while data exposure is bounded to a small, configurable
  number of milliseconds.
- fsyncs per 1k ops dropped 156× vs `always` (6.42 vs 1,000), reducing
  SSD write-amp and disk queue depth.

**What degraded:**
- Throughput is 5.8% lower than `everysec` (8,315 vs 8,824 ops/s) — the
  cost of fsyncing 50×/s instead of 1×/s.
- CPU per 1k ops rose ~9% over `everysec` (17.2 ms vs 15.8 ms) due to the
  extra BIO job dispatches.
- 250 fsyncs/s under load is a higher disk-syscall rate than the
  ~1/s of `everysec` — on cheap consumer SSDs without battery-backed
  caches, this can shorten device lifespan over years of operation.

**Latency vs durability:** The window is the lever. Operators can dial:
- `aof-groupcommit-window-ms 5` → 5 ms loss, ~200 fsyncs/s
- `aof-groupcommit-window-ms 50` → 50 ms loss, ~20 fsyncs/s

**Memory vs performance:** No measurable RSS difference vs `everysec`
under matching throughput. The new server fields cost ~40 bytes total per
process — irrelevant.

**Complexity vs benefit:** The patch is ~30 lines of C across 4 files,
preserves all existing fsync invariants, and reuses the BIO infrastructure.
The benefit is a real third option that previously didn't exist between
"~1s loss" and "~50% throughput drop."

## Efficiency verdict

`groupcommit` is **strictly better than `everysec`** for any workload where
sub-second durability matters and you can absorb a 5–10% throughput dip;
**strictly better than `always`** for any workload that tolerates 5–50 ms
of bounded loss in exchange for 3–4× more throughput.

It is *not* better than `everysec` if your workload is throughput-critical
*and* you don't care about the loss window — i.e. if you're already willing
to accept up to 1 s of loss, the extra fsyncs are pure overhead.

It is *not* better than `always` if your write rate is so low that
per-write fsync cost is negligible (e.g. a config server with a few
writes per minute), or if you genuinely need 0 ms loss for compliance
reasons.

## Limitations

- The fsync is still cooperative with the BIO thread — if the BIO queue
  is saturated by a long-running fsync (e.g. on a saturated disk), new
  windows can pile up. The existing `aof_delayed_fsync` counter still
  catches this.
- Configuration is process-wide. There is no per-key or per-command
  durability tier — every write goes through the same fsync schedule.
  (That was the explicit design choice over the prior sidecar
  experiment, which gave per-write tiering at the cost of being external
  to Redis.)
- The byte-trigger only counts unsynced bytes, not total dirty page-cache
  exposure across the AOF rewrite buffer.
- Windows below ~5 ms approach the BIO scheduler's wakeup jitter and
  start to behave more like `always` without the safety guarantee.

## How to reproduce

```bash
cd experiments/exp5_wal_sidecar
# Build modified redis (already done; binary at bin/redis-server-modified)
python3 src_v2/exp_crash_durability.py
python3 src_v2/exp_high_writeload.py
python3 src_v2/exp_resource_impact.py
# Results land in results_v2/*.json and logs in logs_v2/*.log
```
