# RDB TRACE Instrumentation

This is the foundation observability layer the rest of the project builds on. It is **not** an experiment in itself — it is the tooling that makes the experiments observable from inside Redis.

The patch adds structured trace events to the BGSAVE child's write path. Every event is timestamped and written to `/tmp/rdb_trace.log`. The patch never changes control flow, return values, or error handling — only adds observation points.

## What it covers

- Entry/exit of `rdbSaveInternal()` (the outermost file-open wrapper)
- Phase markers in `rdbSaveRio()` (header, AUX fields, modules, functions, per-DB loop, EOF, checksum)
- Per-DB and per-key events in `rdbSaveDb()` (DB entry, SELECTDB write, key save, key byte cost, progress every 1024 keys, cluster slot transitions, DB completion)

## Files modified

| File | Lines | Nature |
|---|---|---|
| `redis/src/rdb.c` | +~40 (function and macro) and ~20 TRACE call sites | Add `rdbTrace()` variadic logger, `TRACE_ENABLED` toggle, `TRACE()` macro. Insert TRACE calls in `rdbSaveInternal`, `rdbSaveRio`, `rdbSaveDb`. No headers modified. |

## Apply

```
cd redis/
git apply ../1-internals/trace-instrumentation/patch.diff
make -j4
```

To disable without recompile, edit `TRACE_ENABLED = 0` at top of rdb.c, then rebuild.

## Why this is foundation, not result

A trace log is not a finding. It only becomes useful as evidence inside a larger experiment. The Delta RDB and CoW throttling experiments reuse this TRACE infrastructure (and extend it) to produce verifiable in-band timings — they do not rely on `serverLog` or external profilers for the snapshot path.

## Known tradeoffs of this implementation

- `fopen`/`fclose` per call: slow under heavy logging. For experiments with high per-key event rates, switch to a persistent file descriptor or in-memory ring buffer.
- Not safe for production: globals, no log rotation, hardcoded path.
- `TRACE_ENABLED` is compiled-in, not runtime-configurable.

A production-quality version of this would use a ring buffer in shared memory and a separate reader. For an analysis project, the simple version is correct.

## Sample output

See `sample-trace.log` — a real capture from a 2-key BGSAVE.
