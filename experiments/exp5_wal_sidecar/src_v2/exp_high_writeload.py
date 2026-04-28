"""Experiment 2: High write load - burst traffic."""
import json, os, statistics, sys, threading, time
import redis
sys.path.insert(0, os.path.dirname(__file__))
from harness import redis_server

N_WORKERS = 16
DURATION_S = 5.0
VALUE = b"x" * 100
RESULTS_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/results_v2"
LOGS_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/logs_v2"

def worker(port, stop_at, latencies, count_box, idx):
    r = redis.Redis(port=port)
    n = 0
    while time.time() < stop_at:
        t0 = time.perf_counter()
        r.set(f"w{idx}:k{n}", VALUE)
        latencies.append((time.perf_counter() - t0) * 1000)
        n += 1
    count_box[0] = n

def sample_rss(pid, samples, stop_event):
    while not stop_event.is_set():
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        samples.append(int(line.split()[1]))
                        break
        except FileNotFoundError:
            return
        time.sleep(0.1)

def run_one(mode):
    out = {"mode": mode, "n_workers": N_WORKERS, "duration_s": DURATION_S}
    rss_samples = []
    stop_event = threading.Event()
    with redis_server(mode, log_path=os.path.join(LOGS_DIR, f"burst_{mode}.log")) as (proc, port):
        sampler = threading.Thread(target=sample_rss, args=(proc.pid, rss_samples, stop_event), daemon=True)
        sampler.start()
        redis.Redis(port=port).ping()
        latencies_per = [[] for _ in range(N_WORKERS)]
        counts = [[0] for _ in range(N_WORKERS)]
        stop_at = time.time() + DURATION_S
        threads = [threading.Thread(target=worker, args=(port, stop_at, latencies_per[i], counts[i], i)) for i in range(N_WORKERS)]
        t0 = time.time()
        for t in threads: t.start()
        for t in threads: t.join()
        elapsed = time.time() - t0
        stop_event.set()
        sampler.join(timeout=0.5)
        all_lat = [v for sub in latencies_per for v in sub]
        total_ops = sum(c[0] for c in counts)
        out["total_ops"] = total_ops
        out["throughput_ops_s"] = round(total_ops / elapsed, 1)
        out["latency_ms_p50"] = round(statistics.median(all_lat), 3)
        all_lat.sort()
        if all_lat:
            out["latency_ms_p95"] = round(all_lat[int(0.95 * len(all_lat))], 3)
            out["latency_ms_p99"] = round(all_lat[int(0.99 * len(all_lat))], 3)
            out["latency_ms_max"] = round(all_lat[-1], 3)
        out["rss_peak_kb"] = max(rss_samples) if rss_samples else None
        info = redis.Redis(port=port).info("persistence")
        out["aof_current_size"] = info.get("aof_current_size")
        out["aof_groupcommit_fsyncs"] = info.get("aof_groupcommit_fsyncs")
        out["aof_groupcommit_byte_triggers"] = info.get("aof_groupcommit_byte_triggers")
        out["aof_delayed_fsync"] = info.get("aof_delayed_fsync")
    return out

def main():
    results = {}
    for mode in ["everysec", "always", "groupcommit"]:
        print(f"\n=== {mode} ===", flush=True)
        results[mode] = run_one(mode)
        print(json.dumps(results[mode], indent=2), flush=True)
    out_path = os.path.join(RESULTS_DIR, "burst_summary.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {out_path}")

if __name__ == "__main__":
    main()
