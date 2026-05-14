# Gap fixes for exp6 patches

The patches in this directory (`exp6_*.diff`) implement most of Delta RDB but have six gaps identified in `../design.md`. Below are the exact C changes to close them.

Apply order: existing exp6 patches first, then these additions.

---

## Fix 1 — Initialize tracking dicts in `initServer()`

**File:** `redis/src/server.c`
**Location:** in `initServer()`, where each `redisDb` is constructed. Search for `server.db[j].dict = dictCreate(&dbDictType, NULL);`.

**Add immediately after that line:**

```c
/* === EXP6 INCREMENTAL RDB ===
 * Tracking dicts must be allocated on server boot. Without these,
 * every dirty/deleted-key signal NULL-derefs through dictAdd. */
server.db[j].dirty_keys   = dictCreate(&keyptrDictType, NULL);
server.db[j].deleted_keys = dictCreate(&keyptrDictType, NULL);
```

The `keyptrDictType` is the same type used for `db->expires`; it stores sds keys with no associated value, which is exactly the set semantics we want.

---

## Fix 2 — Clear tracking dicts on `FLUSHDB` / `FLUSHALL` / `emptyDb`

**File:** `redis/src/db.c`
**Location:** at the bottom of `emptyDbStructure()`, after the existing `dictEmpty(db->dict, callback);` and `dictEmpty(db->expires, callback);`.

**Add:**

```c
/* === EXP6 INCREMENTAL RDB ===
 * FLUSHDB clears the keyspace; the dirty/deleted sets must be cleared
 * too. A FLUSHDB does NOT mean every previously-existing key needs a
 * tombstone in the next delta — that would be catastrophically large.
 * Instead, FLUSHDB forces the next save to be a full snapshot. */
if (db->dirty_keys)   dictEmpty(db->dirty_keys, NULL);
if (db->deleted_keys) dictEmpty(db->deleted_keys, NULL);
server.rdb_force_full_next = 1;
```

Add the field `int rdb_force_full_next;` to `struct redisServer` in `server.h`, initialized to 0 in `initServerConfig()`.

---

## Fix 3 — Branch save path between delta and full in `rdbSaveBackground()`

**File:** `redis/src/rdb.c`
**Location:** at the top of `rdbSaveBackground()`, before the existing `hasActiveChildProcess()` check is fine, but the actual branch goes inside the child block.

**Replace the child block:**

```c
if ((childpid = redisFork(CHILD_TYPE_RDB)) == 0) {
    int retval;
    /* === EXP6 INCREMENTAL RDB === decide delta vs full */
    size_t total_keys = 0, dirty_count = 0;
    for (int j = 0; j < server.dbnum; j++) {
        total_keys += dictSize(server.db[j].dict);
        if (server.db[j].dirty_keys)
            dirty_count += dictSize(server.db[j].dirty_keys);
        if (server.db[j].deleted_keys)
            dirty_count += dictSize(server.db[j].deleted_keys);
    }
    double ratio = total_keys ? (double)dirty_count / total_keys : 1.0;

    int use_incremental =
        server.rdb_delta_mode_enabled &&
        !server.rdb_force_full_next &&
        server.last_full_save_uuid[0] != '\0' &&
        ratio < server.rdb_delta_fallback_ratio;

    if (use_incremental) {
        retval = rdbSaveIncremental(filename, server.last_full_save_uuid);
        server.stat_delta_keys_serialized += dirty_count;
    } else {
        retval = rdbSave(req, filename, rsi, rdbflags);
        if (retval == C_OK) {
            /* Stamp fresh UUID for the new base */
            struct timeval tv;
            gettimeofday(&tv, NULL);
            snprintf(server.last_full_save_uuid, 40,
                     "%016lx-%08x-%08x",
                     (unsigned long)tv.tv_sec, (unsigned int)tv.tv_usec,
                     (unsigned int)rand());
            if (server.rdb_force_full_next) {
                server.rdb_force_full_next = 0;
                server.stat_delta_full_fallbacks++;
            }
        }
    }
    exitFromChild((retval == C_OK) ? 0 : 1, 0);
}
```

---

## Fix 4 — Clear dirty/deleted sets only after durable rename

**File:** `redis/src/rdb.c`
**Location:** in `rdbSaveIncremental()`, after the `rename(tmpfile, filename)` success branch.

The existing exp6 patch likely clears sets too early. Correct ordering:

