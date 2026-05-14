# Experiment 3 — Fork Latency vs RSS

**Type:** Baseline characterization (no Redis source changes)
**Status:** ⚠️ Partial — has results from one run

---

## Question

When Redis calls `fork()` to start BGSAVE, the parent process pauses while
the Linux kernel sets up the child's page tables. How long is that pause,
and how does it scale with the parent's resident set size (RSS)?

Also: does **Transparent Huge Pages (THP)** make this worse on big instances?
This is widely claimed in the Redis operations community, but rarely measured
with numbers.

## Why it matters

The pause from `fork()` is a hard latency floor for `BGSAVE`. On a 50 GB
Redis instance, this can be **50–500 ms** — visible client latency spikes
during snapshots. Stock Redis already measures this via
`INFO stats` → `latest_fork_usec`, but nobody charts it against RSS.

## What we do

Pure observation. No source modifications. The harness:

1. Boots stock Redis with a target dataset size.
2. Uses `redis-benchmark` to load the dataset.
3. Triggers `BGSAVE` and reads `latest_fork_usec` from `INFO stats`.
4. Repeats with THP set to `always` and `never`.
5. Plots fork pause vs RSS, two curves.

---

## How to run

```bash
cd bench

# Smoke run (smaller dataset sizes)
sudo -E python3 run.py --quick

# Full run — 3 sizes × 2 THP modes × 30 reps each = 180 BGSAVEs
sudo -E python3 run.py --reps 30
```

**Requires `sudo`** to toggle THP via `/sys/kernel/mm/transparent_hugepage/enabled`.
If you can't `sudo`, omit it — the harness will warn you and run with whatever
THP setting your kernel currently has.

Output: `results/raw.jsonl` with one row per trial.

---

## Current data (this machine)

Partial run-on results are in `results/raw.jsonl`. Full reproduction at
production scale needs ≥ 32 GB RAM (to cover the 16 GB cell). On a smaller
WSL2 environment, run with `--quick` and reduced size sweeps.

To plot what you have:

```python
import json, statistics
rows = [json.loads(l) for l in open('results/raw.jsonl')]
by_thp_rss = {}
for r in rows:
    key = (r.get('thp_mode_actual', 'unknown'), r['target_gb'])
    by_thp_rss.setdefault(key, []).append(r['fork_us'])
for key, vals in sorted(by_thp_rss.items()):
    print(f"{key}: mean={statistics.mean(vals):.0f}µs  reps={len(vals)}")
```

---

## Expected story

If THP **does** amplify fork latency at large RSS:
- The `THP=always` curve will sit above `THP=never` and the gap widens with RSS.

If THP **does not** amplify (modern kernels handle this better):
- Both curves overlap.

Either result is a publishable finding — the experiment is honest either way.

---

## Folder contents

```
exp3-fork-latency/
├── README.md
├── bench/
│   └── run.py           ← harness
├── results/
│   └── raw.jsonl
└── logs/                ← per-cell redis-server.log
```
