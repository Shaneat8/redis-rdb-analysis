# Experiment 4 — kill -9 Durability Gap: Analysis

## What changed

Two persistence configurations were tested under identical kill-and-recover
conditions:

- baseline:   `appendonly no`, single initial BGSAVE, then `save ""` to
              disable auto-snapshots.
- modified:   `appendonly yes` + `appendfsync everysec`, plus the same
              initial BGSAVE.

Both servers received a stream of synchronous SETs for ~3 seconds and were
then killed with SIGKILL — no chance for graceful shutdown, no buffered
write flushes, no clean fsync of in-flight data.

## Why it changed (Redis internals)

### What `kill -9` actually skips

A clean `SHUTDOWN` triggers `prepareForShutdown()`:
- `AOF_FSYNC_EVERYSEC`: forces a final synchronous flush of the AOF buffer
- if AOF off and `save_to_rdb_on_shutdown`: performs a final blocking SAVE
- closes file descriptors with proper fsync semantics

`SIGKILL` skips ALL of that. The process is killed mid-instruction; the
kernel reaps file descriptors but does NOT flush page-cache pages that
the application hadn't already fsynced. Anything sitting in:
- the Redis-side AOF write buffer
- the kernel page cache (pre-fsync)

is gone.

### Why RDB-only loses everything

Redis RDB persistence is "snapshot at points in time". Between snapshots,
zero durability is provided. The save schedule (`save 3600 1` style) is
the only built-in trigger; absent that, you'd need to BGSAVE manually.
We disabled the schedule (`save ""`) to make the gap explicit, and
SIGKILL removes the only fallback (the shutdown-hook BGSAVE). Result:
the durable record of the dataset is whatever was on disk at the last
explicit snapshot — in our case, an empty initial RDB.

This is the architectural reality: **RDB is a backup format, not a
durability primitive.** It is excellent for fast recovery from a known
good state, point-in-time backups, and cross-replica seeding. It is
not, by itself, a way to ensure recent writes survive a crash.

### Why AOF-everysec lost nothing on this trial

AOF runs through `feedAppendOnlyFile()` on every write. It:
1. Reconstructs the command in RESP format
2. Appends it to an in-process buffer
3. The buffer is flushed to the OS via `write()` on each event-loop
   tick (this puts data in the kernel page cache)
4. A background thread issues `fsync()` once per second to push the
   page cache to the device

`kill -9` loses anything not yet in step 4. The everysec mode's
*worst-case* loss is therefore the volume of writes between fsyncs,
i.e. up to 1 second of throughput. On this trial the kill happened
within ~50 ms of an fsync — visible in the AOF file mtime — so we
got 0 loss. A trial that happened to land just before an fsync would
have lost up to ~1 s × throughput ≈ 4,000 writes (here).

### Why the AOF cost dropped throughput by 54%

The writer dropped from 8,779 ops/s to 4,021 ops/s. On a tmpfs FS
the cost is not actually fsync — it's:
1. Per-command RESP re-encoding into the AOF buffer
2. The `write()` call into the page cache after each event-loop iter
3. Increased event-loop work per command (the AOF append step is
   done synchronously in the parent before reply)

On a real disk, the per-second fsync would also cost — but it's
done on a background thread so the parent's per-command cost stays
roughly constant. The cost we measured here is intrinsic to AOF
encoding and write-syscall pressure, independent of disk speed.

## Trade-offs

| Configuration                        | Throughput | Worst-case loss on kill -9 | Restart speed | Disk usage          |
|--------------------------------------|-----------|----------------------------|---------------|---------------------|
| `save ""`, no AOF                    | Highest   | EVERYTHING since last save | Fastest       | Smallest            |
| Default `save` schedule, no AOF      | Highest   | Up to save-interval window | Fastest       | Small               |
| `appendonly yes appendfsync no`      | Highest   | OS flush window (~30 s)    | Fast          | AOF grows           |
| `appendonly yes appendfsync everysec`| Medium    | Up to 1 s (default) ✅      | Slower (replay)| AOF grows fastest  |
| `appendonly yes appendfsync always`  | Lowest    | Single in-flight write     | Slower        | AOF grows fastest   |

`everysec` is the canonical "right answer" for most workloads: ~1 s
worst-case data loss for ~50% throughput cost. `always` provides per-
write durability but each SET pays a sync syscall — typical hit is
5–10× lower throughput on real storage.

## Hidden side effects

1. **AOF rewrite churn.** The AOF grows linearly. Redis triggers
   `BGREWRITEAOF` based on `auto-aof-rewrite-percentage`. The rewrite
   forks the process again — a second BGSAVE-shaped event with its
   own COW spike. We didn't trigger one in this short experiment but
   it is a regular operational reality.
2. **`no-appendfsync-on-rewrite`.** When this is `yes`, fsyncs are
   suspended during AOF rewrite to avoid I/O contention. This widens
   the worst-case loss window from 1 s to "duration of the rewrite",
   often tens of seconds. We left it `no` (the safe default).
3. **AOF replay can be slower than RDB load.** RDB load is a
   straight binary deserialisation. AOF replay is "execute every
   recorded command in order" — for our 12 k writes it took 14 ms;
   for billions of writes after months without a rewrite it can take
   minutes. Use `aof-use-rdb-preamble yes` (default in 6.0+) so the
   AOF starts with an RDB snapshot for fast bulk reload, followed by
   the per-command tail.
4. **Disk full = total halt.** AOF is *required* to advance for
   every write. If the AOF disk fills, Redis stops accepting writes.
   This is a feature for safety but a real on-call hazard.

## Match with expectations?

Yes. The classical Redis durability story reproduces cleanly:
- RDB-only is a backup mechanism, not a durability mechanism.
- AOF-everysec gives ~1 s worst-case loss for a moderate throughput
  cost.
- A graceful shutdown would have saved everything (the shutdown hook
  performs a final SAVE in RDB mode and a final fsync in AOF mode);
  `kill -9` removes that safety net specifically to expose the
  underlying durability properties.

## Bonus observation: kill -9 is a stronger test than power loss

On a real power loss, in-flight writes that the kernel has already
acknowledged might still be lost (depending on disk write-cache
behaviour). `kill -9` is *less* destructive in one way: anything
already in the kernel page cache survives, whereas power loss can
clear kernel write caches mid-flight. So:

  power_loss_loss ≥ kill_9_loss

For this reason, AOF-everysec is technically only "1 s worst case"
under power loss if the disk's write cache is honest about fsync
(many consumer SSDs lie). For real durability guarantees, pair AOF
with a disk that respects fsync, or use `appendfsync always`.
