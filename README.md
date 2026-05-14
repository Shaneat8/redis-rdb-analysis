# Redis RDB Snapshot Persistence Analysis

A systems-level study of how Redis serializes in-memory state to disk — analyzing snapshot internals, persistence throughput, memory amplification behavior, and fault tolerance under a stock Redis 6.2.14 build and a locally modified version with two architectural patches applied.

---

## Table of Contents

1. [Overview](#overview)
2. [Project Objectives](#project-objectives)
3. [System Under Study](#system-under-study)
4. [Project Structure](#project-structure)
5. [What We Built](#what-we-built)
6. [Experiments](#experiments)
7. [Reproducing the Experiments](#reproducing-the-experiments)
8. [Learning Outcomes](#learning-outcomes)
9. [Future Enhancements](#future-enhancements)
10. [References](#references)

---

## Overview

Redis is an in-memory data store that periodically checkpoints its entire dataset to disk via `BGSAVE` — a fork-based snapshot mechanism. The child process handles disk I/O while the parent continues serving requests, but this design introduces real system-level costs: memory overhead from copy-on-write amplification, write latency spikes during fork, and serialization throughput that scales with total dataset size rather than the volume of actual changes.

This project reverse engineers those costs, benchmarks them, and implements two source-level patches to address the two most significant bottlenecks.

---

## Project Objectives

- Trace the full `BGSAVE` serialization path at the source-code level
- Measure fork latency, CoW memory overhead, and snapshot I/O throughput
- Implement a write throttle to bound memory amplification during snapshots
- Implement incremental delta snapshots to reduce serialization overhead for low-churn workloads
- Analyze RDB file integrity and fault tolerance under single-byte corruption

---

## System Under Study

**Redis 6.2.14** — specifically the snapshot write path.

| File | Role |
|------|------|
| `rdb.c` | Core serialization and snapshot write path |
| `server.c` | Event loop, command dispatch, background job management |
| `db.c` | In-memory keyspace operations |
| `rio.c` | Buffered I/O abstraction layer |

---

## Project Structure

```
redis-rdb-analysis/
│
├── redis/                        # stock Redis 6.2.14 source
├── redis-modified/               # Redis with both patches applied
│
├── docs/                         # internals writeups and trace logs
│
├── experiments/
│   ├── exp1-cow-throttling/
│   ├── exp2-delta-rdb/
│   ├── exp3-fork-latency/
│   ├── exp4-cow-amplification/
│   └── exp5-corruption-analysis/
│
├── scripts/
│   └── tail_logs.sh
│
├── requirements.txt
└── README.md
```

---

## What We Built

Two source-level modifications were applied to Redis on top of standard trace instrumentation.

**Patch A — CoW Write Throttle**
Introduces backpressure on write commands when CoW memory overhead exceeds a configurable threshold during `BGSAVE`. Stock Redis exposes CoW metrics but applies no backpressure — this patch bounds memory amplification at the cost of reduced write throughput during snapshot windows.

**Patch B — Delta Snapshots**
Replaces O(total keys) full serialization with O(modified keys) incremental snapshots by maintaining per-database dirty and deleted key sets. Dramatically reduces snapshot I/O throughput requirements and serialization latency for low-churn workloads.

---

## Experiments

### Experiment 1 — CoW Write Throttling

**What it asks:** Can backpressure on write commands bound CoW memory amplification during `BGSAVE` without affecting read throughput?

**What we found:** Under a sustained write storm, stock Redis incurred 275 MB of CoW memory overhead on a 270 MB working set — nearly 2× RSS amplification. With the throttle active, peak memory overhead dropped to under 6 MB (44–55× reduction). Read throughput was unaffected throughout.

| Setting | Peak CoW Overhead | Write Commands Throttled |
|---|---:|---:|
| No throttle (stock) | 275.7 MB | 0 |
| Throttle 341 KB | 6.2 MB | ~995 |
| Throttle 683 KB | 5.0 MB | ~1,270 |

**Tradeoffs:**
- Write latency increases for all clients during the snapshot window — `usleep()` blocks the event loop, stalling concurrent requests
- CoW is sampled every 100 ms, so memory overhead can overshoot the threshold between polling intervals
- Linux only — depends on `/proc/self/smaps_rollup`

---

### Experiment 2 — Delta Snapshots

**What it asks:** Can incremental serialization reduce snapshot I/O throughput requirements and latency for low-churn workloads?

**What we found:** Stock Redis serialization throughput is constant — it always writes the full dataset regardless of churn, keeping save latency flat at ~280 ms. Delta snapshots scale serialization cost with actual write volume, reducing both payload size and save latency by orders of magnitude at low churn rates.

| Churn Rate | Full Snapshot | Delta Snapshot | Size Reduction | Stock Latency | Delta Latency |
|---:|---:|---:|---:|---:|---:|
| 0.1% | 33.0 MB | 1.1 KB | 29,403× | 281 ms | 8 ms |
| 5% | 33.1 MB | 53.7 KB | 602× | 290 ms | 14 ms |
| 50% | 33.0 MB | 533.9 KB | 60× | 256 ms | 34 ms |

Even at 50% churn, the delta payload is 60× smaller with an order-of-magnitude lower serialization latency.

**Tradeoffs:**
- Delta files are not independently loadable — recovery requires the base full snapshot plus the delta, increasing recovery complexity
- Per-key dirty tracking adds ~30 bytes of memory overhead per modified key
- Replication payloads always require full serialization — delta mode is incompatible with PSYNC full-resync
- At high churn rates, dirty-set bookkeeping overhead approaches and can exceed the I/O savings

---

### Experiment 3 — Fork Latency vs. Dataset Size

**What it asks:** How does `fork()` latency scale with resident set size (RSS), and does Transparent Huge Pages (THP) amplify that cost?

**What we measured:** Fork latency is captured via `INFO stats → latest_fork_usec` across varying dataset sizes with THP set to `always` and `never`. Results here are partial — full reproduction requires ≥ 32 GB RAM to cover larger dataset cells.

The established expectation is that fork latency scales linearly with RSS, since the kernel must duplicate page table entries proportional to address-space size. At production scale (50 GB+), this can introduce hundreds of milliseconds of client-visible latency per snapshot. Whether THP amplifies this on modern kernels is what this experiment is designed to confirm.

**Tradeoffs:**
- Fork latency is an irreducible serialization overhead — no application-level optimization can eliminate it
- Mitigations: reduce instance RSS, offload `BGSAVE` to a replica, or replace fork-based persistence with an alternative mechanism

---

### Experiment 4 — CoW Amplification by Workload Shape

**What it asks:** At the same aggregate write throughput during `BGSAVE`, how does write access pattern affect CoW memory overhead?

**What we measured:** Three workload patterns — random uniform, sequential, and Zipfian (s=1.2) — at identical write throughput, with CoW memory overhead sampled from `/proc/self/smaps_rollup` every 100 ms across 30 trials.

Random writes maximize page-level scatter, causing the highest CoW amplification since each write dirties a new memory page. Sequential writes concentrate I/O on a small hot region, minimizing page duplication. Zipfian workloads fall between the two. This characterizes which access patterns drive worst-case memory overhead and where the Exp 1 throttle provides the most benefit.

**Tradeoffs:**
- Write access pattern is application-driven — operators have limited ability to control it
- This experiment quantifies an externally imposed memory overhead risk; it does not propose a mitigation

---

### Experiment 5 — RDB Integrity Under Single-Byte Fault Injection

**What it asks:** When a single byte is corrupted at different structural positions in an RDB file, does Redis detect it or silently ingest a corrupt dataset?

**What we found:** Across 30 fault injection trials covering 8 structural sites, Redis detected every corruption — zero silent loads. Structural corruptions are caught by the parser before serialization completes. Payload-region corruptions pass the parser and are caught exclusively by the CRC64 integrity check at end-of-load.

| Corruption Site | Type | Caught By | Silent Load |
|---|---|---|:---:|
| Magic header | Structural | Parser | 0 |
| SELECTDB opcode | Structural | Parser | 0 |
| EOF opcode | Structural | Parser | 0 |
| RESIZEDB length | Mixed | Parser or CRC64 | 0 |
| Key string length | Mixed | Parser or CRC64 | 0 |
| AUX field | Payload | CRC64 only | 0 |
| Value payload | Payload | CRC64 only | 0 |
| CRC trailer | Checksum | CRC64 | 0 |

Structural sites fail fast at parse time. Payload-region sites pass structural validation and rely entirely on the CRC64 trailer for integrity enforcement.

**Tradeoffs:**
- CRC64 verification only executes if the parser reaches end-of-file — a structural abort bypasses it entirely
- Disabling CRC64 (`rdbchecksum no`) eliminates the integrity check for payload-region corruptions, creating a silent data ingestion risk with no detection overhead savings at load time

---

## Reproducing the Experiments

### 1. Clone the Repository

```bash
git clone https://github.com/Shaneat8/redis-rdb-analysis
cd redis-rdb-analysis
```

### 2. Install Dependencies

```bash
sudo apt update
sudo apt install -y build-essential pkg-config libssl-dev python3-pip tcl
pip3 install --user redis numpy matplotlib
```

### 3. Build Stock Redis

```bash
cd redis && make -j$(nproc) && cd ..
```

### 4. Build Modified Redis

```bash
cd redis-modified && make -j$(nproc) && cd ..
```

### 5. Run Experiments

| Experiment | Command |
|---|---|
| Exp 1 — CoW throttling | `cd experiments/exp1-cow-throttling/bench && python3 run_matrix.py --tradeoff-demo && python3 make_plots.py` |
| Exp 2 — Delta snapshots (quick) | `cd experiments/exp2-delta-rdb/bench && ./manual_test.sh` |
| Exp 2 — Delta snapshots (full) | `cd experiments/exp2-delta-rdb/bench && python3 run_churn_matrix.py --reps 2 --n-keys 50000 && python3 make_plots.py` |
| Exp 3 — Fork latency | `cd experiments/exp3-fork-latency/bench && sudo -E python3 run.py --reps 10` |
| Exp 4 — CoW amplification | `cd experiments/exp4-cow-amplification/bench && python3 run.py --reps 10` |
| Exp 5 — Corruption analysis | `cd experiments/exp5-corruption-analysis/bench && python3 run.py --reps 20` |

> **Note:** Exp 3 requires `sudo` to toggle Transparent Huge Pages. All experiments run on Linux only.

---

## Learning Outcomes

- How `BGSAVE` serialization works internally from command dispatch to disk I/O
- How fork-based persistence trades availability for snapshot consistency
- How CoW memory amplification scales with write throughput and access pattern
- Why O(total keys) serialization becomes a throughput bottleneck at high snapshot frequency
- How CRC64 integrity checking protects against silent data corruption at load time

---

## Future Enhancements

- Adaptive full-vs-delta decision in `rdbSaveBackground()` based on configurable dirty-key ratio threshold
- Delta-aware RDB loader to enable direct recovery from incremental snapshots
- Replace `usleep()` throttle with event-loop yield to avoid blocking concurrent request throughput
- Reproduce experiments at production-scale RSS (multi-GB) to characterize absolute latency and overhead numbers

---

## References

- [Redis Documentation — Persistence](https://redis.io/docs/management/persistence/)
- [Redis Source Code](https://github.com/redis/redis/tree/6.2)
- [Linux man page — fork(2)](https://man7.org/linux/man-pages/man2/fork.2.html)
- [RDB File Format Specification](https://github.com/sripathikrishnan/redis-rdb-tools/blob/master/docs/RDB_File_Format.textile)

---
