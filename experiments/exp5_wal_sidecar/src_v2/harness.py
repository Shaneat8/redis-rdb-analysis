"""Harness for in-tree group-commit AOF experiments."""
import os, signal, socket, subprocess, time
from contextlib import contextmanager

REDIS_BIN = os.environ.get(
    "MODIFIED_REDIS",
    "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/"
    "exp5_wal_sidecar/bin/redis-server-modified",
)
CFG_DIR = "/sessions/tender-festive-rubin/mnt/redis-rdb-analysis/experiments/exp5_wal_sidecar/config_v2"
PORTS = {"everysec": 17600, "always": 17601, "groupcommit": 17602}


def _wait_port(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


@contextmanager
def redis_server(mode, log_path=None, wipe=True):
    cfg = os.path.join(CFG_DIR, f"{mode}.conf")
    port = PORTS[mode]
    data_dir = f"/tmp/exp5_v2_{mode}"
    if wipe:
        subprocess.run(["rm", "-rf", data_dir], check=False)
    os.makedirs(data_dir, exist_ok=True)
    log_fp = open(log_path, "w") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(
        [REDIS_BIN, cfg],
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    try:
        if not _wait_port(port, timeout=5):
            raise RuntimeError(f"redis on port {port} did not come up")
        yield proc, port
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)
        if log_fp not in (subprocess.DEVNULL, None):
            log_fp.close()


def kill9(proc):
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
