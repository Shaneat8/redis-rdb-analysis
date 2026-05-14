# Background Documentation

These docs explain how Redis snapshots work internally. Read them if you
want to understand **why** the experiments make the choices they do.
They are **not** required to run the experiments.

| File | What it covers |
|---|---|
| [snapshot-internals.md](snapshot-internals.md) | High-level walkthrough of the BGSAVE write path: `bgsaveCommand` → `redisFork` → `rdbSave` → `rdbSaveRio` → `rdbSaveDb`. Line-anchored to real Redis 6.2.14 source. |
| [execution-trace.md](execution-trace.md) | Deeper code walk with annotated snippets. Originally written from a live instrumented run. |
| [trace-instrumentation.md](trace-instrumentation.md) | Documents an experimental observability patch that adds `TRACE` macros to `rdb.c` for printing structured events during BGSAVE. Optional foundation tooling — none of the final experiments rely on it. |
| [trace-instrumentation.patch](trace-instrumentation.patch) | The actual diff for the trace instrumentation, if you want to apply it. |
| [trace-instrumentation-sample.log](trace-instrumentation-sample.log) | Sample output captured during a real run. |

---

## Suggested reading order

1. Start with **`snapshot-internals.md`** for a quick mental model of the
   stages BGSAVE goes through.
2. Then `experiments/exp1-cow-throttling/README.md` and
   `experiments/exp2-delta-rdb/README.md` will make immediate sense — they
   both reference functions and code paths described in the internals doc.
3. The remaining files (`execution-trace.md`, the trace instrumentation
   docs) are deeper dives for the curious, not required reading.
