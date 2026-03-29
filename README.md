
# Redis RDB Snapshots Analysis

This project analyzes how Redis performs persistence using RDB snapshots, focusing on execution flow, design decisions, and system behavior.

## Objective

To understand how Redis saves in-memory data to disk without blocking operations, by tracing code and validating behavior through experiments.

## System Studied

- Redis (in-memory key-value store)
- Feature: RDB Snapshots

---

## Setup Instructions (Run Locally)

### 1. Install Redis (WSL / Linux)

```bash
sudo apt update
sudo apt install redis
````

### 2. Start Redis Server

```bash
redis-server
```

Open another terminal:

```bash
redis-cli
```

---

## Basic Commands Used

### Insert data

```bash
SET key1 value1
GET key1
```

### Trigger snapshot

```bash
BGSAVE
```

### Find storage location

```bash
CONFIG GET dir
```

### Verify snapshot file

```bash
sudo ls -lh /var/lib/redis
```

---

## Execution Path (Summary)

BGSAVE → bgsaveCommand() → rdbSaveBackground() → fork() → rdbSave() → dump.rdb

---

## Experiments Performed

* Manual snapshot using BGSAVE
* Verified creation of `dump.rdb`
* Observed storage directory and permissions

---

## Repository Structure

```
redis-rdb-analysis/
├── report.md
├── code-references/
├── experiments/
├── screenshots/
└── redis/ (source code)
```

---

## Key Insight

Redis uses a fork-based design to perform snapshotting in a separate process. This keeps the system responsive but introduces memory overhead due to copy-on-write.

```

