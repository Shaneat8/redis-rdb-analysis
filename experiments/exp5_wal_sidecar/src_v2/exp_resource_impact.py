"""Experiment 3: Resource impact - CPU, disk I/O, memory footprint."""
import json, os, sys, threading, time
import redis
sys.path.insert(0, os.path.dirname(__file__))
from harness import redis_server

N_WORKERS = 8
DURATION_S = 5.0
VALUE = b"y" * 100
RESULTS_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/results_v2"
LOGS_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/logs_v2"

def get_cpu(pid):
    try:
        s = open(f"/proc/{pid}/stat").read()
        rparen = s.rfind(")")
        rest = s[rparen + 2:].split()
        return int(rest[11]), int(rest[12])
    except FileNotFoundError:
        return 0, 0

def worker(port, stop_at, count_box, idx):
    r = redis.Redis(port=port)
    n = 0
    while time.time() < stop_at:
        r.set(f"r{idx}:k{n}", VALUE)
        n += 1
    count_box[0] = n

def aof_total(data_dir):
    total = 0
    for root in [data_dir, os.path.join(data_dir, "appendonlydir")]:
        if os.path.isdir(root):
            for f in os.listdir(root):
                fp = os.path.join(root, f)
                if os.path.isfile(fp) and ("aof" in f or "rdb" in f):
                    total += os.path.getsize(fp)
    return total

def run_one(mode):
    out = {"mode": mode, "n_workers": N_WORKERS, "duration_s": DURATION_S}
    data_dir = f"/tmp/exp5_v2_{mode}"
    with redis_server(mode, log_path=os.path.join(LOGS_DIR, f"resource_{mode}.log")) as (proc, port):
        r = redis.Redis(port=port)
        r.ping()
        ut0, st0 = get_cpu(proc.pid)
        aof_b0 = aof_total(data_dir)
        counts = [[0] for _ in range(N_WORKERS)]
        stop_at = time.time() + DURATION_S
        threads = [threading.Thread(target=worker, args=(port, stop_at, counts[i], i)) for i in range(N_WORKERS)]
        t0 = time.time()
        for t in threads: t.start()
        for t in threads: t.join()
        elapsed = time.time() - t0
        ut1, st1 = get_cpu(proc.pid)
        clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        out["total_ops"] = sum(c[0] for c in counts)
        out["throughput_ops_s"] = round(out["total_ops"] / elapsed, 1)
        out["cpu_user_s"] = round((ut1 - ut0) / clk, 3)
        out["cpu_sys_s"] = round((st1 - st0) / clk, 3)
        out["cpu_total_s"] = round(out["cpu_user_s"] + out["cpu_sys_s"], 3)
        out["cpu_per_kop_ms"] = round(out["cpu_total_s"] * 1000 / (out["total_ops"] / 1000), 3) if out["total_ops"] else None
        info = r.info("persistence")
        time.sleep(0.1)
        aof_b1 = aof_total(data_dir)
        out["aof_bytes_written"] = aof_b1 - aof_b0
        out["bytes_per_op"] = round(out["aof_bytes_written"] / out["total_ops"], 2) if out["total_ops"] else None
        with open(f"/proc/{proc.pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss_kb_final"] = int(line.split()[1])
                    break
        out["aof_groupcommit_fsyncs"] = info.get("aof_groupcommit_fsyncs")
        out["aof_groupcommit_byte_triggers"] = info.get("aof_groupcommit_byte_triggers")
        out["aof_delayed_fsync"] = info.get("aof_delayed_fsync")
        if mode == "everysec":
            out["estimated_fsyncs"] = max(1, int(elapsed))
        elif mode == "always":
            out["estimated_fsyncs"] = out["total_ops"]
        else:
            out["estimated_fsyncs"] = info.get("aof_groupcommit_fsyncs")
        if out["total_ops"]:
            out["fsyncs_per_kop"] = round(out["estimated_fsyncs"] * 1000 / out["total_ops"], 2)
    return out

def main():
    results = {}
    for mode in ["everysec", "always", "groupcommit"]:
        print(f"\n=== {mode} ===", flush=True)
        results[mode] = run_one(mode)
        print(json.dumps(results[mode], indent=2), flush=True)
    out_path = os.path.join(RESULTS_DIR, "resource_summary.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {out_path}")

if __name__ == "__main__":
    main()
