#!/usr/bin/env python3
"""S3 — RDB corruption / load-failure classification.

For each structural site (magic, AUX, SELECTDB, RESIZEDB len, key str len,
value mid, EOF, CRC), flip one byte at 20 random offsets within that site
and try to load the result. Classify each outcome.

Site offset detection: we parse the golden RDB ourselves with a minimal
walker to find the byte ranges. We don't depend on Redis source instrumentation.
"""
import argparse, json, os, random, re, shutil, socket, struct
import subprocess, tempfile, time
from pathlib import Path
import redis

REPO_ROOT = Path(__file__).resolve().parents[3]
STOCK_BIN = REPO_ROOT / "redis" / "src" / "redis-server"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def start_redis_load(rdb_path, site, rep, timeout=30):
    """Boot a fresh Redis pointed at rdb_path. Return (outcome_class, log_tail, dbsize).

    Persists the redis-server.log for this trial under ../logs/redis-<site>-<rep>.log
    so it survives after the tmpdir is removed.
    """
    tmp = tempfile.mkdtemp(prefix="s3-")
    shutil.copy(rdb_path, Path(tmp) / "dump.rdb")
    log_path = LOGS_DIR / f"redis-{site}-{rep}.log"
    port = free_port()
    proc = subprocess.Popen(
        [str(STOCK_BIN), "--port", str(port), "--dir", tmp,
         "--save", "", "--appendonly", "no", "--daemonize", "no",
         "--protected-mode", "no", "--loglevel", "notice",
         "--logfile", str(log_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = redis.Redis(port=port, socket_timeout=0.5); r.ping()
            ready = True
            break
        except Exception:
            time.sleep(0.1)
    dbsize = None
    log_tail = ""
    if ready:
        try: dbsize = r.dbsize()
        except Exception: dbsize = None
        try: r.shutdown(nosave=True)
        except Exception: pass
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)
    if log_path.exists():
        log_tail = log_path.read_text(errors="ignore")[-2000:]
    shutil.rmtree(tmp, ignore_errors=True)
    exit_code = proc.returncode

    # Classify
    if ready and dbsize is not None:
        outcome = "SILENT_LOAD"
    elif "Wrong RDB checksum" in log_tail or "checksum mismatch" in log_tail.lower():
        outcome = "CRC_FAIL"
    elif ("Wrong signature" in log_tail or
          "Unknown RDB" in log_tail or
          "Bad data format" in log_tail or
          "RDB file was truncated" in log_tail or
          "Short read" in log_tail or
          "Can't handle RDB" in log_tail or
          "Internal error in RDB reading" in log_tail or
          "Unknown length encoding" in log_tail or
          "Terminating server after rdb file reading failure" in log_tail):
        outcome = "PARSE_ABORT"
    elif exit_code is not None and exit_code < 0:
        outcome = "CRASH"
    else:
        outcome = "OTHER"
    return outcome, log_tail, dbsize, exit_code


def make_golden(out_path):
    """Generate a small mixed-type RDB by booting Redis, loading data, SAVEing."""
    tmp = tempfile.mkdtemp(prefix="s3-golden-")
    port = free_port()
    proc = subprocess.Popen(
        [str(STOCK_BIN), "--port", str(port), "--dir", tmp,
         "--save", "", "--appendonly", "no", "--daemonize", "no",
         "--protected-mode", "no", "--loglevel", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            r = redis.Redis(port=port, socket_timeout=0.1); r.ping(); break
        except Exception: time.sleep(0.1)
    pipe = r.pipeline(transaction=False)
    for i in range(2000):
        pipe.set(f"s:{i}", f"v{i}" + "x" * (i % 32))
    for i in range(500):
        pipe.hset(f"h:{i}", mapping={f"f{j}": f"v{j}" for j in range(5)})
    for i in range(500):
        pipe.rpush(f"l:{i}", *[f"e{j}" for j in range(5)])
    pipe.execute()
    r.save()
    shutil.copy(Path(tmp) / "dump.rdb", out_path)
    r.shutdown(nosave=True)
    proc.wait(timeout=10)
    shutil.rmtree(tmp, ignore_errors=True)


def site_ranges(rdb_bytes):
    """Best-effort byte ranges for each structural site.

    For a small RDB the parsing is straightforward but error-prone for
    multi-DB or module-laden files. We hand-pick the easy ones:
      magic:     [0, 9)
      EOF:       last 9 bytes (1 opcode + 8 CRC)
      CRC:       last 8 bytes
      AUX:       [9, first 0xFE byte)              (rough)
      SELECTDB:  index of 0xFE                     (single byte)
      RESIZEDB:  bytes immediately after SELECTDB+dbid  (2-10 bytes)
      key_str_len: a few sample length-prefix bytes inside the keyspace
      value_mid: middle bytes of a chosen value payload
    """
    n = len(rdb_bytes)
    sel = rdb_bytes.find(b'\xfe', 9)
    return {
        "magic":        (0, 9),
        "AUX":          (9, sel if sel > 9 else 256),
        "SELECTDB":     (sel, sel + 1) if sel > 0 else (9, 10),
        "RESIZEDB_len": (sel + 2, sel + 12) if sel > 0 else (20, 30),
        "key_str_len":  (sel + 12, sel + 64) if sel > 0 else (40, 80),
        "value_mid":    (n // 2, n // 2 + 64),    # mid-file payload
        "EOF":          (n - 9, n - 8),
        "CRC":          (n - 8, n),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../results/raw.jsonl")
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    if args.quick: args.reps = 3

    workdir = Path(tempfile.mkdtemp(prefix="s3-work-"))
    golden = workdir / "golden.rdb"
    make_golden(golden)
    golden_bytes = golden.read_bytes()
    sites = site_ranges(golden_bytes)
    print("sites:", {k: (a, b, b - a) for k, (a, b) in sites.items()})

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        for site, (lo, hi) in sites.items():
            if hi <= lo: continue
            for rep in range(args.reps):
                off = random.randint(lo, hi - 1)
                xor_byte = random.randint(1, 255)
                corrupted = bytearray(golden_bytes)
                corrupted[off] ^= xor_byte
                cpath = workdir / f"corrupt-{site}-{rep}.rdb"
                cpath.write_bytes(bytes(corrupted))
                outcome, log_tail, dbsize, exit_code = start_redis_load(cpath, site, rep)
                row = {
                    "site": site, "rep": rep, "offset": off,
                    "xor": xor_byte, "outcome": outcome,
                    "dbsize": dbsize, "exit_code": exit_code,
                    "log_tail_excerpt": log_tail[-500:],
                    "ts": time.time(),
                }
                f.write(json.dumps(row) + "\n"); f.flush()
                print(f"site={site} rep={rep} off={off} => {outcome}")
                cpath.unlink()


if __name__ == "__main__":
    main()
