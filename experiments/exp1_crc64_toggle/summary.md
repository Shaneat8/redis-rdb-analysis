# Experiment 1 — CRC64 Checksum Toggle: Summary

## Final verdict

**Not useful** as a performance optimisation. Disabling `rdbchecksum`
removes Redis' only end-to-end RDB integrity check while delivering
no measurable save-time benefit on a realistic 200k-key workload
(median delta within run-to-run noise; range >10 % inside each
group). It is a meaningful safety regression for negligible upside.

## Is the idea efficient?

No. The CRC64 cost is dominated by the rest of the save path
(object serialisation, length-prefix writes, fwrite buffering,
fsync). On modern CPUs, table-driven CRC64 over 24 MB is in the
low single-digit milliseconds — invisible next to the ~300 ms total
SAVE wall-clock time. Worse, in 6.2 the running CRC is computed
*regardless* of the setting; only the trailer write is conditional.

## When it can make sense (situational)

- Throwaway workloads where the RDB will never be reloaded: e.g. a
  test fixture you immediately discard, or a benchmark harness that
  uses BGSAVE only as a "fork progress" probe.
- Pre-7.x deployments where every byte of CPU on the parent or save
  child is measurably contended (very rare in practice; usually the
  fork itself is the cost, not CRC).

## Better alternative

Leave `rdbchecksum yes` (the default) and *also* wrap RDB files
with an external integrity check the moment they leave the host:

1. SHA-256 the RDB file post-save and ship hash + file together to
   backup storage; verify on restore. This catches storage-layer
   corruption that would slip past Redis' own CRC because RDB
   verification only runs at server startup.
2. If save-time CPU truly matters, switch to `appendonly` (AOF) with
   `rdb-save-incremental-fsync yes` and use BGSAVE less frequently;
   AOF's per-write append cost amortises better than periodic full
   serialisation.
3. Don't disable the only check that survives a power cut.

CRC64 in the RDB trailer is the cheapest insurance Redis offers.
Turning it off pays nothing and risks silent data corruption.
