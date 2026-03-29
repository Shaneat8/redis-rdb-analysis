
# Execution Trace: BGSAVE in Redis

This section traces the execution of the `BGSAVE` command using actual code from the Redis source.

---

## 1. Command Entry

The command:

BGSAVE

invokes the function:

```c
void bgsaveCommand(client *c)
````

defined in `src/server.c`.

---

## 2. Validation in bgsaveCommand

Redis first checks whether a background save is already running:

```c
if (server.child_type == CHILD_TYPE_RDB) {
    addReplyError(c,"Background save already in progress");
}
```

If true, the command is rejected.

---

If another background process is active:

```c
else if (hasActiveChildProcess() || server.in_exec) {
```

Then:

* If scheduling is allowed:

```c
server.rdb_bgsave_scheduled = 1;
```

* Otherwise, the command is rejected.

---

## 3. Triggering Background Save

If conditions allow execution:

```c
rdbSaveBackground(SLAVE_REQ_NONE,server.rdb_filename,rsiptr,RDBFLAGS_NONE)
```

Control moves from `server.c` to `rdb.c`.

---

## 4. Entering rdbSaveBackground

```c
int rdbSaveBackground(int req, char *filename, rdbSaveInfo *rsi, int rdbflags)
```

Initial guard:

```c
if (hasActiveChildProcess()) return C_ERR;
```

---

## 5. Forking the Process

```c
if ((childpid = redisFork(CHILD_TYPE_RDB)) == 0) {
```

This creates two execution paths:

* Child process (childpid == 0)
* Parent process (childpid > 0)

---

## 6. Child Process Execution

```c
retval = rdbSave(req, filename,rsi,rdbflags);
exitFromChild((retval == C_OK) ? 0 : 1, 0);
```

The child process performs the snapshot and then exits.

---

## 7. Snapshot Creation in rdbSave

```c
int rdbSave(int req, char *filename, rdbSaveInfo *rsi, int rdbflags)
```

Core steps:

```c
snprintf(tmpfile,256,"temp-%d.rdb", (int) getpid());
rdbSaveInternal(req,tmpfile,rsi,rdbflags);
```

A temporary file is created and written.

---

## 8. Final File Commit

```c
rename(tmpfile,filename);
```

The temporary file is atomically renamed to:

dump.rdb

---

## 9. Key-Value Serialization

During snapshot creation, data is written using:

```c
int rdbSaveKeyValuePair(...)
```

Example:

```c
rdbSaveObjectType(rdb,val);
rdbSaveStringObject(rdb,key);
rdbSaveObject(rdb,val,key,dbid);
```

Each key-value pair is serialized into the RDB file.

---

## 10. Parent Process Behavior

After fork:

```c
serverLog(LL_NOTICE,"Background saving started by pid %ld",(long) childpid);
return C_OK;
```

The parent process does not wait for the child and continues handling requests.

---

## Final Observation

The execution path splits at `redisFork()`. The child process handles disk I/O and snapshot creation, while the parent process remains responsive. This design ensures non-blocking persistence but relies on copy-on-write memory behavior.

```
