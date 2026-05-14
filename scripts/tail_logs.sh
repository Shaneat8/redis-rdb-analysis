#!/usr/bin/env bash
# Convenience tail wrapper for live experiment logs.
# Usage: ./tail_logs.sh <experiment>
#   experiment ∈ s1, s2, s3, delta, cow
#
# Examples:
#   ./tail_logs.sh s3                # tail latest log under s3/logs/
#   ./tail_logs.sh delta             # tail latest log under delta-rdb/logs/

set -e

case "${1:-}" in
  s1) DIR="2-baseline-experiments/s1-fork-latency/logs" ;;
  s2) DIR="2-baseline-experiments/s2-cow-amplification/logs" ;;
  s3) DIR="2-baseline-experiments/s3-corruption-analysis/logs" ;;
  delta) DIR="3-major-modifications/delta-rdb/logs" ;;
  cow)   DIR="3-major-modifications/cow-write-throttling/logs" ;;
  "")
    echo "Usage: $0 {s1|s2|s3|delta|cow}"
    echo ""
    echo "Available log directories:"
    find . -path './redis' -prune -o -type d -name logs -print 2>/dev/null
    exit 1
    ;;
  *)
    echo "Unknown experiment: $1"
    exit 1
    ;;
esac

if [ ! -d "$DIR" ]; then
  echo "No logs yet at $DIR"
  echo "Run the experiment first."
  exit 1
fi

LATEST=$(ls -t "$DIR"/*.log 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
  echo "No .log files in $DIR yet."
  exit 1
fi

echo "Tailing: $LATEST"
echo "(Ctrl-C to stop)"
echo "---"
tail -f "$LATEST"
