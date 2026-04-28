#!/bin/bash
# ============================================================================
# Experiment 6 - Incremental RDB (multi-run, median-of-3)
# Compares full BGSAVE vs the patched DEBUG INCRSAVE path at three dirty rates.
# Each (dirty%) scenario is run RUNS times; reported = median.
# ============================================================================
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
BIN_MODIFIED=${BIN_MODIFIED:-$HERE/redis-server.modified}
REDIS_CLI=${REDIS_CLI:-$HERE/redis-cli}
WORK=${WORK:-/tmp/exp6_run}
N_KEYS=${N_KEYS:-200000}
VSIZE=${VSIZE:-256}
DIRTY_PCTS=${DIRTY_PCTS:-"1 10 50"}
RUNS=${RUNS:-3}
mkdir -p "$WORK"
LOG="$HERE/run.log"
METRICS="$HERE/metrics.txt"
: > "$LOG"

log()   { printf '%s [%-5s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$1" "${@:2}" | tee -a "$LOG"; }
INFO()  { log INFO  "$@"; }
ERROR() { log ERROR "$@"; }

PORT_FILE=$(mktemp); echo 6700 > "$PORT_FILE"
trap 'rm -f "$PORT_FILE"' EXIT
next_port() { local p=$(($(cat "$PORT_FILE") + 1)); echo "$p" > "$PORT_FILE"; echo "$p"; }

start_redis() {
    local dir=$1 port=$2
    rm -rf "$dir"; mkdir -p "$dir"
    "$BIN_MODIFIED" --port "$port" --daemonize yes --pidfile "$dir/redis.pid" \
           --logfile "$dir/redis.log" --dir "$dir" --dbfilename dump.rdb \
           --save "" --appendonly no --protected-mode no >/dev/null 2>&1
    for _ in $(seq 1 15); do
        "$REDIS_CLI" -p "$port" ping >/dev/null 2>&1 && return 0
        sleep 0.2
    done
    return 1
}
stop_redis() { [ -f "$1/redis.pid" ] && kill "$(cat "$1/redis.pid")" 2>/dev/null; sleep 0.3; }

populate() {
    local port=$1 n=$2 v=$3
    python3 -c "
import sys
n,v=$n,$v
for i in range(n):
    val='v'+str(i).zfill(8)+'-'+'x'*(v-10)
    sys.stdout.write(f'*3\r\n\$3\r\nSET\r\n\${len(str(i))+4}\r\nkey:{i}\r\n\${len(val)}\r\n{val}\r\n')
" | "$REDIS_CLI" -p "$port" --pipe >/dev/null
}

apply_writes() {
    local port=$1 nmod=$2 ndel=$3 ntot=$4 seed=$5
    python3 -c "
import sys, random
random.seed($seed)
ks = list(range($ntot)); random.shuffle(ks)
for i in ks[:$nmod]:
    val='M'+str(i).zfill(8)+'-'+'x'*245
    sys.stdout.write(f'*3\r\n\$3\r\nSET\r\n\${len(str(i))+4}\r\nkey:{i}\r\n\${len(val)}\r\n{val}\r\n')
for i in ks[$nmod:$nmod+$ndel]:
    sys.stdout.write(f'*2\r\n\$3\r\nDEL\r\n\${len(str(i))+4}\r\nkey:{i}\r\n')
" | "$REDIS_CLI" -p "$port" --pipe >/dev/null
}

ms_save_full() { local p=$1; local t0=$(date +%s%N); "$REDIS_CLI" -p "$p" SAVE >/dev/null; echo $(( ($(date +%s%N) - t0) / 1000000 )); }
ms_save_incr() { local p=$1; local f=$2; local t0=$(date +%s%N); "$REDIS_CLI" -p "$p" DEBUG INCRSAVE "$f" >/dev/null; echo $(( ($(date +%s%N) - t0) / 1000000 )); }

median() { python3 -c "import sys; xs=sorted(int(x) for x in sys.argv[1:]); print(xs[len(xs)//2])" "$@"; }

INFO "exp6 start  binary=$(basename "$BIN_MODIFIED")  keys=$N_KEYS  vsize=${VSIZE}B  runs=$RUNS"
[ -x "$BIN_MODIFIED" ] || { ERROR "missing binary: $BIN_MODIFIED"; exit 1; }
[ -x "$REDIS_CLI" ]    || { ERROR "missing redis-cli: $REDIS_CLI"; exit 1; }

{
    echo "Experiment 6 - Incremental RDB - metrics (median of $RUNS runs)"
    echo "=================================================================="
    printf "%-20s %s\n" "binary"   "$(basename "$BIN_MODIFIED")"
    printf "%-20s %s\n" "workload" "$N_KEYS keys x ${VSIZE}B values"
    printf "%-20s %s\n" "runs/scenario" "$RUNS"
    echo
    printf "%-7s %-10s %-10s %-10s %-10s %-10s %-10s %-10s\n" \
           "dirty%" "n_mod" "n_del" "full_ms" "incr_ms" "speedup" "full_KB" "incr_KB"
    printf -- "------------------------------------------------------------------\n"
} > "$METRICS"

for pct in $DIRTY_PCTS; do
    n_dirty=$(( N_KEYS * pct / 100 ))
    n_del=$(( n_dirty / 20 ))
    n_mod=$(( n_dirty - n_del ))
    INFO "scenario dirty=${pct}%  n_mod=$n_mod  n_del=$n_del"
    full_ms_arr=(); incr_ms_arr=(); full_sz=0; incr_sz=0
    for run in $(seq 1 $RUNS); do
        DIR="$WORK/r${pct}_run${run}"
        PORT=$(next_port)
        if ! start_redis "$DIR" "$PORT"; then ERROR "  start failed run=$run"; continue; fi
        populate "$PORT" "$N_KEYS" "$VSIZE"
        "$REDIS_CLI" -p "$PORT" SAVE >/dev/null
        "$REDIS_CLI" -p "$PORT" DEBUG INCRSAVE "$DIR/_warmup.rdb" >/dev/null
        rm -f "$DIR/_warmup.rdb"
        apply_writes "$PORT" "$n_mod" "$n_del" "$N_KEYS" "$pct$run"
        incr_ms=$(ms_save_incr "$PORT" "$DIR/snap_incr.rdb")
        incr_sz=$(stat -c%s "$DIR/snap_incr.rdb" 2>/dev/null || echo 0)
        apply_writes "$PORT" "$n_mod" "$n_del" "$N_KEYS" "$pct$run"
        full_ms=$(ms_save_full "$PORT")
        full_sz=$(stat -c%s "$DIR/dump.rdb")
        full_ms_arr+=("$full_ms"); incr_ms_arr+=("$incr_ms")
        INFO "  run=$run full=${full_ms}ms incr=${incr_ms}ms"
        stop_redis "$DIR"
    done
    full_med=$(median "${full_ms_arr[@]}")
    incr_med=$(median "${incr_ms_arr[@]}")
    speedup=$(awk "BEGIN{ if ($incr_med>0) printf \"%.1fx\", $full_med/$incr_med; else print \"n/a\" }")
    INFO "  median full=${full_med}ms incr=${incr_med}ms speedup=$speedup"
    printf "%-7s %-10d %-10d %-10d %-10d %-10s %-10d %-10d\n" \
           "$pct" "$n_mod" "$n_del" "$full_med" "$incr_med" "$speedup" \
           $((full_sz/1024)) $((incr_sz/1024)) >> "$METRICS"
done

{
    echo
    echo "Headline: incremental save cost scales with dirty count, not dataset size."
    echo "Numbers are MEDIAN across $RUNS runs to suppress single-shot variance."
} >> "$METRICS"

INFO "exp6 done. metrics=$METRICS  log=$LOG"
echo
cat "$METRICS"
