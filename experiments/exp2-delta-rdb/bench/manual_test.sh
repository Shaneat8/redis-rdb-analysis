#!/usr/bin/env bash
# Delta RDB manual end-to-end test.
#
# Loads 50K keys, snapshots, restarts, churns 1%, takes a delta snapshot,
# compares sizes. Logs everything for post-mortem.
#
# Usage:  ./manual_test.sh
# Output: HEADLINE COMPARISON at end + paths to log files.

set -u   # error on unset vars
PORT=16400
N_KEYS=50000
VALUE_SIZE=1024
CHURN_KEYS=500
DELETE_KEYS=50

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
REDIS="${REPO_ROOT}/redis-modified/src/redis-server"
CLI="${REPO_ROOT}/redis-modified/src/redis-cli"
BENCH="${REPO_ROOT}/redis-modified/src/redis-benchmark"

if [ ! -x "$REDIS" ]; then
    echo "ERROR: redis-server not found at $REDIS"; exit 1
fi

# Fresh tmpdir
TMPDIR=$(mktemp -d -t delta-rdb-XXXX)
echo "Working in: $TMPDIR"

cleanup() {
    "$CLI" -p $PORT SHUTDOWN NOSAVE 2>/dev/null
    pkill -f "redis-server.*$PORT" 2>/dev/null
    sleep 0.5
}
trap cleanup EXIT

pkill -f redis-server 2>/dev/null
sleep 1

# ============================================================
echo ""
echo "=== STEP 1: boot fresh Redis ==="
# ============================================================
"$REDIS" --port $PORT --daemonize yes --save "" \
    --dir "$TMPDIR" \
    --logfile "$TMPDIR/redis.log"
sleep 1
"$CLI" -p $PORT PING

# ============================================================
echo ""
echo "=== STEP 2: load $N_KEYS keys (base dataset) ==="
# ============================================================
"$BENCH" -p $PORT -t set -n $N_KEYS -r $N_KEYS \
    -d $VALUE_SIZE -c 50 -P 100 -q

# ============================================================
echo ""
echo "=== STEP 3: dirty stats before save (should show all $N_KEYS) ==="
# ============================================================
"$CLI" -p $PORT DEBUG INCRSTATS

# ============================================================
echo ""
echo "=== STEP 4: full baseline snapshot via SAVE ==="
# ============================================================
"$CLI" -p $PORT SAVE
ls -la "$TMPDIR/dump.rdb"
FULL_SIZE=$(stat -c%s "$TMPDIR/dump.rdb")
echo "FULL_SIZE = $FULL_SIZE bytes"

# ============================================================
echo ""
echo "=== STEP 5: restart Redis (loads from dump.rdb, dirty resets) ==="
# ============================================================
"$CLI" -p $PORT SHUTDOWN NOSAVE
sleep 1
"$REDIS" --port $PORT --daemonize yes --save "" \
    --dir "$TMPDIR" \
    --logfile "$TMPDIR/redis.log"
sleep 2

DBSIZE=$("$CLI" -p $PORT DBSIZE | tr -d '\r')
echo "After restart: DBSIZE=$DBSIZE"
"$CLI" -p $PORT DEBUG INCRSTATS

# ============================================================
echo ""
echo "=== STEP 6: apply 1% churn — $CHURN_KEYS SETs + $DELETE_KEYS DELs ==="
# ============================================================
{
    for i in $(seq 1 $CHURN_KEYS); do
        echo "SET key:$i \"modified_value_$i\""
    done
    for i in $(seq 1000 $((999 + DELETE_KEYS))); do
        echo "DEL key:$i"
    done
} | "$CLI" -p $PORT --pipe > /dev/null

# ============================================================
echo ""
echo "=== STEP 7: dirty stats after churn ==="
# ============================================================
"$CLI" -p $PORT DEBUG INCRSTATS

# ============================================================
echo ""
echo "=== STEP 8: incremental snapshot ==="
# ============================================================
RESP=$("$CLI" -p $PORT DEBUG INCRSAVE "$TMPDIR/delta.rdb" | tr -d '\r')
echo "DEBUG INCRSAVE -> $RESP"

if [ ! -f "$TMPDIR/delta.rdb" ]; then
    echo "FAIL: delta.rdb not produced"
    echo "Last 30 log lines:"
    tail -30 "$TMPDIR/redis.log"
    exit 1
fi

ls -la "$TMPDIR/delta.rdb"
DELTA_SIZE=$(stat -c%s "$TMPDIR/delta.rdb")
echo "DELTA_SIZE = $DELTA_SIZE bytes"

# ============================================================
echo ""
echo "===================================================="
echo "    HEADLINE COMPARISON"
echo "===================================================="
echo "  Full snapshot ($N_KEYS keys):              $FULL_SIZE bytes"
echo "  Delta snapshot (~$CHURN_KEYS dirty + $DELETE_KEYS del): $DELTA_SIZE bytes"
if [ "$DELTA_SIZE" -gt 0 ]; then
    RATIO=$(awk "BEGIN{ printf \"%.1f\", $FULL_SIZE / $DELTA_SIZE }")
    echo "  Reduction:                              ${RATIO}x smaller"
fi
echo "===================================================="
echo ""
echo "Log:   $TMPDIR/redis.log"
echo "Full:  $TMPDIR/dump.rdb"
echo "Delta: $TMPDIR/delta.rdb"