```c
if (rename(tmpfile, filename) == -1) {
    serverLog(LL_WARNING, "EXP6 incr save: rename failed: %s", strerror(errno));
    unlink(tmpfile);
    return C_ERR;
}

/* Fsync the containing directory so the rename is durable.
 * Without this, a power loss between rename and the next save
 * could leave us with cleared sets but no actual delta on disk. */
char dirpath[256];
char *slash = strrchr(filename, '/');
if (slash) {
    size_t n = slash - filename;
    if (n >= sizeof(dirpath)) n = sizeof(dirpath) - 1;
    memcpy(dirpath, filename, n);
    dirpath[n] = '\0';
} else {
    strcpy(dirpath, ".");
}
int dfd = open(dirpath, O_RDONLY);
if (dfd >= 0) { fsync(dfd); close(dfd); }

/* Only NOW is it safe to clear the tracking sets. */
for (int j = 0; j < server.dbnum; j++) {
    if (server.db[j].dirty_keys)   dictEmpty(server.db[j].dirty_keys, NULL);
    if (server.db[j].deleted_keys) dictEmpty(server.db[j].deleted_keys, NULL);
}
return C_OK;
```

The same directory-fsync should be added to `rdbSave()` for symmetry, but stock Redis already does this so check whether your build has it (look for `fsync(dirfd)` near the end of `rdbSave`).

---

## Fix 5 — Block delta during replication full-resync

**File:** `redis/src/replication.c`
**Location:** in `startBgsaveForReplication()`, before calling `rdbSaveBackground()`.

```c
/* === EXP6 INCREMENTAL RDB ===
 * Replicas need a self-contained RDB. Force full snapshot. */
int saved_delta_mode = server.rdb_delta_mode_enabled;
server.rdb_delta_mode_enabled = 0;
int retval = rdbSaveBackground(...);   /* existing call */
server.rdb_delta_mode_enabled = saved_delta_mode;
```

Or, if cleaner, gate inside `rdbSaveBackground()` itself by checking the rsi/rdbflags for `RDBFLAGS_REPLICATION` and forcing full when set.

---

## Fix 6 — Wire INFO counters

**File:** `redis/src/server.c`
**Location:** in `genRedisInfoString()`, inside the `persistence` section after the existing `rdb_last_bgsave_time_sec` line.

```c
info = sdscatprintf(info,
    "rdb_delta_mode_enabled:%d\r\n"
    "rdb_delta_fallback_ratio:%.2f\r\n"
    "rdb_delta_keys_serialized:%lld\r\n"
    "rdb_delta_full_fallbacks:%lld\r\n"
    "rdb_last_full_save_uuid:%s\r\n",
    server.rdb_delta_mode_enabled,
    server.rdb_delta_fallback_ratio,
    server.stat_delta_keys_serialized,
    server.stat_delta_full_fallbacks,
    server.last_full_save_uuid);
```

And in `initServerConfig()` add the defaults:

```c
server.rdb_delta_mode_enabled = 0;       /* off by default */
server.rdb_delta_fallback_ratio = 0.5;
server.last_full_save_uuid[0] = '\0';
server.stat_delta_keys_serialized = 0;
server.stat_delta_full_fallbacks = 0;
server.rdb_force_full_next = 0;
```

And register the two config knobs in `config.c`'s `configs[]` table:

```c
createBoolConfig("rdb-delta-mode", NULL, MODIFIABLE_CONFIG,
                 server.rdb_delta_mode_enabled, 0, NULL, NULL),
createDoubleConfig("rdb-delta-fallback-ratio", NULL, MODIFIABLE_CONFIG,
                   0.0, 1.0, server.rdb_delta_fallback_ratio, 0.5, NULL, NULL),
```

(If `createDoubleConfig` doesn't exist in your Redis 6.2 base, use an integer percent-config from 0–100 instead.)

---

## Build

```
cd redis/
git apply ../3-major-modifications/delta-rdb/patches/exp6_server.h.diff
git apply ../3-major-modifications/delta-rdb/patches/exp6_rdb.h.diff
git apply ../3-major-modifications/delta-rdb/patches/exp6_db.c.diff
git apply ../3-major-modifications/delta-rdb/patches/exp6_lazyfree.c.diff
git apply ../3-major-modifications/delta-rdb/patches/exp6_rdb.c.diff
git apply ../3-major-modifications/delta-rdb/patches/exp6_debug.c.diff
git apply ../3-major-modifications/delta-rdb/patches/exp6_server.c.diff
# then apply the six fixes above manually (no diff yet — paste into the right spots)
make -j4
```
