# How to Run This Project

Step-by-step reproduction guide. Follow in order.

---

## 0. Prerequisites

You need:

- **Linux or WSL2 Ubuntu** (Linux kernel ≥ 4.14 for `/proc/self/smaps_rollup`)
- **At least 12 GB RAM** allocated to the OS (8 GB minimum for smaller experiments)
- **~10 GB free disk space**
- **Build tools and Python**

Install everything in one go:

```bash
sudo apt update
sudo apt install -y build-essential pkg-config libssl-dev python3-pip tcl
pip3 install --user redis numpy matplotlib
```

Verify:

```bash
gcc --version | head -1
python3 -c "import redis, numpy, matplotlib; print('ok')"
free -h     # confirm RAM
```

WSL2 users: if `free -h` shows less than 8 GB, edit `%USERPROFILE%\.wslconfig`
on Windows:

```ini
[wsl2]
memory=12GB
processors=8
swap=8GB
```

Then in PowerShell: `wsl --shutdown`. Reopen Ubuntu.

---

## 1. Build both Redis binaries

```bash
cd redis
make -j$(nproc)
ls -la src/redis-server     # stock binary
cd ..

cd redis-modified
make -j$(nproc)
ls -la src/redis-server     # modified binary (both patches applied)
cd ..
```

Verify the modified build has the new INFO fields:

```bash
./redis-modified/src/redis-server --port 16380 --daemonize yes --save ""
./redis-modified/src/redis-cli -p 16380 INFO persistence | grep bgsave_cow
./redis-modified/src/redis-cli -p 16380 SHUTDOWN NOSAVE
```

You should see four `bgsave_cow_*` lines. If they're missing, the patches
didn't compile in — try `cd redis-modified && make distclean && make -j$(nproc)`.

The stock build should NOT print those lines (it's the control).

---

## 2. Reproduce experiment 1 — CoW write throttling

```bash
cd experiments/exp1-cow-throttling/bench

# Smoke test (~1 min)
python3 run_matrix.py --tradeoff-demo

# Aggregate + plot
python3 make_plots.py

# View results
cat ../results/summary.csv
ls ../plots/
```

Expected outcome: a row with throttle OFF showing ~275 MB CoW (varies by host),
and two rows with throttle armed showing single-digit MB CoW.

Open the plots from Windows:

```bash
explorer.exe "$(wslpath -w ../plots/headline_bar.png)"
```

---

## 3. Reproduce experiment 2 — Delta RDB

### Quick proof (~30 seconds):

```bash
cd experiments/exp2-delta-rdb/bench
./manual_test.sh
```

This loads 50,000 keys, applies 1% churn, and prints the size ratio at the
bottom (expect ~2000× reduction).

### Full churn sweep (~5 minutes):

```bash
python3 run_churn_matrix.py --reps 2 --n-keys 50000
python3 make_plots.py
cat ../results/summary.csv
```

Five churn levels are tested: 0.1%, 1%, 5%, 25%, 50%. The reduction shrinks
as churn rises — that's the architectural story.

---

## 4. Reproduce baseline experiments (optional)

Each baseline runs against stock Redis only:

```bash
# Experiment 3 — fork latency vs RSS (needs sudo for THP toggle)
cd experiments/exp3-fork-latency/bench
sudo -E python3 run.py --reps 10

# Experiment 4 — CoW amplification by workload shape
cd ../../exp4-cow-amplification/bench
python3 run.py --reps 5

# Experiment 5 — RDB corruption fault injection (~15 minutes)
cd ../../exp5-corruption-analysis/bench
python3 run.py --reps 20
```

Results land in each experiment's `results/raw.jsonl`.

---

## 5. Watching live experiment logs

While any benchmark is running, in a second terminal:

```bash
./scripts/tail_logs.sh exp1   # exp1, exp2, exp3, exp4, exp5
```

It tails the latest redis-server log from that experiment's `logs/` folder.

---

## 6. Cleaning up between runs

If a benchmark hangs or you want to start fresh:

```bash
pkill -f redis-server 2>/dev/null
pkill -f redis-benchmark 2>/dev/null
rm -f /tmp/dump.rdb
```

Wipe an experiment's existing results:

```bash
rm -f experiments/exp1-cow-throttling/results/raw.jsonl
rm -f experiments/exp1-cow-throttling/logs/*.log
rm -f experiments/exp1-cow-throttling/plots/*.png
```

Then re-run the steps above.

---

## 7. Common problems

| Problem | Fix |
|---|---|
| `make` fails on jemalloc | `cd redis/deps && make hiredis lua linenoise jemalloc -j$(nproc) && cd .. && make -j$(nproc)` |
| `pip3` not found | `sudo apt install python3-pip` |
| Redis exits with "Memory overcommit must be enabled" warning | `echo 'vm.overcommit_memory = 1' | sudo tee -a /etc/sysctl.conf && sudo sysctl vm.overcommit_memory=1` |
| `bgsave_cow_observed_bytes` always 0 in modified Redis | Kernel < 4.14, no `smaps_rollup`. Check `cat /proc/self/smaps_rollup`. |
| `manual_test.sh` says "redis-server not found" | Run `cd redis-modified && make -j$(nproc)` first. |
| Benchmark hangs | `pkill -f redis-server` and check the latest log under `experiments/expN-*/logs/`. |

---

## 8. What "done" looks like

When everything works:

- `experiments/exp1-cow-throttling/results/summary.csv` has rows for OFF / 25% / 50% throttle.
- `experiments/exp2-delta-rdb/results/summary.csv` has rows for each churn level.
- Both experiments' `plots/` directories contain 3 PNGs each.
- Each experiment's `results.md` has the headline table populated.

If you got that far, the project reproduces.
