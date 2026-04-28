# Redis RDB & Durability — A Big Data Engineering Case Study

This project analyzes how Redis persists in-memory data to disk and what happens when those mechanisms are pushed to their limits. We trace the code, run controlled experiments, and ultimately design and implement a custom **Selective Group-Commit Durability Sidecar** that closes Redis's per-write durability gap without paying the full cost of `appendfsync always`.

The point of the project is not just "does Redis work?" — it's to use Redis as a microscope for the recurring patterns of **big data engineering**: copy-on-write, write amplification, fsync semantics, storage-encoding trade-offs, RPO/RTO, group commit, and idempotent recovery.

---

## System Studied

- **Software:** Redis 6.2.14 (bundled via `redislite`) — an in-memory key-value store
- **Features:** RDB snapshotting, AOF (append-only file) persistence, hash encoding (listpack vs hashtable), CRC64 integrity checking, fork-based child processes, and a custom durability sidecar built on top
- **Source-code references:** see `code-references/execution-trace.md` for the annotated `BGSAVE → rdbSaveBackground() → fork() → rdbSave()` path

---

## Repository Structure

```
redis-rdb-analysis/
├── README.md                  ← this file
├── report.md                  ← long-form report (all 5 experiments + SGC)
├── changes.md                 ← per-experiment changelog
├── code-references/           ← annotated function-by-function trace
├── experiments/
│   ├── exp1_crc64_toggle/         ← CRC64 on/off + corruption test
│   ├── exp2_listpack_sweep/       ← Listpack vs hashtable encoding
│   ├── exp3_writestorm_bgsave/    ← Write storm during BGSAVE (CoW)
│   ├── exp4_kill9_durability/     ← RDB-only vs AOF hybrid kill -9
│   └── exp5_wal_sidecar/          ← Selective Group-Commit Durability Sidecar
└── redis/                     ← Redis source (read-only reference)
```

Each experiment folder contains: `config/` (Redis configs used), `src/` (test harnesses), `logs/` (raw output), `results/` (metrics tables and JSON), `analysis.md` (deep-dive), and `summary.md` (TL;DR).

---

## The Five Experiments

| # | Name | What we changed | What we measured |
|---|------|-----------------|------------------|
| 1 | **CRC64 Checksum Toggle** | `rdbchecksum yes` vs `no`; flipped a single byte in `dump.rdb` | Save time, file bytes, load behavior on a corrupt file |
| 2 | **Listpack Threshold Sweep** | Swept `hash-max-listpack-entries` from 8 → 512 | Memory per hash, HSET latency, RDB size, OBJECT ENCODING |
| 3 | **Write Storm During BGSAVE** | Heavy parent writes overlapping `BGSAVE` | RSS spike, CoW bytes, `rdb_last_bgsave_time_sec` bloat |
| 4 | **Kill -9 + Durability Gap** | `kill -9` between snapshots, RDB-only vs AOF hybrid | Keys lost, recovery time, AOF replay behavior |
| 5 | **Selective Group-Commit Sidecar** | New library: critical writes go through batched WAL with `__sgc:applied_seq`; normal writes stay on AOF everysec | Worst-case loss window, throughput under burst, fsync amortization |

The first four are observational — we poke Redis and watch. The fifth is constructive — we build a small Python sidecar (`exp5_wal_sidecar/src/sgc_client.py`) that acts as a durability layer in front of Redis and demonstrably matches `appendfsync always`-grade safety for *critical* writes while keeping near-`everysec` throughput overall.

---

## Big Data Engineering Concepts Demonstrated

The experiments are not isolated curiosities — each one maps to a foundational concept that recurs across databases, storage engines, and stream processors. This section is the gist of what to take away.

### 1. Copy-on-Write (CoW) and fork-based snapshotting *(Exp 3)*

Redis's `BGSAVE` calls `fork()`. The OS clones the parent's page table without copying physical pages — both processes share the same RAM until one of them writes. Each parent write under load duplicates a page. That's why a 600 MB dataset under a write storm can balloon to 1.1 GB RSS during a snapshot.

