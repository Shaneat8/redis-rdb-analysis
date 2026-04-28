"""
Selective Group-Commit Durability Sidecar
=========================================

A client-side library that adds bounded-loss durability for *critical*
writes on top of an unmodified Redis configured with AOF everysec.

Key idea
--------
Two write paths:

  set(key, value)           -> normal Redis SET, durability = AOF everysec
  set_critical(key, value)  -> appended to a client-side WAL buffer; a
                                background flusher fsyncs the WAL every
                                FLUSH_INTERVAL_MS or when the buffer
                                reaches FLUSH_BYTES, whichever first.
                                The caller blocks until its sequence
                                number has been fsynced.

Crucially, set_critical does NOT fsync per write. N concurrent critical
writes that arrive within the same flush window share ONE fsync. This is
the "group commit" trick used by databases like PostgreSQL
(commit_delay/commit_siblings) and InnoDB
(innodb_flush_log_at_trx_commit=1 with the IO thread).

Redis itself is unchanged: appendonly yes / appendfsync everysec.
We don't touch the server.

Why this beats per-write fsync
------------------------------
A single fsync on a modern SSD costs ~100-500us regardless of how many
bytes are queued. Batching K writes into one fsync amortizes that cost
by K. With FLUSH_INTERVAL_MS=5, the worst-case ack latency added is
5ms but the per-write fsync amortization can be 5-50x under concurrent
load.

Why it beats AOF everysec for critical data
-------------------------------------------
AOF everysec leaves an up-to-1-second window of writes vulnerable to
power loss. set_critical bounds that window to <= FLUSH_INTERVAL_MS
(default 5ms) for the writes the user marked critical, while leaving
non-critical writes on the cheap fast path.

Recovery
--------
On restart, replay() reads the WAL, looks up __sgc:applied_seq in
Redis, and reapplies any critical writes whose seq > applied_seq.
Recovery is idempotent.
"""
from __future__ import annotations
import os
import struct
import threading
import time
from collections import deque
from typing import Optional

import redis


HDR = struct.Struct(">QI")  # seq:u64, payload_len:u32


