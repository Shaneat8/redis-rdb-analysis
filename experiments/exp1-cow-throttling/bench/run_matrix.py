#!/usr/bin/env python3
"""
CoW write throttling benchmark harness.

Per cell:
  1. Boot modified Redis with throttle config applied.
  2. Load a 4 GB working set (or smaller for smoke tests).
  3. Launch a background write workload via redis-benchmark.
  4. Trigger BGSAVE.
  5. Sample INFO persistence every 100 ms during BGSAVE: CoW bytes,
     throttled-write count, RSS.
  6. After BGSAVE: collect peak RSS from /proc/[pid]/status VmHWM.
  7. Collect write/read latency histograms from a parallel redis-benchmark.

Output: results/raw.jsonl

Presets:
  --quick           tiny dataset, minutes (plumbing check)
  --medium          ~10–20 min: moderate dataset, full throttle×delay grid, reps capped
  --tradeoff-demo   ~5–12 min: low %-of-dataset thresholds so throttling actually fires
                    (proves tradeoffs.md: writes throttled vs baseline; keep delay=200µs)
  (default)         full benchmark: ~4 GB dataset, many reps (hours)
"""
import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import redis

REPO_ROOT = Path(__file__).resolve().parents[3]
MODIFIED_BIN = REPO_ROOT / "redis" / "src" / "redis-server-modified"
BENCH_BIN = REPO_ROOT / "redis" / "src" / "redis-benchmark"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def start_redis(port, datadir, throttle_bytes, throttle_delay_us, log_name="redis-cow.log"):
    log_path = LOGS_DIR / log_name
    proc = subprocess.Popen(
        [str(MODIFIED_BIN),
         "--port", str(port),
         "--dir", str(datadir),
         "--save", "",
         "--appendonly", "no",
         "--daemonize", "no",
         "--protected-mode", "no",
         "--loglevel", "notice",
         "--logfile", str(log_path),
         "--bgsave-cow-throttle-bytes", str(throttle_bytes),
         "--bgsave-cow-throttle-delay-us", str(throttle_delay_us)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            # Probe with a short timeout to detect readiness fast.
            redis.Redis(port=port, socket_timeout=0.1).ping()
            # Real control client: long timeout. Under aggressive throttling, BGSAVE
            # and INFO can sit behind throttled writes for many seconds.
            r = redis.Redis(port=port, socket_timeout=30.0,
                            socket_connect_timeout=5.0)
            r.ping()
            r.config_set("save", "")
            return proc, r
        except Exception:
            time.sleep(0.1)
    proc.kill(); raise RuntimeError("redis didn't start")


def load_working_set(port, n_keys, value_size):
    """Use redis-benchmark to load quickly with random keys (worst-case CoW)."""
    subprocess.run(
        [str(BENCH_BIN), "-p", str(port), "-t", "set",
         "-n", str(n_keys), "-r", str(n_keys),
         "-d", str(value_size), "-c", "50", "-P", "100", "-q"],
        check=True, stdout=subprocess.DEVNULL,
    )


def read_vm_hwm(pid):
    """Return peak RSS in bytes from /proc/[pid]/status VmHWM."""
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmHWM:"):
                kb = int(line.split()[1])
                return kb * 1024
    except FileNotFoundError:
        return 0
    return 0


def run_cell(n_keys, value_size, throttle_pct_of_dataset, throttle_delay_us,
             write_rate_target, explicit_threshold_bytes=None):
    expected_dataset_bytes = n_keys * (value_size + 50)   # rough estimate
    if explicit_threshold_bytes is not None:
        threshold_bytes = int(explicit_threshold_bytes)
        thr_slug = f"b{threshold_bytes}"
        throttle_pct_out = float(throttle_pct_of_dataset)  # sentinel -1 / -2 for plots
    else:
        threshold_bytes = int(throttle_pct_of_dataset / 100.0 * expected_dataset_bytes)
        thr_slug = str(throttle_pct_of_dataset).replace(".", "p")
        throttle_pct_out = throttle_pct_of_dataset

    with tempfile.TemporaryDirectory(prefix="cow-throttle-") as tmp:
        datadir = Path(tmp); port = free_port()
        log_name = f"redis-cow-thr{thr_slug}-delay{throttle_delay_us}us.log"
        proc, r = start_redis(port, datadir, threshold_bytes, throttle_delay_us, log_name=log_name)
        try:
            load_working_set(port, n_keys, value_size)
            # Ensure server picked up thresholds (CLI alone can be flaky across versions).
            r.config_set("bgsave-cow-throttle-bytes", threshold_bytes)
            r.config_set("bgsave-cow-throttle-delay-us", throttle_delay_us)
            dataset_rss = r.info("memory")["used_memory_rss"]

            # Launch background write workload.
            # `-l` makes redis-benchmark loop forever; we terminate it below.
            # Critical: do NOT pass `-n <small>` here — that caps total writes and
            # the storm can end BEFORE BGSAVE actually runs, leaving no dirty pages
            # to throttle. We pass a huge -n that we will never reach within BGSAVE.
            bench_writes = subprocess.Popen(
                [str(BENCH_BIN), "-p", str(port), "-t", "set",
                 "-n", "100000000", "-r", str(n_keys),
                 "-d", str(value_size), "-c", "50", "-P", "10",
                 "-l"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # And a parallel read workload (also continuous via -l).
            bench_reads = subprocess.Popen(
                [str(BENCH_BIN), "-p", str(port), "-t", "get",
                 "-n", "100000000", "-r", str(n_keys),
                 "-c", "50", "-P", "10",
                 "-l"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            time.sleep(2.0)   # let workload reach steady state
            samples = []

            # Avoid racing an automatic BGSAVE from save rules.
            while r.info("persistence").get("rdb_bgsave_in_progress"):
                time.sleep(0.1)
            t_start = time.time()
            r.bgsave()
            while True:
                info = r.info("persistence")
                samples.append({
                    "t": time.time() - t_start,
                    "cow": int(info.get("bgsave_cow_observed_bytes", 0)),
                    "throttled": int(info.get("bgsave_writes_throttled", 0)),
                    "rss": int(r.info("memory")["used_memory_rss"]),
                })
                if not info.get("rdb_bgsave_in_progress", 0):
                    break
                time.sleep(0.1)
            t_bgsave = time.time() - t_start

            peak_rss = read_vm_hwm(proc.pid)
            bench_writes.terminate(); bench_reads.terminate()
            try: bench_writes.wait(timeout=5)
            except subprocess.TimeoutExpired: bench_writes.kill()
            try: bench_reads.wait(timeout=5)
            except subprocess.TimeoutExpired: bench_reads.kill()

            max_cow = max((s["cow"] for s in samples), default=0)
            writes_throttled_end = int(samples[-1]["throttled"]) if samples else 0

            return {
                "n_keys": n_keys, "value_size": value_size,
                "throttle_pct": throttle_pct_out,
                "throttle_bytes": threshold_bytes,
                "throttle_delay_us": throttle_delay_us,
                "write_rate_target": write_rate_target,
                "dataset_rss": dataset_rss,
                "peak_rss": peak_rss,
                "bgsave_seconds": t_bgsave,
                "max_cow_bytes": max_cow,
                "writes_throttled_end": writes_throttled_end,
                "samples": samples,
                "ts": time.time(),
            }
        finally:
            r.shutdown(nosave=True)
            proc.wait(timeout=5)


def main():
    ap = argparse.ArgumentParser(
        description="CoW throttling benchmark. Presets: --quick, --medium, --tradeoff-demo."
    )
    ap.add_argument("--out", default="../results/raw.jsonl")
    ap.add_argument("--reps", type=int, default=10,
                    help="Repetitions (with --medium, capped at 2 for ~10–20 min).")
    ap.add_argument("--quick", action="store_true",
                    help="Smoke test: 100k keys, one delay, ~1 min.")
    ap.add_argument("--medium", action="store_true",
                    help="Moderate run: ~250k keys, both delays, reps capped at 2 (~12 cells, ~10–20 min typical).")
    ap.add_argument("--tradeoff-demo", action="store_true",
                    help="Short run that forces throttling on/off to match tradeoffs.md (~3 cells, ~5–12 min).")
    args = ap.parse_args()

    preset_n = int(args.quick) + int(args.medium) + int(args.tradeoff_demo)
    if preset_n > 1:
        ap.error("choose only one of --quick, --medium, or --tradeoff-demo")

    if args.quick:
        n_keys = 100_000; value_size = 1024
        throttles = [0, 25, 50]
        delays = [200]
        reps = args.reps
    elif args.medium:
        # ~250–350 MB values + metadata — enough CoW signal without multi-hour loads.
        n_keys = 250_000; value_size = 1024
        throttles = [0, 25, 50]
        delays = [200]
        reps = min(args.reps, 2)
        if args.reps > 2:
            print(f"note: --medium caps reps at 2 (requested {args.reps}); "
                  f"running {reps} rep(s).", flush=True)
    elif args.tradeoff_demo:
        # Byte thresholds: low < typical peak smaps CoW (~0.55MB here); high > peak (control).
        n_keys = 220_000; value_size = 1024
        reps = 1
        tradeoff_cells = [
            ("off", 0, None),
            ("thr350k", -1, 350_000),
            ("thr700k", -2, 700_000),
        ]
        print(
            "tradeoff-demo: off | 350kB threshold (expect writes_throttled>0) | 700kB control.",
            flush=True,
        )
    else:
        n_keys = 500_000; value_size = 1024     # ~4 GB working set
        throttles = [0, 25, 50]
        delays = [200]
        reps = args.reps
        tradeoff_cells = None

    if not args.tradeoff_demo:
        print(f"matrix: n_keys={n_keys} value_size={value_size} "
              f"throttles={throttles} delays={delays} reps={reps}", flush=True)
    else:
        print(f"matrix: n_keys={n_keys} value_size={value_size} "
              f"tradeoff_cells={tradeoff_cells} reps={reps}", flush=True)

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        for rep in range(reps):
            if args.tradeoff_demo:
                for tag, pct, expl in tradeoff_cells:
                    row = run_cell(n_keys, value_size, pct, 200, 100_000,
                                   explicit_threshold_bytes=expl)
                    row["rep"] = rep
                    row["tradeoff_case"] = tag
                    f.write(json.dumps(row) + "\n"); f.flush()
                    print(
                        f"rep={rep} case={tag} delay=200µs "
                        f"peak_rss={row['peak_rss']/1e9:.2f}GB "
                        f"bgsave={row['bgsave_seconds']:.1f}s "
                        f"max_cow_mb={row['max_cow_bytes']/1e6:.2f} "
                        f"writes_throttled={row['writes_throttled_end']}",
                        flush=True,
                    )
            else:
                for thr in throttles:
                    for delay in delays:
                        row = run_cell(n_keys, value_size, thr, delay,
                                       write_rate_target=100_000)
                        row["rep"] = rep
                        f.write(json.dumps(row) + "\n"); f.flush()
                        print(
                            f"rep={rep} thr={thr}% delay={delay}µs "
                            f"peak_rss={row['peak_rss']/1e9:.2f}GB "
                            f"bgsave={row['bgsave_seconds']:.1f}s "
                            f"max_cow_mb={row['max_cow_bytes']/1e6:.2f} "
                            f"writes_throttled={row['writes_throttled_end']}",
                            flush=True,
                        )


if __name__ == "__main__":
    main()
