"""
WRITE BURST workload (concurrent writers — the realistic scenario).
Modes:
  everysec    plain SET, AOF everysec
  always      plain SET, AOF always (per-write fsync server-side)
  sgc         SgcClient: 30% set_critical, 70% normal set
"""
import json, os, sys, time, random, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis
from sgc_client import SgcClient

mode = sys.argv[1]
port = int(sys.argv[2])
duration = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
wal_path = sys.argv[4] if len(sys.argv) > 4 else ""
acks_path = sys.argv[5] if len(sys.argv) > 5 else f"/tmp/exp5_results/{mode}_burst_acks.csv"
n_workers = int(sys.argv[6]) if len(sys.argv) > 6 else 16
crit_frac = 0.30

is_sgc = (mode == "sgc")
if is_sgc:
    sgc = SgcClient(wal_path=wal_path, port=port, flush_interval_ms=5.0, flush_bytes=64*1024)
    sgc.r.flushall()
else:
    redis.Redis(port=port).flushall()

barrier = threading.Barrier(n_workers + 1)
stop_at = [0.0]
counters = {"i": 0, "crit": 0}
counters_lock = threading.Lock()
lat_normal = []
lat_critical = []
lock_lat = threading.Lock()


def worker(worker_id):
    rng = random.Random(worker_id * 1000 + 1)
    if not is_sgc:
        r = redis.Redis(port=port)
    barrier.wait()
    local_normal = []
    local_critical = []
    local_c = 0
    while time.time() < stop_at[0]:
        with counters_lock:
            i = counters["i"]
            counters["i"] = i + 1
        is_crit = (rng.random() < crit_frac)
        k = f"w:{worker_id}:{i}"
        v = "x" * 64
        t0 = time.perf_counter()
        if is_sgc:
            if is_crit:
                sgc.set_critical(k, v)
            else:
                sgc.set(k, v)
        else:
            r.set(k, v)
        t1 = time.perf_counter()
        dt = t1 - t0
        if is_crit:
            local_critical.append(dt); local_c += 1
        else:
            local_normal.append(dt)
    with lock_lat:
        lat_normal.extend(local_normal)
        lat_critical.extend(local_critical)
        counters["crit"] += local_c


threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
for t in threads:
    t.start()
stop_at[0] = time.time() + duration + 0.2
barrier.wait()
t0_total = time.time()
stop_at[0] = t0_total + duration
for t in threads:
    t.join()
elapsed = time.time() - t0_total


def pct(xs, p):
    if not xs:
        return 0.0
    return sorted(xs)[max(0, min(len(xs) - 1, int(p * len(xs))))] * 1e6


summary = {
    "mode": mode,
    "n_workers": n_workers,
    "duration_s": elapsed,
    "total_writes": counters["i"],
    "critical_writes": counters["crit"],
    "throughput_ops_s": counters["i"] / elapsed,
    "lat_normal_us": {"p50": pct(lat_normal, 0.50), "p95": pct(lat_normal, 0.95), "p99": pct(lat_normal, 0.99)},
    "lat_critical_us": {"p50": pct(lat_critical, 0.50), "p95": pct(lat_critical, 0.95), "p99": pct(lat_critical, 0.99)},
}

if is_sgc:
    summary["sgc_stats"] = sgc.stats()
    sgc.close()

print(json.dumps(summary, indent=2))
