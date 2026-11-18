#!/usr/bin/env bash
# Print the root-cause lines of the last failed start: ./err.sh for rank 0 here, ./err.sh 1 for the second Spark.
cd "$(dirname "$0")"
[ -f local.sh ] && . ./local.sh
NAME=${NAME:-hy4-flash}
WORKER=${WORKER:-$USER@10.100.16.1}
if [ "${1:-0}" = 1 ]; then src() { ssh -o BatchMode=yes "$WORKER" "docker logs $NAME 2>&1"; }; else src() { docker logs "$NAME" 2>&1; }; fi
src | grep "multiproc_executor.py:1091\]" | sed 's/.*1091\] //' | grep -v '^ *\^' | tail -${2:-14} | cut -c1-260
