#!/usr/bin/env python3
"""S2 — CoW amplification by workload shape.

Three workloads (random / sequential / Zipfian) at the same target write
rate during BGSAVE. Record bgsave_cow_observed_bytes time series and
peak. Uses the modified Redis build with throttling disabled.
"""
import argparse, json, os, random, socket, subprocess, tempfile, threading, time
from pathlib import Path
import numpy as np
import redis

REPO_ROOT = Path(__file__).resolve().parents[3]
MODIFIED_BIN = REPO_ROOT / "redis" / "src" / "redis-server-modified"
BENCH_BIN = REPO_ROOT / "redis" / "src" / "redis-benchmark"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def start_redis(port, datadir, log_name="redis-s2.log"):
    log_path = LOGS_DIR / log_name
    proc = subprocess.Popen(
        [str(MODIFIED_BIN), "--port", str(port), "--dir", str(datadir),
         "--save", "", "--appendonly", "no", "--daemonize", "no",
         "--protected-mode", "no", "--loglevel", "notice",
         "--logfile", str(log_path),
         "--bgsave-cow-throttle-bytes", "0"],   # throttle OFF (S2 is passive)
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            r = redis.Redis(port=port, socket_timeout=0.1); r.ping(); return proc, r
        except Exception: time.sleep(0.1)
    proc.kill(); raise RuntimeError("redis didn't start")


def workload(port, shape, n_keys, value_size, stop_event, target_ops_per_sec):
    """Background workload thread issuing SETs in the chosen pattern."""
    r = redis.Redis(port=port)
    val = ("x" * value_size).encode()
    target_per_batch = max(1, int(target_ops_per_sec / 100))   # 100 batches/s
    pipe = r.pipeline(transaction=False)
    rng = np.random.default_rng()
    seq_i = 0
    while not stop_event.is_set():
        t0 = time.time()
        for _ in range(target_per_batch):
            if shape == "random":
                k = rng.integers(0, n_keys)
            elif shape == "sequential":
                k = seq_i % n_keys; seq_i += 1
            elif shape == "zipfian":
                # zipf returns 1..inf; clip to [0, n_keys-1]
                k = int(rng.zipf(1.2) - 1)
                if k >= n_keys: k = k % n_keys
            pipe.set(f"k:{k}".encode(), val)
        try: pipe.execute()
        except Exception: pass
        elapsed = time.time() - t0
        if elapsed < 0.01:
            time.sleep(0.01 - elapsed)


def run_cell(shape, n_keys, value_size, target_rate, reps):
    rows = []
    for rep in range(reps):
        with tempfile.TemporaryDirectory(prefix="s2-") as tmp:
            port = free_port()
            proc, r = start_redis(port, Path(tmp),
                                   log_name=f"redis-s2-{shape}-rep{rep}.log")
            try:
                # Quick dataset load via redis-benchmark
                subprocess.run(
                    [str(BENCH_BIN), "-p", str(port), "-t", "set",
                     "-n", str(n_keys), "-r", str(n_keys),
                     "-d", str(value_size), "-c", "50", "-P", "100", "-q"],
                    check=True, stdout=subprocess.DEVNULL)
                time.sleep(2)
                stop = threading.Event()
                t = threading.Thread(target=workload,
                                     args=(port, shape, n_keys, value_size,
                                           stop, target_rate),
                                     daemon=True)
                t.start()
                time.sleep(2)
                samples = []
                t_start = time.time()
                r.bgsave()
                while True:
                    info = r.info("persistence")
                    samples.append({
                        "t": time.time() - t_start,
                        "cow": int(info.get("bgsave_cow_observed_bytes", 0)),
                    })
                    if not info.get("rdb_bgsave_in_progress", 0):
                        break
                    time.sleep(0.2)
                bgsave_s = time.time() - t_start
                stop.set()
                peak = max(s["cow"] for s in samples) if samples else 0
                rows.append({
                    "shape": shape, "n_keys": n_keys,
                    "value_size": value_size,
                    "target_rate": target_rate, "rep": rep,
                    "peak_cow_bytes": peak,
                    "bgsave_seconds": bgsave_s,
                    "samples": samples, "ts": time.time(),
                })
            finally:
                r.shutdown(nosave=True); proc.wait(timeout=10)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../results/raw.jsonl")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    if args.quick:
        n_keys = 100_000; value_size = 1024; rate = 10_000
    else:
        n_keys = 4_000_000; value_size = 1024; rate = 50_000

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        for shape in ["sequential", "zipfian", "random"]:
            for row in run_cell(shape, n_keys, value_size, rate, args.reps):
                f.write(json.dumps(row) + "\n"); f.flush()
                print(f"shape={shape} rep={row['rep']} "
                      f"peak_cow={row['peak_cow_bytes']/1e6:.0f}MB")


if __name__ == "__main__":
    main()
