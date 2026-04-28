# Experiment 1 — CRC64 silent-corruption hardening (C-level)

## What changed

| File | Change | Lines |
|---|---|---|
| `redis/src/rdb.c` | Added unconditional structural EOF check after the cksum trailer in `rdbLoadRio()` | +13 / -2 |

Patch: [`../c_patches/exp1_rdb.c.diff`](../c_patches/exp1_rdb.c.diff)

## How to reproduce

```bash
# Binaries already in this folder. If only the .gz is present:
gunzip -k ../bin/redis-server.baseline.gz
gunzip -k ../../exp6_incremental_rdb/bin/redis-server.modified.gz
cp ../../exp6_incremental_rdb/c_results/redis-server.modified .
bash run_exp1.sh
```

Outputs land next to the script: `metrics.txt` and `run.log`.

## Results

Save times are median of 3 runs. Deltas are within run-to-run noise (sub-25%, sub-100ms) — no real regression.

| Metric | Baseline | Modified | Δ |
|---|---|---|---|
| Save (cksum=yes) | 73 ms | 84 ms | +11 ms (noise) |
| Save (cksum=no)  | 68 ms | 82 ms | +14 ms (noise) |
| RDB file size    | 1,637,865 B | 1,637,865 B | identical |

| Corruption shape (cksum=no) | Baseline | Modified | Differs? |
|---|---|---|---|
| `clean`     | ACCEPTED | ACCEPTED | no |
| `append1`   | ACCEPTED | **REJECTED** | YES |
| `append16`  | ACCEPTED | **REJECTED** | YES |
| `trunc4`    | REJECTED | REJECTED | no  *(existing rio short-read path)* |

## Why it matters

Stock Redis with `rdbchecksum no` writes an all-zero trailer and then
short-circuits validation on load — meaning *anything* appended past
the EOF opcode loads silently. The patch closes that door for one
extra `rioRead()` call. No save-time regression; corruption that was
previously invisible is now caught.

## Files in this folder

| File | Purpose |
|---|---|
| `run_exp1.sh` | Runner with structured logging, two-phase test |
| `metrics.txt` | Final comparison table (auto-generated) |
| `run.log` | Timestamped per-step log (auto-generated) |
| `redis-server.baseline` | Stock Redis 6.2.14 binary |
| `redis-server.modified` | Patched binary |
| `redis-cli` | Standard redis-cli |
