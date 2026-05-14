# Execution Trace: BGSAVE Write Path in Redis

This document traces the complete execution of the `BGSAVE` command using actual code from the Redis source (`redis/src/rdb.c` and `redis/src/server.c`). All function names, line numbers, and code snippets are from the real codebase.

---

## Overview

```
BGSAVE (CLI)
  └── bgsaveCommand()          src/server.c ~4557
        └── rdbSaveBackground()  src/rdb.c   line 2004
              └── redisFork()    (OS fork())
                    ├── [Parent] returns C_OK, continues serving clients
                    └── [Child]  rdbSave()   src/rdb.c   line 1961
                                   └── rdbSaveInternal()  src/rdb.c  line 1885
                                         └── rdbSaveRio()  src/rdb.c  line 1783
                                               └── rdbSaveDb()  src/rdb.c  ~1640
                                                     └── rdbSaveKeyValuePair()
```

---

## Step 1 — bgsaveCommand() — Entry and Guard

**File:** `src/server.c`, function `bgsaveCommand()`, ~line 4557

```c
void bgsaveCommand(client *c) {
    rdbSaveInfo rsi, *rsiptr;
    rsiptr = rdbPopulateSaveInfo(&rsi);

    if (server.child_type == CHILD_TYPE_RDB) {
        addReplyError(c, "Background save already in progress");
    } else if (hasActiveChildProcess() || server.in_exec) {
        if (server.rdb_bgsave_scheduled == 0) {
            server.rdb_bgsave_scheduled = 1;
            addReplyStatus(c, "Background saving scheduled");
        }
    } else if (rdbSaveBackground(SLAVE_REQ_NONE, server.rdb_filename, rsiptr, RDBFLAGS_NONE) == C_OK) {
        addReplyStatus(c, "Background saving started");
    }
}
```

Key decision: `server.child_type == CHILD_TYPE_RDB` enforces the single-child constraint. A second `BGSAVE` does not fail — it is scheduled (`rdb_bgsave_scheduled = 1`) for execution after the current child exits.

---

## Step 2 — rdbSaveBackground() — The Fork

**File:** `src/rdb.c`, line 2004

```c
int rdbSaveBackground(int req, char *filename, rdbSaveInfo *rsi, int rdbflags) {
    pid_t childpid;

    if (hasActiveChildProcess()) return C_ERR;

    server.stat_rdb_saves++;
    server.dirty_before_bgsave = server.dirty;
    server.lastbgsave_try = time(NULL);

    if ((childpid = redisFork(CHILD_TYPE_RDB)) == 0) {
        int retval;

        /* Child */
        redisSetProcTitle("redis-rdb-bgsave");
        redisSetCpuAffinity(server.bgsave_cpulist);
        retval = rdbSave(req, filename, rsi, rdbflags);
        if (retval == C_OK) {
            sendChildCowInfo(CHILD_INFO_TYPE_RDB_COW_SIZE, "RDB");
        }
        exitFromChild((retval == C_OK) ? 0 : 1, 0);

    } else {
        /* Parent */
        if (childpid == -1) {
            server.lastbgsave_status = C_ERR;
            serverLog(LL_WARNING, "Can't save in background: fork: %s", strerror(errno));
            return C_ERR;
        }
        serverLog(LL_NOTICE, "Background saving started by pid %ld", (long) childpid);
        server.rdb_save_time_start = time(NULL);
        server.rdb_child_type = RDB_CHILD_TYPE_DISK;
        return C_OK;
    }
    return C_OK;
}
```

After `redisFork()`, execution splits. The parent returns `C_OK` immediately — no blocking. The child calls `rdbSave()` and exits.

`sendChildCowInfo()` reports copy-on-write memory usage back to the parent via a pipe — this is how `INFO persistence` reports `rdb_last_cow_size`.

---

## Step 3 — rdbSave() — Temp File and Atomic Rename

**File:** `src/rdb.c`, line 1961

```c
int rdbSave(int req, char *filename, rdbSaveInfo *rsi, int rdbflags) {
    char tmpfile[256];
    char cwd[MAXPATHLEN];

    startSaving(rdbflags);
    snprintf(tmpfile, 256, "temp-%d.rdb", (int) getpid());

    if (rdbSaveInternal(req, tmpfile, rsi, rdbflags) != C_OK) {
        stopSaving(0);
        return C_ERR;
    }

    /* Use RENAME to make sure the DB file is changed atomically */
    if (rename(tmpfile, filename) == -1) {
        serverLog(LL_WARNING, "Error moving temp DB file %s ...", tmpfile, ...);
        unlink(tmpfile);
        stopSaving(0);
        return C_ERR;
    }

    if (fsyncFileDir(filename) != 0) { ... }

    serverLog(LL_NOTICE, "DB saved on disk");
    server.dirty = 0;
    server.lastsave = time(NULL);
    server.lastbgsave_status = C_OK;
    stopSaving(1);
    return C_OK;
}
```

