# Experiment 1 — CRC64 Checksum Toggle: Analysis

## What changed

The single configuration directive `rdbchecksum` was flipped from `yes` (default)
to `no`. The same workload of 200,000 SET commands was run against both servers
and the serialised RDB file plus load behaviour were compared.

## Why it changed (Redis internals)

Redis writes RDB files through a streaming abstraction called `rio` (`rio.c`).
The `rio` layer wraps the underlying file descriptor and *also* keeps a running
state — including a 64-bit CRC. Every `rdbWriteRaw()` call funnels through
`rioWrite()`, which calls `rioGenericUpdateChecksum()` on the bytes written.

What `rdbchecksum no` actually does is *not* "skip the CRC computation" —
the chain is wired up unconditionally. What changes is at the very end of
`rdbSaveRio()`:

```c
cksum = rdb->cksum;          /* the running CRC64 in the rio struct */
if (server.rdb_checksum)
    memrev64ifbe(&cksum);
else
    cksum = 0;               /* <-- with rdbchecksum=no */
rioWrite(rdb, &cksum, 8);
```

So the trailer is forced to 8 zero bytes. On load, `rdbLoadRio()` reads the
trailer and only verifies if it's non-zero — a zero trailer is interpreted as
"this file was written without a checksum, skip the check".

That single design decision is what we observe: the file size is identical
(8 bytes of trailer either way), the save path takes effectively the same
time, and the load path either aborts hard or loads silently depending on
whether a trailer value is present to compare against.

## Trade-offs

| Dimension       | rdbchecksum=yes (default)              | rdbchecksum=no                           |
|-----------------|----------------------------------------|------------------------------------------|
| Save CPU        | Streaming CRC64 — a few ns per byte    | Same path, same cost (still computed!)   |
| Save throughput | Negligible diff in measurement         | Negligible diff in measurement           |
| File size       | Identical (24,678,103 bytes)           | Identical (24,678,103 bytes)             |
| Corruption det. | Detected at load → server refuses boot | NOT detected → silent data corruption    |
| Replication     | CRC carries through to replicas        | Replicas inherit unverified state        |
| Operational     | Loud failures, easy to alert on        | Quiet drift, very hard to root-cause     |

## Hidden side effects

1. **CRC computation is not actually disabled.** In Redis 6.2, the CRC64
   table-driven update sits inside `rioGenericUpdateChecksum()` and runs
   regardless of the `rdbchecksum` setting (only the *trailer write* is
   conditional). So you don't actually gain CPU back by turning it off.
   This is why our measured save-time delta is noise (+8.74% with a
   range >10% inside each group). To truly skip the work, you'd need
   to short-circuit `rioGenericUpdateChecksum` itself.

2. **A zero trailer is an in-band sentinel.** This means a future RDB
   produced *with* checksum on, but where the CRC happens to compute to
   exactly 0, would also be loaded without verification. The probability
   is 2⁻⁶⁴ but it's a real "magic value" footgun.

3. **Cross-version compatibility.** RDB load uses the trailer for
   integrity. Anyone shipping snapshots between machines (backup
   restore, replica seed) implicitly trusts that trailer. Disabling
   it removes the only end-to-end check that the file you trust is
   the file that was written — bit-rot, partial fsync, copy errors,
   storage-firmware bugs all become silent.

## Match with expectations?

Partially. The corruption result was as predicted: CRC ON → loud abort,
CRC OFF → silent acceptance. The *save-time* result was not — naive
intuition is "less work = faster save", but in practice the save path
is dominated by serialisation (object encoding, length-prefix writes,
buffered fwrite) and a streaming CRC over ~24 MB is barely measurable.

This matches the design intent of CRC64-on-by-default: the safety
benefit is concrete and the cost is, in practice, unobservable on
realistic workloads.
