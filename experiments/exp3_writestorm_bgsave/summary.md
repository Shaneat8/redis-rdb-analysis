# Experiment 3 — Write Storm During BGSAVE: Summary

## Final verdict

**Situational.** `rdb-save-incremental-fsync yes` is the right
default on production hardware (rotational or SATA SSD with shared
filesystem) but in our sandbox (tmpfs-backed FS, ~100 MB RDB) it
*hurt* parent latency and storm throughput because the per-4MB
`sync_file_range` syscalls were more expensive than the flush
flood they exist to mitigate.

The structural takeaways are independent of the toggle:

1. BGSAVE COW cost depends on **page overlap**, not write volume.
   Storming new keys → tiny COW (4.5 MB). Storming existing keys
   → COW proportional to the unique pages touched.
2. Total host RSS during BGSAVE can briefly approach 2× the
   parent's RSS (parent + child snapshot). Plan for it.
3. RDB content is identical regardless of fsync mode — the toggle
   is purely a writeback-scheduling lever.

## Is the idea efficient (the toggle)?

Yes on real disks, no on this FS. The lever has *zero* effect on
correctness and very small effect on the BGSAVE wall-clock itself
(both runs: 1.0–1.3 s child time). Its job is to *spread* I/O
pressure, which only matters when there's pressure to spread.

## When this tuning is worth applying

- RDB > 1 GB.
- Parent doing significant disk-aware work (AOF rewrite,
  replication backlog flush, log rotation on the same FS).
- Backing storage is rotational or shared cloud volume (gp2/gp3
  with throughput credits).
- p99/p999 latency is the SLO that matters.

## When NOT to apply it (or when to combine with other levers)

- RDB-only deployments on tmpfs/local NVMe with sub-second
  BGSAVE: the syscall overhead can dominate. Leave at default
  but don't expect a win.
- Workloads that overwrite hot keys during BGSAVE: the dominant
  cost is COW, not fsync; tune `save` cadence so BGSAVE doesn't
  fire during predictable write spikes.

## Better approach (if the goal is "no parent-latency spike during
snapshot")

A sequence of complementary levers, ordered by impact:

1. **`save ""` + cron BGSAVE off-peak.** Stop letting BGSAVE fire
   reactively to write rate. Snapshot when the parent is quiet.
2. **Replicate to a read replica and snapshot there.** The replica
   bears all BGSAVE cost; the primary never forks for snapshots.
3. **`appendonly yes` + `appendfsync everysec`.** Replaces full
   periodic snapshots with continuous incremental durability;
   parent latency cost is amortised across every write rather
   than concentrated at fork time.
4. **Disable transparent huge pages on the host** so COW operates
   at 4 KB granularity and peak COW memory is bounded.
5. *Then* and only then tune `rdb-save-incremental-fsync` for the
   specific FS.