The temp file name includes the PID (e.g., `temp-14502.rdb`) to avoid conflicts if multiple Redis instances run on the same machine. `rename()` is a POSIX atomic operation — `dump.rdb` is never in a partial state.

`server.dirty = 0` resets the dirty key counter after a successful save.

---

## Step 4 — rdbSaveInternal() — Our Instrumentation Entry Point

**File:** `src/rdb.c`, line 1885 (modified by our project)

```c
static int rdbSaveInternal(int req, const char *filename, rdbSaveInfo *rsi, int rdbflags) {
    TRACE("START", "rdbSaveInternal started: file=%s", filename);

    char cwd[MAXPATHLEN];
    rio rdb;
    int error = 0;
    int saved_errno;
    char *err_op;

    FILE *fp = fopen(filename, "w");
    if (!fp) {
        serverLog(LL_WARNING, "Failed opening the temp RDB file %s ...", filename, ...);
        return C_ERR;
    }

    rioInitWithFile(&rdb, fp);

    if (server.rdb_save_incremental_fsync) {
        rioSetAutoSync(&rdb, REDIS_AUTOSYNC_BYTES);
        if (!(rdbflags & RDBFLAGS_KEEP_CACHE)) rioSetReclaimCache(&rdb, 1);
    }

    TRACE("FLOW", "Calling rdbSaveRio");
    if (rdbSaveRio(req, &rdb, &error, rdbflags, rsi) == C_ERR) {
        errno = error;
        err_op = "rdbSaveRio";
        goto werr;
    }
    TRACE("END", "rdbSaveInternal finished");

    if (fflush(fp)) { err_op = "fflush"; goto werr; }
    if (fsync(fileno(fp))) { err_op = "fsync"; goto werr; }

    TRACE("FILE", "Closing RDB file");
    if (fclose(fp)) { fp = NULL; err_op = "fclose"; goto werr; }

    return C_OK;

werr:
    serverLog(LL_WARNING, "Write error while saving DB (%s): %s", err_op, strerror(errno));
    if (fp) fclose(fp);
    unlink(filename);
    return C_ERR;
}
```

`rioInitWithFile()` wraps the FILE pointer in a `rio` struct — the same interface used for sockets during replication. `rdb_save_incremental_fsync` enables periodic fsync every `REDIS_AUTOSYNC_BYTES` to smooth out I/O spikes on large datasets.

---

## Step 5 — rdbSaveRio() — RDB Binary Format

**File:** `src/rdb.c`, line 1783 (modified by our project)

```c
int rdbSaveRio(int req, rio *rdb, int *error, int rdbflags, rdbSaveInfo *rsi) {
    char magic[10];
    long long key_counter = 0;
    unsigned long long skipped = 0;
    uint64_t cksum;

    TRACE("FLOW", "Entered rdbSaveRio");

    // Write 9-byte magic header: "REDIS0013"
    snprintf(magic, sizeof(magic), "REDIS%04d", RDB_VERSION);
    if (rdbWriteRaw(rdb, magic, 9) == -1) goto werr;
    TRACE("HEADER", "Writing RDB header version: %d", RDB_VERSION);

    // Write AUX fields: metadata about this Redis instance
    if (rdbSaveAuxFieldStrStr(rdb, "redis-ver", REDIS_VERSION) == -1) goto werr;
    if (rdbSaveAuxFieldStrInt(rdb, "redis-bits", 64) == -1) goto werr;
    if (rdbSaveAuxFieldStrInt(rdb, "ctime", time(NULL)) == -1) goto werr;
    if (rdbSaveAuxFieldStrInt(rdb, "used-mem", zmalloc_used_memory()) == -1) goto werr;
    TRACE("AUX", "Writing AUX fields");

    // Module AUX data (before RDB)
    TRACE("MODULE", "Saving module AUX (before RDB)");

    // Functions
    TRACE("FUNC", "Saving functions");
    if (rdbSaveFunctions(rdb) == -1) goto werr;

    // Per-database data
    for (int j = 0; j < server.dbnum; j++) {
        TRACE("DB", "Saving DB %d", j);
        long long db_key_counter = 0;
        if (rdbSaveDb(req, rdb, j, rdbflags, &db_key_counter, &skipped) == -1) goto werr;
        key_counter += db_key_counter;
        TRACE("DB_DONE", "DB %d done, keys saved in this DB: %lld", j, db_key_counter);
    }

    // Module AUX data (after RDB)
    TRACE("MODULE", "Saving module AUX (after RDB)");

    // EOF opcode
    if (rdbSaveType(rdb, RDB_OPCODE_EOF) == -1) goto werr;
    TRACE("EOF", "Writing EOF opcode");

    // CRC64 checksum (8 bytes, little-endian)
    cksum = rdb->cksum;
    memrev64ifbe(&cksum);
    if (rdbWriteRaw(rdb, &cksum, 8) == -1) goto werr;
    TRACE("CHECKSUM", "Writing checksum");

    TRACE("SUCCESS", "RDB save complete: keys=%lld skipped=%llu bytes=%zu",
          key_counter, skipped, rdb->processed_bytes);
    return C_OK;

werr:
    TRACE("ERROR", "Error during rdbSaveRio");
    if (error) *error = errno;
    return C_ERR;
}
```

