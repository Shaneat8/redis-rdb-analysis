"""
RESOURCE IMPACT measurement.
Reports for a fixed-duration concurrent burst:
  - throughput
  - bytes written to disk (AOF + WAL files)
  - fsync count and average fsync wall time (sgc only)
  - peak RSS for redis (best-effort via /proc/<pid>/status)
"""
import json, os, sys, time, subprocess, threading, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis
from sgc_client import SgcClient

mode = sys.argv[1]
port = int(sys.argv[2])
duration = float(sys.argv[3])
wal_path = sys.argv[4] if len(sys.argv) > 4 else ""
out_dir = sys.argv[5] if len(sys.argv) > 5 else "/tmp/exp5_results"
n_workers = int(sys.argv[6]) if len(sys.argv) > 6 else 16
data_dir = sys.argv[7] if len(sys.argv) > 7 else f"/tmp/exp5_{mode}"

env_pid = os.environ.get("REDIS_PID")
redis_pid = int(env_pid) if env_pid else None


def read_rss(pid):
    if pid is None:
        return 0
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return 0
    return 0


is_sgc = (mode == "sgc")
if is_sgc:
    sgc = SgcClient(wal_path=wal_path, port=port, flush_interval_ms=5.0, flush_bytes=64 * 1024)
    sgc.r.flushall()
else:
    redis.Redis(port=port).flushall()
time.sleep(0.2)

rss_max = 0
counter = {"i": 0}
counter_lock = threading.Lock()


def worker(worker_id):
    rng = random.Random(worker_id)
    if not is_sgc:
        r = redis.Redis(port=port)
    while time.time() < t_end:
        with counter_lock:
            i = counter["i"]; counter["i"] = i + 1
        is_crit = rng.random() < 0.30
        k = f"w:{i}"; v = "x" * 64
        if is_sgc:
            if is_crit: sgc.set_critical(k, v)
            else:       sgc.set(k, v)
        else:
            r.set(k, v)


def rss_sampler():
    global rss_max
    while time.time() < t_end:
        rss_max = max(rss_max, read_rss(redis_pid))
        time.sleep(0.05)


t_end = time.time() + duration
threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(n_workers)]
sampler_t = threading.Thread(target=rss_sampler, daemon=True)
for t in threads: t.start()
sampler_t.start()
for t in threads: t.join()
time.sleep(0.2)

aof_path = os.path.join(data_dir, "appendonly.aof")
aof_size = os.path.getsize(aof_path) if os.path.exists(aof_path) else 0
manifest_dir = os.path.join(data_dir, "appendonlydir")
if not aof_size and os.path.isdir(manifest_dir):
    for f in os.listdir(manifest_dir):
        if f.endswith(".aof"):
            aof_size += os.path.getsize(os.path.join(manifest_dir, f))
wal_size = os.path.getsize(wal_path) if wal_path and os.path.exists(wal_path) else 0

result = {
    "mode": mode,
    "duration_s": duration,
    "n_workers": n_workers,
    "total_writes": counter["i"],
    "throughput_ops_s": counter["i"] / duration,
    "redis_pid": redis_pid,
    "redis_peak_rss_kb": rss_max,
    "aof_size_bytes": aof_size,
    "wal_size_bytes": wal_size,
    "total_bytes_to_disk": aof_size + wal_size,
}
if is_sgc:
    result["sgc_stats"] = sgc.stats()
    sgc.close()
print(json.dumps(result, indent=2))
with open(f"{out_dir}/{mode}_resource_summary.json", "w") as f:
    json.dump(result, f, indent=2)
