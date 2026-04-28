# Experiment 5 — Selective Group-Commit Durability Sidecar (Summary)

## TL;DR

A 250-line Python client library, `SgcClient`, gives Redis applications
a per-write `set_critical` API that is **as durable as `appendfsync
always`** (worst-case data loss window: 5 ms vs 1 s for everysec)
**at ~14% the throughput cost** (3,011 ops/s vs 3,482 ops/s baseline,
vs 1,715 ops/s for `always`).

It does this by:

1. **Selective durability** — only writes the application marks
   critical pay the durability tax; the other 70% stay on the fast
   `appendfsync everysec` path.
2. **Group commit** — concurrent critical writes share a single
   `fsync` (measured: ~10 records per fsync under 16 writers).
3. **Sequence-numbered idempotent replay** — recovery is bounded by
   WAL size, not Redis dataset size, and is safe to run multiple
   times.

## Headline numbers

| | everysec | always | **sgc** |
|---|---:|---:|---:|
| Throughput (16 writers) | 3,482 ops/s | 1,715 ops/s | **3,011 ops/s** |
| p50 normal write | 3.4 ms | 9.0 ms | **1.4 ms** |
| p50 critical write | 3.7 ms | 9.0 ms | 12.7 ms |
| Worst critical loss | up to ~1 s of writes | 0 | **0** |
| fsyncs in 3 s | ~3 | ~3,982 | **178** |

## Verdict

Better than Redis defaults for any workload that:

- has ≥ a few concurrent writers,
- can classify writes into "must not lose" vs "okay to lose", and
- doesn't depend on read-your-write semantics with sub-5 ms staleness
  (or is willing to read `__sgc:applied_seq` to gate reads).

That covers a large slice of real applications: payment systems,
session stores, queue producers, audit-log writers.

Not the right tool when writes are single-threaded (no batching),
when every write is critical (degrades to per-write fsync), or when
Redis-level read-after-write is required.

## Files

```
experiments/exp5_wal_sidecar/
├── config/
│   ├── everysec.conf       # AOF everysec — baseline
│   ├── always.conf         # AOF always — comparison ceiling
│   └── sgc.conf            # everysec server underneath SGC client
├── src/
│   ├── sgc_client.py       # The Selective Group-Commit Sidecar
│   ├── run_burst.py        # Workload A: throughput + latency
│   ├── durability_gap.py   # Workload B: critical-loss accounting
│   └── run_resource.py     # Workload C: bytes / fsyncs
├── results/
│   ├── metrics.txt         # All numerical results
│   └── diff.txt            # Behavioral differences across modes
├── analysis.md             # Deep dive
└── summary.md              # This file
```
