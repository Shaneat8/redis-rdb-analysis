# Experiment 3 — Write Storm During BGSAVE: Analysis

## What changed

Two configurations were compared while a write storm overlapped a BGSAVE:

- baseline:    `rdb-save-incremental-fsync no` — the RDB child writes
               the entire ~100 MB RDB to the kernel page cache and
               issues a single `fsync()` at the end.
- modified:    `rdb-save-incremental-fsync yes` (the default since 6.0)
               + `lazyfree-lazy-expire yes`. The child calls
               `rioFdsetWrite` → `rioFdsetUpdateChecksum` and, every
               4 MB written, performs a `sync_file_range(...,
               SYNC_FILE_RANGE_WRITE)` so dirty pages are pushed to
               the device incrementally instead of all at once.

The *parent* keeps serving the storm (200k SET on new keys `sk:*`)
through both runs.

## Why it changed (Redis internals)

### The fork() and COW path

`BGSAVE` calls `rdbSaveBackground()` → `redisFork()` → `fork()`. The
child inherits a copy-on-write snapshot of the parent's heap. Any page
the parent then *writes* (any SET that mutates an existing dict entry,
allocator metadata, or causes a dict bucket-array resize) triggers a
page fault and the kernel duplicates that page so parent and child
diverge.

Crucially, the storm writes here are to **new keys** (`sk:*`). Those
allocations land in pages the snapshot never saw, so they don't
trigger COW. This is why `rdb_last_cow_size` is only 4.46 MB — the
COW we paid is just for dict resizes and allocator bookkeeping. If
the storm had been *overwriting* existing keys (`k:*`), COW would
have been many tens of megabytes.

This is the single most important hidden detail of BGSAVE behaviour:
**COW cost is proportional to the overlap between snapshot pages and
parent-write pages, not to the volume of writes.**

### Why incremental fsync exists

Without it, the child writes 100 MB to the page cache cheaply, then
calls `fsync()` once at the end. That single fsync triggers a flood
of dirty page writeback on the underlying device. On the same FS,
the parent's writes (page cache eviction pressure, journal contention)
get caught behind that flush. Result: a long latency spike at the
*end* of BGSAVE.

With incremental fsync, the child issues `sync_file_range` every
4 MB, so dirty pages drain continuously. The peak queue depth on the
device is much lower, and the final `fsync()` finds little to flush.

### Why the result is the wrong sign here

In a real production environment with rotating disks, large datasets
(GB-scale RDBs), and a parent doing real work on the same filesystem,
incremental fsync wins decisively. In our sandbox:

1. The RDB is only ~100 MB, written to a tmpfs-backed mount in <1.3 s.
   The "flood of writeback at the end" that incremental fsync exists
   to mitigate doesn't really happen: the device drains a 100 MB
   flush in tens of milliseconds.
2. `sync_file_range` is itself a syscall that contests page-cache
   locks. With incremental fsync ON, the child issues ~25 of those
   during the BGSAVE window, each one briefly contending with the
   parent's page cache activity. On a fast in-memory FS that
   per-call overhead actually *exceeds* the writeback flood it's
   trying to amortise.
3. The lazyfree change in modified.conf had no effect — there were
   no expirations or large key deletions during the storm.

So we measured the regime in which the tuning's overhead exceeds its
benefit. p99 went from 16.8 ms → 21.5 ms (+28%). That is a real
result for tmpfs / in-memory FS but **does not generalise to disk-backed
production**, where the same toggle yields the opposite outcome.

## Trade-offs

| Lever                              | Pro                                   | Con                                       |
|-----------------------------------|---------------------------------------|-------------------------------------------|
| `rdb-save-incremental-fsync yes`  | Smooth disk pressure on real devices, | Per-4MB syscall overhead; can hurt on tmpfs|
|                                   | avoids tail-latency spike at end of   | or very fast NVMe under tiny RDBs.        |
|                                   | BGSAVE                                |                                           |
| `lazyfree-lazy-expire yes`        | Big DEL/expiry won't block parent     | None unless you do many synchronous DELs  |
|                                   | event loop                            |                                           |
| `save ""` during storms           | Avoids cascade of auto-snapshots      | You need an external scheduler            |
| THP off (`/sys/kernel/mm/...`)    | Reduces COW page granularity from 2MB | Requires root; not testable in this sandbox|
|                                   | back to 4KB → smaller COW spikes      |                                           |

## Hidden side effects

1. **COW-as-RSS illusion.** `rdb_last_cow_size` is only the COW
   delta. The total RSS during BGSAVE momentarily includes both
   parent and child's RSS, so peak host RSS can briefly approach
   2× the steady-state. That's the real OOM hazard, and it has
   nothing to do with our incremental-fsync toggle.
2. **`vm.overcommit_memory` warning.** Both runs logged the
   classic Redis startup warning. Without overcommit=1, the
   `fork()` in BGSAVE can fail with ENOMEM under low memory even
   though the actual COW will be tiny — Linux pre-checks committed
   memory pessimistically.
3. **Storm against new keys understates COW cost.** A storm against
   *existing* keys (overwriting `k:*` instead of allocating `sk:*`)
   would have produced a much larger COW number. The choice of
   target key namespace matters more for BGSAVE peak memory than
   most operators realise.
4. **Storm throughput dropped 13%** with incremental fsync on. That
   is the clearest harm signal in this run, and likely comes from
   syscall contention rather than disk pressure.

## Match with expectations?

Mixed. We confirmed the structural facts (BGSAVE forks, COW size
correlates with parent overwrite of snapshot pages, RDB content
unchanged by fsync mode). We did *not* reproduce the canonical
"incremental fsync improves tail latency" result, because the
sandbox FS is too fast for the tuning to pay off. This is itself
useful — it's a reminder that this lever is workload- and
device-dependent and benchmarking on the wrong FS will mislead.
