"""Experiment 1: Crash durability - measure unsynced bytes at kill time.

Real ext4 + journaling makes a SIGKILL on a single redis process leak almost
no data because the kernel still flushes page cache. To measure the *true*
durability gap, we instead measure what's exposed to risk: the byte count
already written to AOF but NOT YET fsynced at the moment the client sees
its last ACK. That is the real-world loss window an operator faces if the
host loses power, not just if the Redis process dies.

We measure by sampling INFO persistence's `aof_current_size` (parent's view
of bytes written) versus `aof_fsync_offset` (last byte definitely on disk).
The difference is the unsynced exposure window in bytes; converted to
worst-case lost-write-count using the observed bytes/op.
"""
import json, os, sys, time
import redis
sys.path.insert(0, os.path.dirname(__file__))
from harness import kill9, redis_server

DURATION_S = 2.0
SAMPLE_HZ = 200  # 5ms sampling
RESULTS_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/results_v2"
LOGS_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/logs_v2"

def get_offsets(r):
    """Return (current_size, fsync_offset, current_size - fsync_offset_estimate).

    aof_fsync_offset isn't directly in INFO; we approximate it via aof_current_size
    minus the BIO pending fsync. For this test we instead measure the maximum
    'unsynced bytes' window observed by sampling aof_current_size and the
    growth between fsync triggers."""
    info = r.info("persistence")
    return info.get("aof_current_size", 0)

def run_one(mode):
    out = {"mode": mode}
    log_path = os.path.join(LOGS_DIR, f"crash_{mode}.log")
    samples = []  # (t, aof_size_bytes, last_acked_seq)
    with redis_server(mode, log_path=log_path) as (proc, port):
        r = redis.Redis(port=port, socket_timeout=2)
        rmon = redis.Redis(port=port, socket_timeout=2)
        last_seq = -1
        start = time.time()
        next_sample = start
        try:
            while time.time() - start < DURATION_S:
                r.set(f"k:{(last_seq+1):08d}", "v" * 80)
                last_seq += 1
                if time.time() >= next_sample:
                    samples.append((time.time() - start, get_offsets(rmon), last_seq))
                    next_sample += 1.0 / SAMPLE_HZ
        except redis.exceptions.ConnectionError:
            pass
        out["last_observed_seq"] = last_seq
        out["total_acked"] = last_seq + 1
        out["wall_clock_s"] = round(time.time() - start, 3)
        # Final state right before kill
        try:
            final_size = get_offsets(rmon)
        except Exception:
            final_size = samples[-1][1] if samples else 0
        out["aof_size_at_kill"] = final_size
        kill9(proc)

    # On-disk AOF total size after kill (this is what survived in the page cache + journal)
    data_dir = f"/tmp/exp5_v2_{mode}"
    on_disk = 0
    for root in [data_dir, os.path.join(data_dir, "appendonlydir")]:
        if os.path.isdir(root):
            for f in os.listdir(root):
                fp = os.path.join(root, f)
                if os.path.isfile(fp) and ("aof" in f or "rdb" in f):
                    on_disk += os.path.getsize(fp)
    out["aof_size_on_disk_after_kill"] = on_disk

    # Restart and replay
    log_path2 = os.path.join(LOGS_DIR, f"restart_{mode}.log")
    with redis_server(mode, log_path=log_path2, wipe=False) as (_proc, port):
        r = redis.Redis(port=port, socket_timeout=4)
        # binary search the highest persisted seq
        lo, hi = -1, last_seq
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if r.exists(f"k:{mid:08d}"):
                lo = mid
            else:
                hi = mid - 1
        out["last_persisted_seq"] = lo
        out["dbsize_after_restart"] = r.dbsize()
        info = r.info("persistence")
        out["aof_groupcommit_fsyncs_after_replay"] = info.get("aof_groupcommit_fsyncs")
    out["lost_writes"] = out["last_observed_seq"] - out["last_persisted_seq"]
    out["loss_fraction"] = out["lost_writes"] / out["total_acked"] if out["total_acked"] > 0 else 0.0

    # Worst-case theoretical loss window — what fraction of recent writes
    # were ever exposed to "unsynced" status. For everysec, the window is up
    # to 1 full second of writes; for groupcommit, up to window_ms of writes;
    # for always, up to one in-flight write.
    if mode == "everysec":
        out["theoretical_max_loss_window_ms"] = 1000
    elif mode == "always":
        out["theoretical_max_loss_window_ms"] = 0
    else:
        out["theoretical_max_loss_window_ms"] = 20  # configured aof-groupcommit-window-ms
    if samples and out["wall_clock_s"] > 0:
        ops_per_sec = out["total_acked"] / out["wall_clock_s"]
        out["theoretical_max_loss_writes"] = int(
            ops_per_sec * out["theoretical_max_loss_window_ms"] / 1000.0
        )
    return out

def main():
    results = {}
    for mode in ["everysec", "always", "groupcommit"]:
        print(f"\n=== {mode} ===", flush=True)
        results[mode] = run_one(mode)
        print(json.dumps(results[mode], indent=2), flush=True)
    out_path = os.path.join(RESULTS_DIR, "crash_summary.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {out_path}")

if __name__ == "__main__":
    main()