class SgcClient:
    """Selective Group-Commit Durability Sidecar client."""

    def __init__(
        self,
        *,
        wal_path: str,
        port: Optional[int] = None,
        flush_interval_ms: float = 5.0,
        flush_bytes: int = 64 * 1024,
    ) -> None:
        self.wal_path = wal_path
        self.flush_interval = flush_interval_ms / 1000.0
        self.flush_bytes = flush_bytes
        kwargs = {}
        if port is not None:
            kwargs["port"] = port
        self.r = redis.Redis(**kwargs)

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        self.fd = os.open(wal_path, flags, 0o600)
        self.seq_lock = threading.Lock()
        self.seq = self._scan_for_max_seq(wal_path)

        # Pending records waiting to be fsynced.
        # Each entry: (seq, record_bytes, ack_event, key, value)
        self.buf_lock = threading.Lock()
        self.pending: deque = deque()
        self.pending_bytes = 0
        self.flush_event = threading.Event()
        self.last_flushed_seq = self.seq  # everything <= this is durable

        # Telemetry
        self.flush_count = 0
        self.flush_record_count = 0
        self.flush_byte_count = 0
        self.fsync_us_total = 0
        self.fsync_us_max = 0

        self.stop_flag = threading.Event()
        self.flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self.flusher.start()

    # ---------- helpers ----------

    @staticmethod
    def _scan_for_max_seq(path: str) -> int:
        max_seq = 0
        try:
            with open(path, "rb") as f:
                while True:
                    hdr = f.read(HDR.size)
                    if len(hdr) < HDR.size:
                        break
                    seq, plen = HDR.unpack(hdr)
                    f.read(plen)
                    if seq > max_seq:
                        max_seq = seq
        except FileNotFoundError:
            pass
        return max_seq

    def close(self) -> None:
        self.stop_flag.set()
        self.flush_event.set()
        self.flusher.join(timeout=2.0)
        # Final flush of anything lingering (defensive)
        self._flush_once()
        try:
            os.close(self.fd)
        except OSError:
            pass

    # ---------- public write paths ----------

    def set(self, key: str, value: str) -> None:
        """Normal path: relies on AOF everysec for durability."""
        self.r.set(key, value)

    def set_critical(self, key: str, value: str) -> int:
        """Critical path: durable before returning. Group-committed."""
        kb = key.encode()
        vb = value.encode()
        payload = b"%d\n%s\n%s" % (len(kb), kb, vb)

        with self.seq_lock:
            self.seq += 1
            seq = self.seq
        record = HDR.pack(seq, len(payload)) + payload

        ack = threading.Event()
        with self.buf_lock:
            self.pending.append((seq, record, ack, key, value))
            self.pending_bytes += len(record)
            should_signal = self.pending_bytes >= self.flush_bytes
        if should_signal:
            self.flush_event.set()
        # Block until the flusher has fsynced and applied us.
        ack.wait()
        return seq

    # ---------- group-commit flusher ----------

    def _flush_loop(self) -> None:
        while not self.stop_flag.is_set():
            # Wait up to flush_interval; the writer may signal early.
            self.flush_event.wait(timeout=self.flush_interval)
            self.flush_event.clear()
            self._flush_once()

    def _flush_once(self) -> None:
        with self.buf_lock:
            if not self.pending:
                return
            batch = list(self.pending)
            self.pending.clear()
            self.pending_bytes = 0

        # 1) Append all records, then ONE fsync covers them.
        big = b"".join(rec for (_seq, rec, _ack, _k, _v) in batch)
        os.write(self.fd, big)
        t_fsync = time.perf_counter()
        os.fsync(self.fd)
        fsync_dt = (time.perf_counter() - t_fsync) * 1e6  # us

        # 2) Apply to Redis in one pipeline. Non-transactional is fine —
        #    the WAL is the source of truth; Redis is the cache.
        last_seq = batch[-1][0]
        with self.r.pipeline(transaction=False) as p:
            for _seq, _rec, _ack, k, v in batch:
                p.set(k, v)
            p.set("__sgc:applied_seq", last_seq)
            p.execute()

        self.last_flushed_seq = last_seq
        self.flush_count += 1
        self.flush_record_count += len(batch)
        self.flush_byte_count += len(big)
        self.fsync_us_total += fsync_dt
        if fsync_dt > self.fsync_us_max:
            self.fsync_us_max = fsync_dt

        # 3) Wake every caller in this batch.
        for _seq, _rec, ack, _k, _v in batch:
            ack.set()

    # ---------- recovery ----------

    @staticmethod
    def replay(wal_path: str, *, port: Optional[int] = None) -> tuple[int, int]:
        """Replay every record in the WAL whose seq > applied_seq.
        Returns (records_seen, records_applied).
        """
        kwargs = {}
        if port is not None:
            kwargs["port"] = port
        r = redis.Redis(**kwargs)
        applied = int(r.get("__sgc:applied_seq") or 0)
        seen = applied_now = 0
        try:
            f = open(wal_path, "rb")
        except FileNotFoundError:
            return 0, 0
        with f:
            while True:
                hdr = f.read(HDR.size)
                if len(hdr) < HDR.size:
                    break
                seq, plen = HDR.unpack(hdr)
                rec = f.read(plen)
                if len(rec) < plen:
                    break  # torn tail
                seen += 1
                if seq <= applied:
                    continue
                first_nl = rec.find(b"\n")
                klen = int(rec[:first_nl])
                key = rec[first_nl + 1 : first_nl + 1 + klen].decode()
                value = rec[first_nl + 1 + klen + 1 :].decode()
                with r.pipeline(transaction=True) as p:
                    p.set(key, value)
                    p.set("__sgc:applied_seq", seq)
                    p.execute()
                applied_now += 1
        return seen, applied_now

    # ---------- telemetry ----------

    def stats(self) -> dict:
        return {
            "flushes": self.flush_count,
            "records_flushed": self.flush_record_count,
            "bytes_flushed": self.flush_byte_count,
            "avg_records_per_flush": self.flush_record_count / max(1, self.flush_count),
            "fsync_us_total": int(self.fsync_us_total),
            "fsync_us_avg": int(self.fsync_us_total / max(1, self.flush_count)),
            "fsync_us_max": int(self.fsync_us_max),
        }