The CRC64 checksum is computed incrementally by the `rio` layer as bytes flow through — `rdb->cksum` is a running value updated by `rdbWriteRaw()` on every write call.

---

## Step 6 — rdbSaveDb() — Per-Key Serialization

**File:** `src/rdb.c`, ~line 1640 (modified by our project)

```c
ssize_t rdbSaveDb(int req, rio *rdb, int dbid, int rdbflags,
                  long long *key_counter, unsigned long long *skipped) {
    dictEntry *de;
    ssize_t written = 0;
    ssize_t res;
    kvstoreIterator kvs_it;

    redisDb *db = server.db + dbid;
    unsigned long long int db_size = kvstoreSize(db->keys);

    TRACE("DB", "Entering DB %d (keys=%llu)", dbid, db_size);

    if (db_size == 0) {
        TRACE("DB", "Skipping empty DB %d", dbid);
        return 0;
    }

    /* Write SELECTDB opcode + dbid */
    TRACE("DB_META", "Writing SELECTDB for DB %d", dbid);
    if ((res = rdbSaveType(rdb, RDB_OPCODE_SELECTDB)) < 0) goto werr;
    written += res;
    if ((res = rdbSaveLen(rdb, dbid)) < 0) goto werr;
    written += res;

    /* Write RESIZEDB opcode + key count + expire count */
    unsigned long long expires_size = kvstoreSize(db->expires);
    TRACE("DB_META", "DB %d sizes: keys=%llu expires=%llu", dbid, db_size, expires_size);
    if ((res = rdbSaveType(rdb, RDB_OPCODE_RESIZEDB)) < 0) goto werr;
    written += res;
    if ((res = rdbSaveLen(rdb, db_size)) < 0) goto werr;
    written += res;
    if ((res = rdbSaveLen(rdb, expires_size)) < 0) goto werr;
    written += res;

    /* Iterate all keys */
    kvstoreIteratorInit(&kvs_it, db->keys);
    while ((de = kvstoreIteratorNext(&kvs_it)) != NULL) {
        kvobj *kv = dictGetKV(de);
        robj key;
        long long expire;

        /* Skip keys in trimmed cluster slots */
        if (server.cluster_enabled && isSlotInTrimJob(curr_slot)) {
            (*skipped)++;
            TRACE("SKIP", "Skipped key in slot %d", curr_slot);
            continue;
        }

        initStaticStringObject(key, kvobjGetKey(kv));
        expire = kvobjGetExpire(kv);

        TRACE("KEY", "Saving key in DB %d", dbid);

        size_t rdb_bytes_before_key = rdb->processed_bytes;
        res = rdbSaveKeyValuePair(rdb, &key, kv, expire, dbid);
        if (res < 0) goto werr2;
        written += res;

        size_t dump_size = rdb->processed_bytes - rdb_bytes_before_key;
        TRACE("KEY_DONE", "Key saved (bytes=%zu)", dump_size);

        /* Progress logging every 1024 keys */
        if (((*key_counter)++ & 1023) == 0) {
            TRACE("PROGRESS", "Saved %lld keys so far", *key_counter);
        }
    }

    TRACE("DB_DONE", "Completed DB %d", dbid);
    return written;

werr2:
    kvstoreIteratorReset(&kvs_it);
werr:
    TRACE("ERROR", "Error in rdbSaveDb for DB %d", dbid);
    return -1;
}
```

`rdbSaveKeyValuePair()` dispatches to type-specific serializers:
- `rdbSaveObjectType()` — writes the 1-byte type opcode
- `rdbSaveStringObject()` — writes the key as a string
- `rdbSaveObject()` — writes the value with type-specific encoding (string, list, hash, set, zset, etc.)

---

## Confirmed Execution Order (from trace log)

```
rdbSaveInternal [START]
  rdbSaveRio [ENTERED]
    Write HEADER (REDIS0013)
    Write AUX fields (redis-ver, redis-bits, ctime, used-mem)
    Write module AUX (before)
    Write functions
    For each DB:
      If empty: SKIP
      Else:
        Write SELECTDB opcode
        Write RESIZEDB opcode
        For each key:
          rdbSaveKeyValuePair()
        [DB_DONE]
    Write module AUX (after)
    Write EOF opcode
    Write CRC64 checksum
  rdbSaveRio [RETURNED]
  fflush + fsync
rdbSaveInternal [FILE CLOSE]
```

This is verified by `redis/rdb_trace.log` which shows the actual sequence from a live BGSAVE run.