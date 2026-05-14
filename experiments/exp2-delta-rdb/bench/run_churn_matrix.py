#!/usr/bin/env python3
"""
Delta RDB churn sweep benchmark.

For each churn percentage, measure:
  - Full snapshot wall-time + file size (stock-equivalent path: SAVE)
  - Delta snapshot wall-time + file size (DEBUG INCRSAVE)

Single dataset size, varied churn. The headline plot is:
  delta-file-size / full-file-size vs churn %.

Writes results/churn_matrix.jsonl. Run make_plots.py afterward.
"""
import argparse
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import redis

REPO_ROOT = Path(__file__).resolve().parents[3]
REDIS_MOD = REPO_ROOT / "redis-modified" / "src" / "redis-server"
CLI = REPO_ROOT / "redis-modified" / "src" / "redis-cli"
BENCH = REPO_ROOT / "redis-modified" / "src" / "redis-benchmark"

if not REDIS_MOD.exists():
    sys.exit(f"missing {REDIS_MOD}")

RESULTS = Path(__file__).resolve().parent.parent / "results"
LOGS = Path(__file__).resolve().parent.parent / "logs"
RESULTS.mkdir(exist_ok=True); LOGS.mkdir(exist_ok=True)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def boot(datadir, log_name):
    port = free_port()
    proc = subprocess.Popen(
        [str(REDIS_MOD), "--port", str(port),
         "--daemonize", "no", "--save", "",
         "--dir", str(datadir),
         "--protected-mode", "no",
         "--loglevel", "notice",
         "--logfile", str(LOGS / log_name)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            r = redis.Redis(port=port, socket_timeout=30.0); r.ping()
            return proc, r, port
        except Exception: time.sleep(0.1)
    proc.kill(); raise RuntimeError("redis didn't start")


def load_dataset(port, n_keys, value_size):
    """Use redis-benchmark to load. -r N_KEYS uses random keys; actual distinct
    key count will be ~63% of n_keys due to birthday-collision."""
    subprocess.run(
        [str(BENCH), "-p", str(port), "-t", "set",
         "-n", str(n_keys), "-r", str(n_keys),
         "-d", str(value_size), "-c", "50", "-P", "100", "-q"],
        check=True, stdout=subprocess.DEVNULL)


def time_full_save(r):
    t0 = time.time()
    r.save()
    return time.time() - t0


def time_delta_save(r, filename):
    t0 = time.time()
    resp = r.execute_command("DEBUG", "INCRSAVE", filename)
    return time.time() - t0, resp


def churn_keys(r, dbsize_actual, churn_frac, delete_frac=0.1):
    """Apply churn: modify churn_frac of keys; of those, delete_frac become DELs."""
    target = max(1, int(dbsize_actual * churn_frac))
    n_del  = max(1, int(target * delete_frac)) if churn_frac > 0 else 0
    n_set  = target - n_del
    cmds = []
    rng = random.Random(42)
    sample = rng.sample(range(dbsize_actual), target)
    for i, k in enumerate(sample):
        if i < n_set:
            cmds.append(f"SET key:{k} mod_value_{k}_xxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        else:
            cmds.append(f"DEL key:{k}\n")
    # Use --pipe for speed
    proc = subprocess.Popen(
        [str(CLI), "-p", str(r.connection_pool.connection_kwargs["port"]),
         "--pipe"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    proc.stdin.write("".join(cmds).encode())
    proc.stdin.close()
    proc.wait(timeout=60)
    return n_set, n_del


def restart_for_clean_dirty(proc, r, datadir, log_name):
    """Shutdown and re-boot from the on-disk dump.rdb so dirty/deleted reset."""
    r.shutdown(nosave=True)
    proc.wait(timeout=10)
    time.sleep(0.5)
    return boot(datadir, log_name)


def run_cell(churn_frac, n_keys, value_size, rep, out_jsonl_path):
    log_a = f"delta-rdb-churn{int(churn_frac*1000)}-rep{rep}-load.log"
    log_b = f"delta-rdb-churn{int(churn_frac*1000)}-rep{rep}-after.log"
    with tempfile.TemporaryDirectory(prefix="delta-rdb-bench-") as tmp:
        datadir = Path(tmp)
        proc, r, port = boot(datadir, log_a)
        try:
            load_dataset(port, n_keys, value_size)
            dbsize_loaded = r.dbsize()

            # Full snapshot (baseline)
            full_ms = time_full_save(r) * 1000
            full_size = (datadir / "dump.rdb").stat().st_size

            # Restart to clear dirty sets (loads from dump.rdb cleanly)
            proc, r, port = restart_for_clean_dirty(proc, r, datadir, log_b)
            dbsize_after_restart = r.dbsize()
            # Sanity: dirty should be 0 after restart
            stats_before = r.execute_command("DEBUG", "INCRSTATS")
            dirty_before = int(stats_before[1])

            # Apply churn
            if churn_frac > 0:
                n_set, n_del = churn_keys(r, dbsize_after_restart, churn_frac)
            else:
                n_set = n_del = 0

            stats_after = r.execute_command("DEBUG", "INCRSTATS")
            dirty_after = int(stats_after[1])
            deleted_after = int(stats_after[3])

            # Delta snapshot
            delta_path = datadir / "delta.rdb"
            if churn_frac > 0:
                delta_ms, resp = time_delta_save(r, str(delta_path))
                delta_ms *= 1000
                delta_size = delta_path.stat().st_size if delta_path.exists() else 0
            else:
                delta_ms, delta_size, resp = 0, 0, "skipped"

            row = {
                "churn_frac": churn_frac,
                "n_keys_requested": n_keys,
                "value_size": value_size,
                "rep": rep,
                "dbsize_loaded": dbsize_loaded,
                "dbsize_after_restart": dbsize_after_restart,
                "dirty_before_churn": dirty_before,
                "dirty_after_churn": dirty_after,
                "deleted_after_churn": deleted_after,
                "set_applied": n_set, "del_applied": n_del,
                "full_save_ms": full_ms,
                "full_size_bytes": full_size,
                "delta_save_ms": delta_ms,
                "delta_size_bytes": delta_size,
                "size_ratio": (full_size / delta_size) if delta_size > 0 else None,
                "ts": time.time(),
            }
            with open(out_jsonl_path, "a") as f:
                f.write(json.dumps(row) + "\n")
            print(f"churn={churn_frac*100:>5.2f}% rep={rep}  "
                  f"full={full_size/1e6:6.2f}MB({full_ms:5.0f}ms)  "
                  f"delta={delta_size/1024:7.2f}KB({delta_ms:5.0f}ms)  "
                  f"ratio={row['size_ratio'] or 0:.1f}x")
        finally:
            try: r.shutdown(nosave=True)
            except Exception: pass
            proc.wait(timeout=10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RESULTS / "churn_matrix.jsonl"))
    ap.add_argument("--n-keys", type=int, default=50_000)
    ap.add_argument("--value-size", type=int, default=1024)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--churns", default="0.001,0.01,0.05,0.25,0.5",
                    help="Comma-separated churn fractions to sweep")
    args = ap.parse_args()

    churns = [float(x) for x in args.churns.split(",")]
    Path(args.out).unlink(missing_ok=True)
    print(f"output -> {args.out}")
    print(f"matrix: churns={churns} reps={args.reps} n_keys={args.n_keys}")

    for cf in churns:
        for rep in range(args.reps):
            try:
                run_cell(cf, args.n_keys, args.value_size, rep, args.out)
            except Exception as e:
                print(f"  cell failed: {e}")


if __name__ == "__main__":
    main()
