# Redis RDB Snapshot Execution Flow

A line-anchored walkthrough of how a single `BGSAVE` command becomes bytes on disk. Every function reference points to real code in `redis/src/`. The intent is to ground the rest of the project: before modifying the snapshot path, the reader should know exactly what the snapshot path is.

This document is the cleaned-up successor to the original `report.md` sections 2–4. Material that was about persistence-in-general (AOF, kill -9 behavior, RDB-vs-AOF comparisons) has been removed.

---

## 1. Command entry — `bgsaveCommand`

File: `redis/src/server.c` (~line 4557)

```c
void bgsaveCommand(client *c) {
    if (server.child_type == CHILD_TYPE_RDB) {
        addReplyError(c, "Background save already in progress");
        return;
    }
    rdbSaveBackground(SLAVE_REQ_NONE, server.rdb_filename, rsiptr, RDBFLAGS_NONE);
}
```

The `server.child_type == CHILD_TYPE_RDB` guard enforces Redis's single-background-child invariant: only one BGSAVE / AOF-rewrite / replication-fork can run at a time. This guard is the architectural reason BGSAVE serialization cost cannot be hidden behind parallelism.

---

## 2. Fork — `rdbSaveBackground`

File: `redis/src/rdb.c` (~line 2004)

```c
int rdbSaveBackground(int req, char *filename, rdbSaveInfo *rsi, int rdbflags) {
    if (hasActiveChildProcess()) return C_ERR;

    server.dirty_before_bgsave = server.dirty;

    if ((childpid = redisFork(CHILD_TYPE_RDB)) == 0) {
        /* Child */
        retval = rdbSave(req, filename, rsi, rdbflags);
        exitFromChild((retval == C_OK) ? 0 : 1, 0);
    } else {
        /* Parent — returns immediately */
        server.rdb_child_type = RDB_CHILD_TYPE_DISK;
        return C_OK;
    }
}
```

`redisFork()` wraps the OS `fork()`. The parent records the child PID and returns to the event loop. The child does all I/O.

Two structural facts to remember:

- The parent pause during `fork()` itself is not zero — the kernel must duplicate page tables. This is the cost Supporting Experiment **S1** measures.
- Every page the parent later modifies during the child's lifetime gets duplicated by the kernel's CoW mechanism. This is the cost Major Experiment **B (CoW throttling)** addresses.

---

## 3. Temp file + atomic rename — `rdbSave`

File: `redis/src/rdb.c` (~line 1961)

```c
int rdbSave(int req, char *filename, rdbSaveInfo *rsi, int rdbflags) {
    char tmpfile[256];
    snprintf(tmpfile, 256, "temp-%d.rdb", (int) getpid());

    if (rdbSaveInternal(req, tmpfile, rsi, rdbflags) != C_OK) {
        unlink(tmpfile);
        return C_ERR;
    }

    if (rename(tmpfile, filename) == -1) {
        unlink(tmpfile);
        return C_ERR;
    }

    server.dirty = 0;
    server.lastsave = time(NULL);
    return C_OK;
}
```

Two crash-safety properties depend on this layout:

- `dump.rdb` is only ever overwritten by a complete file. A crash mid-write leaves the previous good snapshot untouched.
- `rename()` must be atomic on the target filesystem (this is true on ext4, xfs, btrfs; not guaranteed on some NFS configurations).

---

## 4. File open + dispatch — `rdbSaveInternal`

File: `redis/src/rdb.c` (~line 1885)

```c
static int rdbSaveInternal(int req, const char *filename,
                           rdbSaveInfo *rsi, int rdbflags) {
    rio rdb;
    FILE *fp = fopen(filename, "w");
    rioInitWithFile(&rdb, fp);
    rdbSaveRio(req, &rdb, &error, rdbflags, rsi);
    fflush(fp);
    fsync(fileno(fp));
    fclose(fp);
}
```

The `rio` abstraction (`redis/src/rio.c`) is the reason the same serialization code works for disk, sockets (replication), and in-memory buffers. The buffering threshold and `rdb-save-incremental-fsync` interact here — `rioFileAutoSync()` issues `sync_file_range()` every N bytes (default 4 MB).

---

## 5. Format orchestration — `rdbSaveRio`

File: `redis/src/rdb.c` (~line 1783)

```c
int rdbSaveRio(int req, rio *rdb, int *error, int rdbflags, rdbSaveInfo *rsi) {
    /* Magic: "REDIS0013" */
    snprintf(magic, sizeof(magic), "REDIS%04d", RDB_VERSION);
    rdbWriteRaw(rdb, magic, 9);

    /* AUX fields: redis-ver, redis-bits, ctime, used-mem, repl-id... */
    rdbSaveAuxFieldStrStr(rdb, "redis-ver", REDIS_VERSION);

    /* Per-database loop */
    for (int j = 0; j < server.dbnum; j++) {
        rdbSaveDb(req, rdb, j, rdbflags, &key_counter, &skipped);
    }

    /* EOF + 8-byte CRC64 trailer */
    rdbSaveType(rdb, RDB_OPCODE_EOF);
    cksum = rdb->cksum;
    memrev64ifbe(&cksum);
    rdbWriteRaw(rdb, &cksum, 8);
}
```

The structural layout is: magic → AUX → modules-before → functions → per-DB blocks → modules-after → EOF → CRC64. **Supporting Experiment S3** corrupts one byte in each of these structural regions and measures whether the load path catches it before reaching the CRC, at the CRC, or silently loads garbage.

---

## 6. Per-key serialization — `rdbSaveDb`

File: `redis/src/rdb.c` (~line 1640)

```c
int rdbSaveDb(...) {
    redisDb *db = server.db + dbid;
    if (dbSize(db) == 0) return C_OK;            /* Empty DBs skipped */

    rdbSaveType(rdb, RDB_OPCODE_SELECTDB);
    rdbSaveLen(rdb, dbid);
    rdbSaveType(rdb, RDB_OPCODE_RESIZEDB);
    rdbSaveLen(rdb, db_size);

    while ((de = kvstoreIteratorNext(&kvs_it)) != NULL) {
        /* Currently: every entry is serialized.
         * Major Experiment A (Delta RDB) inserts a dirty-generation
         * check here that skips entries whose rdb_last_touched_gen
         * is older than rdb_generation_last_saved. */
        rdbSaveKeyValuePair(rdb, &key, kv, expire, dbid);
    }
    return C_OK;
}
```

This loop is the **O(n) over keyspace** cost that Major Experiment A attacks. Stock Redis has no notion of "which keys are dirty" — every key is iterated and serialized on every BGSAVE regardless of whether it changed.

---

## 7. Key insight, restated

Two structural costs dominate BGSAVE:

1. **Serialization cost: O(total keys), not O(modified keys).** Attacked by Major Experiment A (Delta RDB).
2. **Memory cost: up to 2× RSS under worst-case write traffic during BGSAVE.** Attacked by Major Experiment B (CoW throttling).

The next sections of this repo characterize each cost in stock Redis (supporting experiments S1, S2, S3), then propose and measure architectural modifications (major experiments A, B).
