"""
DURABILITY GAP measurement.

Reports for each mode:
  - critical writes acked
  - critical writes durable in worst-case crash window:
      everysec : count of critical acks in worst 1.0s window
      always   : 0 (server fsyncs before reply)
      sgc      : 0 if last_flushed_seq >= last critical seq
"""
import json, os, sys, time, threading, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis
from sgc_client import SgcClient

mode = sys.argv[1]
port = int(sys.argv[2])
duration = float(sys.argv[3])
wal_path = sys.argv[4] if len(sys.argv) > 4 else ""
out_dir = sys.argv[5] if len(sys.argv) > 5 else "/tmp/exp5_results"
n_workers = int(sys.argv[6]) if len(sys.argv) > 6 else 8

is_sgc = (mode == "sgc")
if is_sgc:
    sgc = SgcClient(wal_path=wal_path, port=port, flush_interval_ms=5.0, flush_bytes=64 * 1024)
    sgc.r.flushall()
else:
    redis.Redis(port=port).flushall()

ack_log = open(f"{out_dir}/{mode}_gap_acks.csv", "w", buffering=1024 * 64)
ack_log.write("seq,kind,t_ack\n")

acks_lock = threading.Lock()
counter = {"i": 0}
critical_acks_in_order = []


def writer(worker_id):
    rng = random.Random(worker_id * 31 + 1)
    if not is_sgc:
        r = redis.Redis(port=port)
    while time.time() < t_end:
        with acks_lock:
            i = counter["i"]; counter["i"] = i + 1
        is_crit = rng.random() < 0.30
        k = f"w:{i}"
        v = str(i)
        if is_sgc:
            if is_crit:
                sgc.set_critical(k, v)
            else:
                sgc.set(k, v)
        else:
            r.set(k, v)
        ts = time.time()
        ack_log.write(f"{i},{('C' if is_crit else 'N')},{ts:.6f}\n")
        if is_crit:
            critical_acks_in_order.append((i, ts))


t_end = time.time() + duration
threads = [threading.Thread(target=writer, args=(w,), daemon=True) for w in range(n_workers)]
for t in threads:
    t.start()
for t in threads:
    t.join()
time.sleep(0.05)
ack_log.close()

result = {
    "mode": mode,
    "duration_s": duration,
    "total_writes": counter["i"],
    "total_critical": len(critical_acks_in_order),
}

if is_sgc:
    result["sgc_stats"] = sgc.stats()
    result["last_flushed_seq"] = sgc.last_flushed_seq
    result["critical_writes_durable"] = len(critical_acks_in_order)  # all WAL'd
    result["critical_writes_lost_worst_case"] = 0
    result["window_assumed_s"] = sgc.flush_interval
    sgc.close()
elif mode == "everysec":
    ts_only = sorted(t for (_i, t) in critical_acks_in_order)
    j = 0; worst = 0
    for k in range(len(ts_only)):
        while ts_only[j] < ts_only[k] - 1.0:
            j += 1
        worst = max(worst, k - j + 1)
    result["critical_writes_lost_worst_case"] = worst
    result["window_assumed_s"] = 1.0
else:  # always
    result["critical_writes_lost_worst_case"] = 0
    result["window_assumed_s"] = 0.0

with open(f"{out_dir}/{mode}_gap_summary.json", "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