This is the same mechanism Postgres uses for `pg_basebackup`-via-fork in some configurations, and the same trap that causes OOMs in Redis production deployments with high write rates. **Take-away:** "snapshot the whole thing in a child process" is elegant on paper but introduces a memory cost proportional to *write rate × snapshot duration*, not dataset size alone.

### 2. Storage encoding trade-offs — compact contiguous vs pointer-based *(Exp 2)*

Redis stores small hashes as a **listpack** (one contiguous byte buffer, no per-entry pointers, ~11 B overhead per field) and large hashes as a **hashtable** (each entry is a `dictEntry` with three pointers, plus SDS string headers). Same logical `HSET` command, two completely different physical layouts, ~2.6× memory difference for the same data.

This mirrors the **row-store vs columnar** decision in OLAP systems, the **B-tree-leaf-page vs overflow-page** decision in relational engines, and the **packed slice vs typed array** decision in columnar formats like Parquet. The conversion is one-way (listpack → hashtable, never back), which is the same shape as **immutable SSTable promotion** in LSM trees: once you've paid the cost of upgrading representation, downgrading isn't worth the bookkeeping.

### 3. Data integrity and in-band sentinels — CRC64 *(Exp 1)*

Redis checksums the entire RDB file with CRC64 as it streams writes through the `rio` abstraction (`rioGenericUpdateChecksum`). The cost is negligible because CRC64 is hardware-accelerated and dwarfed by disk I/O. With checksum disabled, the trailing 8 bytes are written as zero — and on load Redis interprets `cksum == 0` as "skip validation."

This is **in-band sentinel encoding**: a magic value embedded in the data stream signals "this region is unprotected" without requiring a separate metadata file. The same pattern shows up in TCP (zero checksum = "not used" in IPv4 UDP), Kafka (CRC32 per record batch), Parquet (per-column-chunk checksums), and ext4 (metadata checksum opt-in). **Take-away:** integrity verification is essentially free; silent corruption is the truly expensive failure mode.

### 4. Durability gap, RPO/RTO, and write-ahead logging *(Exp 4)*

RDB-only mode has a quantifiable durability gap: any writes since the last successful `BGSAVE` are lost on `kill -9`. The `save 60 10000` config makes this explicit — at 1000 ops/sec, BGSAVE fires every ~10 s and a crash inside that window loses ~10 000 writes.

This is the textbook **RPO (recovery point objective)** trade-off: how many seconds of data can you afford to lose? The fix — **AOF hybrid mode** — is structurally identical to the **LSM tree base + WAL tail** pattern used by RocksDB, Cassandra, and HBase: the AOF file starts with a full RDB snapshot (the "base") and appends incremental commands (the "tail"). On startup, Redis loads the base fast (binary deserialization) and replays the small tail. Recovery time (**RTO**) grows with tail size, not full dataset size.

### 5. Group commit, batched WAL, and selective durability tiering *(Exp 5)*

`appendfsync always` gives you zero data loss but pays one `fsync` per write — throughput drops by ~50 %, p50 latency multiplies by 5–10×. `appendfsync everysec` is fast but leaves up to a full second of writes on the table during a crash.

The **Selective Group-Commit Sidecar** built for Experiment 5 borrows two well-known patterns:

- **Group commit** — collect pending writes for ~5 ms, then issue a single `fsync` covering all of them. Postgres has this as `commit_delay`; InnoDB has `innodb_flush_log_at_trx_commit`; the original BSD-FFS journaling paper introduced the term in 1990. Our sidecar achieves ~10 records per fsync under 16-writer load, ~22× fewer fsyncs than `appendfsync always` for equivalent durability.
- **Selective durability tiering** — only writes the application explicitly marks as `critical` go through the WAL fast path. The other 70 % of traffic stays on the cheap `everysec` path. This is the same idea as Kafka's per-topic `acks=all` vs `acks=1`, DynamoDB's strong-vs-eventual reads, and Postgres's per-transaction `synchronous_commit = off`. **Durability is a per-write property, not a global setting.**

