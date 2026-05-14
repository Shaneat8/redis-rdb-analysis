# Redis RDB Snapshot Analysis

A systems-engineering study of how Redis writes its in-memory data to disk
(the `BGSAVE` snapshot path), what goes wrong under stress, and two targeted
C-level modifications that improve different parts of the picture.

This repo contains:

- **Stock Redis 6.2.14** (the baseline).
- A **modified Redis** with two architectural patches applied.
- **Five experiments** with benchmarks, plots, and logs.

---

## TL;DR

Stock Redis snapshots the entire keyspace on every BGSAVE, and the parent
process may double its RAM use during the save because of copy-on-write.
We attacked **both** of those problems with surgical patches.

| Problem | Stock behavior | Our modification | Result |
|---|---|---|---|
| BGSAVE rewrites whole dataset every time | 33 MB written for 0.1% change | Track dirty keys, write deltas | **29,400× smaller** snapshot files |
| Parent RSS can grow ~2× during BGSAVE under writes | 275 MB CoW amplification | Throttle writes when CoW > threshold | **44× less** memory amplification |

Neither modification is "free." Each is an honest tradeoff. See the experiment
folders for the full numbers and the costs you pay.

---

## The problem in 60 seconds

Redis is an in-memory database. To survive crashes it periodically writes a
**snapshot** of its memory to disk (called `BGSAVE`). To avoid blocking
clients during this multi-second operation, it uses Linux `fork()` to spin
off a **child process** that does the disk I/O while the parent keeps serving
traffic.

Two things break under load:

1. **The snapshot file is huge.** Even if only 1% of keys changed since the
   last snapshot, Redis serializes **all** of them — the whole dataset gets
   re-written every time. Disk I/O grows with total size, not change size.

2. **Memory can double.** When the parent modifies a memory page after the
   fork, Linux's copy-on-write mechanism duplicates that page so the child
   keeps its consistent view. Under a write storm during BGSAVE, the parent's
   resident memory can roughly **2×**. On a tight host, this causes OOM crashes.

Our two experiments tackle each problem independently.

---

## Workflow

```
            ┌─────────────────────────┐
            │   Stock Redis 6.2.14    │  ← redis/    (baseline)
            │   (control)             │
            └────────────┬────────────┘
                         │
              compare against
                         │
            ┌────────────▼────────────┐
            │   Modified Redis        │  ← redis-modified/
            │   ┌──────────────────┐  │
            │   │ Patch A:         │  │
            │   │ CoW throttle     │  │   (cuts memory amplification)
            │   ├──────────────────┤  │
            │   │ Patch B:         │  │
            │   │ Delta RDB        │  │   (cuts snapshot size & time)
            │   └──────────────────┘  │
            └────────────┬────────────┘
                         │
                runs all experiments
                         │
    ┌────────────────────┼────────────────────┐
    │                    │                    │
┌───▼────────┐   ┌───────▼──────┐   ┌─────────▼────────┐
│ Major (2)  │   │ Baseline (3) │   │ Plots + results  │
│            │   │              │   │                  │
│ exp1 CoW   │   │ exp3 fork    │   │ Each experiment  │
│ exp2 Delta │   │ exp4 CoW amp │   │ has its own      │
│            │   │ exp5 corrupt │   │ results.md +     │
└────────────┘   └──────────────┘   │ plots/*.png      │
                                    └──────────────────┘
```

---

## Folder structure

```
redis-rdb-analysis/
├── README.md                      ← you are here
├── HOW_TO_RUN.md                  ← build + reproduce
│
├── redis/                         ← stock Redis 6.2.14 source
├── redis-modified/                ← Redis with both patches applied
│
├── docs/                          ← background documentation
│   ├── snapshot-internals.md      ← how BGSAVE actually works
│   ├── execution-trace.md         ← deeper code walk
│   └── trace-instrumentation.md   ← foundation observability patch
│
├── experiments/
│   ├── exp1-cow-throttling/       ← MAJOR  — CoW write throttle
│   ├── exp2-delta-rdb/            ← MAJOR  — Incremental snapshots
│   ├── exp3-fork-latency/         ← BASELINE — fork() pause vs RSS
│   ├── exp4-cow-amplification/    ← BASELINE — CoW under workload shape
│   └── exp5-corruption-analysis/  ← BASELINE — fault injection on RDB files
│
└── scripts/
    └── tail_logs.sh               ← convenience: tail latest experiment log
```

