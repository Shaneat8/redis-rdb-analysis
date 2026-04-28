# Summary — In-tree Adaptive Group-Commit AOF (Exp 5 v2)

## TL;DR

Added a fourth `appendfsync` mode to Redis 6.2.14 — `groupcommit` — that
replaces the second-resolution timer of `everysec` with a millisecond-
window adaptive trigger backed by the existing BIO fsync thread.

```c
// new mode in src/server.h
#define AOF_FSYNC_GROUPCOMMIT 3

// configurable knobs in src/config.c
aof-groupcommit-window-ms     1..1000   default 20
aof-groupcommit-max-bytes     0..LLONG_MAX  default 65536
```

## Headline numbers (16 concurrent writers, 5 s, 100 B values)

| | `everysec` | `always` | **`groupcommit`** |
|---|---|---|---|
| Throughput | 8,824 ops/s | 2,407 ops/s | **8,315 ops/s** |
| p50 latency | 1.44 ms | 7.01 ms | **1.50 ms** |
| Worst-case loss window | 1,000 ms | 0 ms | **20 ms** |
| fsyncs/sec | ~1 | ~2,400 | 50 |

**50× tighter durability than `everysec` for a 6% throughput cost;
3.45× faster than `always` for a bounded 20 ms loss penalty.**

## Where the win comes from

Single change to `flushAppendOnlyFile()`'s try_fsync block: schedule a
background fsync when EITHER the configured millisecond window has
elapsed since the last fsync, OR the unsynced byte volume exceeds the
ceiling. The fsync still runs in the BIO thread — the event loop is
never blocked. The new `INFO persistence` counters
`aof_groupcommit_fsyncs` and `aof_groupcommit_byte_triggers` make the
behavior auditable.

## When to use

- **Use `groupcommit`** when sub-second durability matters and you have
  the disk headroom for 50–200 fsyncs/sec.
- **Stick with `everysec`** when up-to-1-second loss is acceptable and
  every percent of throughput counts.
- **Stick with `always`** when zero loss is required (e.g. financial
  compliance, the AOF is the only record of truth).