The sidecar uses a **monotonic sequence number** (`__sgc:applied_seq` stored in Redis) and a pipelined apply, making **recovery idempotent**: the replay loop reads the sidecar's WAL, skips any record whose sequence is ≤ the last applied seq, and applies the remainder atomically. This is the same `LSN` (log sequence number) pattern used by every serious database engine.

### 6. Write amplification and fsync semantics *(Exp 3, Exp 5)*

Every "one write" at the application layer can become many writes at the disk layer: the AOF write, the AOF fsync, the RDB child's serialized output, the page-cache flush, the filesystem journal commit. The write storm experiment exposes this in the parent's RSS; the SGC resource experiment quantifies bytes-to-disk per mode (`everysec` ≈ 595 KB, `always` ≈ 381 KB at lower throughput, `sgc` ≈ 723 KB including its own WAL).

This is the **write amplification factor** that dominates SSD wear-leveling, LSM compaction cost, and replication bandwidth in distributed databases. Understanding *which* writes are amplified and *why* is the core skill behind capacity planning for any persistent system.

### 7. Sequence-numbered idempotent recovery *(Exp 5)*

After a crash, the SGC's `replay()` reads the WAL, decodes each record's `(seq, key, value, op)` tuple, and only applies records with `seq > __sgc:applied_seq`. The applied-seq advance is bundled into the same Redis pipeline as the data writes, so **either both succeed or neither does** — atomic from the operator's standpoint.

This is the **exactly-once-effective** primitive that powers Kafka consumer offsets, Flink checkpoints, Spark structured streaming, and every event-sourced system. The principle: durability + monotonic sequencing + idempotent apply = safe replay across any number of failures.

---

## How to Reproduce

### Prerequisites

- Linux or WSL2
- Python 3.10+
- `pip install redislite redis` (no system Redis required — `redislite` bundles its own `redis-server`)

### Run an individual experiment

```bash
cd experiments/exp1_crc64_toggle && bash run.sh
cd experiments/exp2_listpack_sweep && bash sweep.sh
cd experiments/exp3_writestorm_bgsave && python3 src/measure_storm.py
cd experiments/exp4_kill9_durability && bash run_kill_test.sh
cd experiments/exp5_wal_sidecar && python3 src/run_burst.py --mode sgc
```

Each `results/` folder contains a `*_summary.json` with the captured metrics and a `metrics.txt` with the human-readable comparison tables.

### Read the analysis

- Start with `report.md` — top-level narrative tying all five experiments together
- For depth on any one experiment, read `experiments/expN_*/analysis.md`
- For the exact code lines we instrumented, see `code-references/execution-trace.md`

---

## Headline Findings

- **Snapshotting is not free.** A write storm during BGSAVE can nearly double process RSS — in production this is the #1 cause of Redis OOMs.
- **Encoding choice dominates memory cost.** A 2.6× memory difference for the same hash data, controlled by a single config integer.
- **Silent corruption is the expensive failure.** CRC64's CPU cost is unmeasurable; disabling it lets `dump.rdb` corrupt invisibly.
- **Durability is a per-write property, not a global setting.** The SGC sidecar matches `appendfsync always` safety for the 30 % of writes that need it, while the other 70 % keep `everysec` throughput.
- **Group commit pays for itself immediately.** ~10 records per fsync under load, ~22× fewer disk syncs than per-write fsync for the same durability guarantee.

---

## Design Insight

Redis is small enough to read end-to-end and instrument byte-by-byte, which makes it an unusually clean teaching example for storage-system fundamentals. The same trade-offs — CoW vs duplication, compact vs pointer-based encoding, full-snapshot vs incremental log, per-write fsync vs group commit, global durability vs per-record durability — show up in every database, every stream processor, and every distributed log we are likely to build or operate. Studying them at the scale of a 50 MB codebase is the fastest way to recognize them at the scale of a 50 GB production system.
