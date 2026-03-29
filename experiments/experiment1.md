--

## Experiment: RDB Snapshot using BGSAVE

### Objective

To observe how Redis creates a snapshot of in-memory data and stores it on disk.

---

### Setup

- Redis installed and running locally
- Redis CLI used for interaction

---

### Steps

1. Inserted sample data into Redis:

```

SET key1 value1
GET key1

```

2. Triggered snapshot:

```

BGSAVE

```

3. Checked storage directory:

```

CONFIG GET dir

```

4. Verified snapshot file:

```

sudo ls -lh /var/lib/redis

```

---

### Observations

- Redis returned "Background saving started" after executing BGSAVE.
- The system did not block during snapshot creation.
- A file named `dump.rdb` was created.
- The file was stored in `/var/lib/redis`, which required root access.

---

### Screenshots

![BGSAVE](../screenshots/bgsave.png)

![RDB File](../screenshots/dump_rdb.png)

---

### Conclusion

Redis performs snapshotting asynchronously using a background process. The use of fork() allows the system to continue serving requests while persistence is handled separately.

This confirms that Redis prioritizes performance while providing basic durability through snapshots.
```

