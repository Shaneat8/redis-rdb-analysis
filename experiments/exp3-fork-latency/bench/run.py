#!/usr/bin/env python3
"""S1 — fork latency vs RSS, THP on/off.

Boot stock Redis, load to target RSS, run BGSAVE 30x per cell, capture
latest_fork_usec from INFO stats. Emits results/raw.jsonl.

Requires root (or write access to /sys/kernel/mm/transparent_hugepage/enabled)
to toggle THP. If you can't toggle THP, run with --thp-fixed and record the
current setting manually.
"""
import argparse, json, os, socket, subprocess, tempfile, time
from pathlib import Path
import redis

REPO_ROOT = Path(__file__).resolve().parents[3]
STOCK_BIN = REPO_ROOT / "redis" / "src" / "redis-server"
BENCH_BIN = REPO_ROOT / "redis" / "src" / "redis-benchmark"
THP_PATH = "/sys/kernel/mm/transparent_hugepage/enabled"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def set_thp(mode):
    """mode: 'always', 'madvise', or 'never'. Returns True if set."""
    try:
        with open(THP_PATH, "w") as f:
            f.write(mode)
        return True
    except (PermissionError, FileNotFoundError):
        return False


def read_thp():
    try:
        return open(THP_PATH).read().strip()
    except Exception:
        return "unknown"


LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def start_redis(port, datadir, log_name="redis-s1.log"):
    log_path = LOGS_DIR / log_name
    proc = subprocess.Popen(
        [str(STOCK_BIN), "--port", str(port), "--dir", str(datadir),
         "--save", "", "--appendonly", "no", "--daemonize", "no",
         "--protected-mode", "no", "--loglevel", "notice",
         "--logfile", str(log_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            r = redis.Redis(port=port, socket_timeout=0.1); r.ping(); return proc, r
        except Exception: time.sleep(0.1)
    proc.kill(); raise RuntimeError("redis didn't start")


def load_to_target(port, target_bytes, value_size):
    """Load via redis-benchmark until RSS reaches target_bytes."""
    n_estimate = int(target_bytes / (value_size + 50))
    subprocess.run(
        [str(BENCH_BIN), "-p", str(port), "-t", "set",
         "-n", str(n_estimate), "-r", str(n_estimate),
         "-d", str(value_size), "-c", "50", "-P", "100", "-q"],
        check=True, stdout=subprocess.DEVNULL)


def trigger_bgsave_get_fork_us(r):
    """BGSAVE, wait for done, return latest_fork_usec."""
    last = r.info("persistence")["rdb_last_save_time"]
    r.bgsave()
    while True:
        info = r.info("persistence")
        if not info.get("rdb_bgsave_in_progress", 0) and \
           info["rdb_last_save_time"] != last:
            break
        time.sleep(0.02)
    return r.info("stats")["latest_fork_usec"]


def run_cell(target_gb, thp_mode, reps):
    if not set_thp(thp_mode):
        print(f"WARN: could not set THP={thp_mode}, current={read_thp()}")
    actual_thp = read_thp()
    rows = []
    with tempfile.TemporaryDirectory(prefix="s1-") as tmp:
        port = free_port()
        proc, r = start_redis(port, Path(tmp),
                               log_name=f"redis-s1-{target_gb}gb-thp{thp_mode}.log")
        try:
            target_bytes = int(target_gb * 1e9)
            load_to_target(port, target_bytes, value_size=1024)
            time.sleep(5)        # let jemalloc settle
            rss = r.info("memory")["used_memory_rss"]
            for rep in range(reps):
                fork_us = trigger_bgsave_get_fork_us(r)
                rows.append({
                    "target_gb": target_gb, "rss_bytes": rss,
                    "thp_mode_requested": thp_mode,
                    "thp_mode_actual": actual_thp,
                    "rep": rep, "fork_us": fork_us,
                    "ts": time.time(),
                })
                time.sleep(2)    # let child fully reap
        finally:
            r.shutdown(nosave=True); proc.wait(timeout=10)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../results/raw.jsonl")
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    sizes_gb = [0.5, 2] if args.quick else [1, 2, 4]
    thp_modes = ["never", "always"]

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        for gb in sizes_gb:
            for thp in thp_modes:
                for row in run_cell(gb, thp, args.reps):
                    f.write(json.dumps(row) + "\n"); f.flush()


if __name__ == "__main__":
    main()
