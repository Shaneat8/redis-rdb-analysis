# Experiment 4 — CoW Amplification by Workload Shape

**Type:** Baseline characterization (no Redis source changes)
**Status:** ✅ Has 30-trial results

---

## Question

When writes happen during BGSAVE, how does the **shape** of the write
workload affect copy-on-write memory amplification? Three workloads at the
same total write rate:

- **Random uniform** — every write hits a random key. Spreads page dirtying
  evenly.
- **Sequential** — writes cycle through keys 0, 1, 2, ... in order. Touches
  the same hot region repeatedly.
- **Zipfian (s=1.2)** — heavy tail. Most writes hit a small hot set, but
  there's still a long tail.

## Why it matters

Experiment 1 (CoW throttling) attacks the *symptom* — bounded RSS growth.
This experiment establishes the *root-cause shape*: which workloads are
worst-case for CoW. If random workloads amplify more than sequential ones,
operators know which traffic patterns to watch out for.

## What we do

Pure observation. No source modifications. The harness:

1. Boots stock Redis.
2. Loads a fixed dataset (a few hundred MB).
3. Launches one of the three workload patterns.
4. Triggers `BGSAVE`.
5. Reads `Private_Dirty` from `/proc/self/smaps_rollup` every 100 ms during BGSAVE.
6. Records the peak.

---

## How to run

```bash
cd bench

# Smoke
python3 run.py --quick

# Full — 3 workloads × 10 reps = 30 BGSAVEs
python3 run.py --reps 10
```

Output: `results/raw.jsonl` with one row per trial.

---

## Expected story

| Workload | Expected peak CoW |
|---|---|
| Sequential | Lowest — same pages get dirtied repeatedly |
| Zipfian | Middle — hot set + tail |
| Random uniform | Highest — every write touches a new page |

The ratio between random and sequential is the answer to "how much does
workload shape matter."

---

## Folder contents

```
exp4-cow-amplification/
├── README.md
├── bench/
│   └── run.py            ← harness with workload generators
├── results/
│   └── raw.jsonl         ← 30 trials (10 per workload)
└── logs/
    └── redis-s2-*.log    ← per-cell redis-server.log
```
