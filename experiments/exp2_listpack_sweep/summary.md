# Experiment 2 — Listpack Threshold Sweep: Summary

## Final verdict

**Situational — and the Redis default (128) is correct for our
workload.** The sweep behaves as a step function: below the
threshold corresponding to your hashes' entry count, every hash
falls back to a real `dict` (hashtable encoding) and pays roughly
3× memory and RDB inflation; at or above that threshold, all
hashes pack as ziplists/listpacks and the gains are immediate.
There is *no* incremental benefit beyond the smallest threshold
that already covers your hashes.

## Is the idea efficient?

Yes — but only as a one-time tuning exercise. The directive is
free at runtime (a single integer comparison on each HSET).
Setting it correctly buys 3× memory savings and 2.5× smaller
RDB files. Setting it too low forces silent over-allocation.
Setting it too high is normally fine, except that very large
ziplists/listpacks make individual HSET operations more
expensive (memmove on insert) and increase BGSAVE child wall time
on a single key.

## Optimal value (this workload)

`hash-max-ziplist-entries 128` (Redis 6.2)
`hash-max-listpack-entries 128` (Redis 7.x)

…which is the project default. Our sweep reproduced the reasoning
behind the default rather than improving on it.

## When to deviate

- Hashes have ~ 200–400 entries with small values: raise to 256
  or 512. Memory/RDB stay flat (already packed), but you avoid
  promotion to hashtable when a few hashes drift over 128.
- Hashes have very large values (multi-KB): leave entries low,
  and pay attention to `hash-max-ziplist-value` instead — large
  values force conversion regardless of entry count and are a
  bigger lever for memory.
- Pathological case: many hashes hovering around the threshold
  with frequent HSET/HDEL. Each conversion is irreversible, so
  set the threshold *above* the steady-state size to avoid
  mid-life promotions you can't undo.

## Better alternative if you've over-tuned

If your hashes have permanently exceeded the threshold and you
care more about memory than HGET latency, the *real* memory lever
is **schema**: combine multiple small hashes into one larger
packed hash keyed by a synthetic prefix (e.g. `user:{shard}` with
`field = userid:attr`) so that one hash holds many users worth of
fields and stays cheaply addressable. This buys the same 3×
memory savings the encoding gives — at the application layer,
where you control it.
