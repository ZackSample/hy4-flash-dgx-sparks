#!/usr/bin/env bash
# Stop Hy4 flash on both Sparks; each rank's log is kept in logs/.
cd "$(dirname "$0")"
[ -f local.sh ] && . ./local.sh
NAME=${NAME:-hy4-flash}
WORKER=${WORKER:-$USER@10.100.16.1}
mkdir -p logs
docker logs "$NAME" > logs/last-rank0.log 2>&1
docker rm -f "$NAME" >/dev/null 2>&1 && echo "[hy4-flash] rank 0 stopped"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$WORKER" "docker logs $NAME 2>&1" > logs/last-rank1.log 2>/dev/null
ssh -o BatchMode=yes -o ConnectTimeout=8 "$WORKER" "docker rm -f $NAME >/dev/null 2>&1" && echo "[hy4-flash] rank 1 stopped"
exit 0