Each `experiments/expN-*/` directory contains:

```
bench/      benchmark + plot scripts
patches/    the C diff(s) applied to redis-modified/
results/    raw.jsonl + summary.csv
plots/      headline PNGs
logs/       redis-server logs from real runs
README.md   what the experiment is and what we found
```

---

## Experiments at a glance

| # | Type | What it asks | What we built |
|---|---|---|---|
| 1 | **Major** | Can we bound CoW memory growth during BGSAVE? | Write throttle in `processCommand` reading `/proc/self/smaps_rollup` |
| 2 | **Major** | Can BGSAVE be O(modified keys) instead of O(total keys)? | Per-DB `dirty_keys`/`deleted_keys` shadow sets + `rdbSaveIncremental()` |
| 3 | Baseline | How does `fork()` pause scale with dataset RSS? | redis-benchmark + `latest_fork_usec` polling, with/without THP |
| 4 | Baseline | Does workload shape (random/sequential/Zipfian) change CoW? | Custom workload generator, `/proc/self/smaps_rollup` sampling |
| 5 | Baseline | Can corrupt RDB files load silently? | Bit-flip fault injection at 8 structural sites × 20 reps |

---

## Headline results

### Experiment 1 — CoW write throttling

When BGSAVE runs against a write storm, the stock parent's resident memory
explodes. Our throttle bounds it at the cost of slightly slower writes.

![Headline](experiments/exp1-cow-throttling/plots/headline_bar.png)

| Setting | Peak parent CoW | Writes delayed |
|---|---:|---:|
| Throttle OFF (stock) | **275 MB** | 0 |
| Throttle 341 KB | 6.2 MB (**44× less**) | ~1,000 |
| Throttle 683 KB | 5.0 MB (**55× less**) | ~1,270 |

Full details: [experiments/exp1-cow-throttling/README.md](experiments/exp1-cow-throttling/README.md)

### Experiment 2 — Delta RDB

When you snapshot frequently with low churn, stock Redis wastes huge amounts
of disk I/O writing keys that haven't changed. Delta RDB writes only what
changed.

![Headline](experiments/exp2-delta-rdb/plots/size_ratio_vs_churn.png)

| Churn % | Stock snapshot | Delta snapshot | Reduction |
|---:|---:|---:|---:|
| 0.1% | 33 MB | 1.1 KB | **29,400×** |
| 1% | 33 MB | 11 KB | 3,000× |
| 5% | 33 MB | 54 KB | 600× |
| 25% | 33 MB | 268 KB | 120× |
| 50% | 33 MB | 534 KB | 60× |

Full details: [experiments/exp2-delta-rdb/README.md](experiments/exp2-delta-rdb/README.md)

---

## Quick start

You need Linux (or WSL2), ~12 GB RAM, ~10 GB free disk.

```bash
# 1. Build both Redis trees
cd redis            && make -j$(nproc) && cd ..
cd redis-modified   && make -j$(nproc) && cd ..

# 2. Install Python dependencies
pip3 install --user redis numpy matplotlib

# 3. Run the smoke test that proves Delta RDB works (~30 sec)
cd experiments/exp2-delta-rdb/bench
./manual_test.sh
```

Expected output: 50,000 keys loaded → full snapshot ~33 MB → 1% churn applied
→ delta snapshot ~14 KB. Reduction shown at the bottom of the script.

For the full reproduction guide: [HOW_TO_RUN.md](HOW_TO_RUN.md).

---

## What this project is NOT

Honesty section. So you know what to expect:

- **Not production-ready code.** Both patches are deliberately minimal. The
  CoW throttle uses `usleep()` (blocks the event loop). Delta RDB files
  aren't loadable by stock Redis — they're measurement artifacts.
- **Not a Redis pull request.** The maintainers know about these problems
  and have chosen not to ship server-side fixes for them. Our experiments
  show *why* the tradeoffs exist, not that they should be merged upstream.
- **Not at production scale.** Datasets here are 50k–500k keys
  (tens to hundreds of MB), not the gigabytes you'd see in real deployments.
  The relative shape of the results should hold at scale; absolute numbers
  won't.

This repo IS:

- A working demonstration that small, surgical changes to a real C codebase
  produce measurable, defensible tradeoffs.
- A complete experimental setup you can rebuild, rerun, and extend.
- A starting point for understanding `fork()`, copy-on-write, and Redis
  snapshot internals at the source level.

---
