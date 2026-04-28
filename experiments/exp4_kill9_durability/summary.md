# Experiment 4 — kill -9 Durability Gap: Summary

## Final verdict

**Beneficial — and the result reproduces the canonical Redis
durability tradeoff exactly.** Adding AOF (`everysec`) to a stock
RDB-only configuration converted a *100 %* write-loss event into
a *0 %* write-loss event in this trial, with a worst-case bounded
to ~1 second of writes. The cost was roughly half the parent
throughput on this in-memory FS — a real but predictable penalty.

If your service ever needs to survive `kill -9`, OOM-kills, or
power loss with anything more recent than your last manual
snapshot, AOF is not optional. RDB by itself does not provide
durability — it provides recovery to a point-in-time, which is
a distinct property.

## Is the idea efficient?

Yes for AOF-everysec; no for RDB-only as a durability mechanism.

- AOF-everysec gives a precise, tunable durability bound (1 s)
  with bounded throughput cost (~50 % here, less on real storage
  because the fsync runs on a background thread). The fsync
  cadence is configurable; you can also disable it with
  `appendfsync no` and let the OS decide when to flush —
  giving up the bound but paying nothing on the hot path.
- RDB-only is *very* efficient at what it actually is: a
  backup format. As a durability mechanism it is unfit for
  purpose.

## Better approach (if the goal is bounded data loss)

The standard recipe, in increasing order of strictness:

1. **`appendonly yes` + `appendfsync everysec`** — ≤1 s loss.
   The default, the right answer for the majority of workloads.
2. **`aof-use-rdb-preamble yes`** (default in 6.0+) — keeps
   AOF replay fast on restart by prepending an RDB snapshot.
3. **`auto-aof-rewrite-percentage 100`** — control AOF growth
   without manual intervention.
4. **`no-appendfsync-on-rewrite no`** — keep fsync running
   during AOF rewrite even at the cost of some I/O contention,
   so the 1 s loss bound holds during rewrites too.
5. **Replication with `min-replicas-to-write 1`** — refuse
   writes the primary cannot replicate. This shifts durability
   from disk to network: even kill -9 of the primary can't
   lose acked writes if a replica acked them too. Combine
   with WAIT for stronger semantics.
6. **`appendfsync always`** — only for workloads that genuinely
   need per-write durability (financial ledgers, idempotency
   keys for non-replayable side-effects). Expect 5–10× throughput
   penalty on real storage.

## When RDB-only is actually appropriate

- Cache layer where the upstream truth lives in another store
  and re-warming from cold is acceptable.
- Test fixtures and ephemeral environments.
- Low-write-rate services where a daily snapshot is sufficient
  and the service can absorb hours of replay from upstream.

For anything where "the writes I just acked must not vanish",
RDB alone is the wrong tool, and `kill -9` is the test case
that proves it.
