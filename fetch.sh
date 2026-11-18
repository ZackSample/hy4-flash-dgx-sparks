#!/usr/bin/env bash
# Download tencent/Hy4-flash-FP8 (268 GB) and restart the downloader whenever it stalls.
# Plain resumable HTTP: with xet, a restart threw away the partly downloaded 30 GB shards.
cd "$(dirname "$0")"
[ -f local.sh ] && . ./local.sh
export HF_HOME=${HF_HOME:-$HOME/.cache/huggingface} HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=30
D=$HF_HOME/hub/models--tencent--Hy4-flash-FP8
IF=${NET_IF:-$(ip route show default | awk '{print $5; exit}')}     # interface whose traffic shows a stall
MIN_BPS=${MIN_BPS:-5000000}
mkdir -p logs; LOG=logs/download.log
rx() { awk -v i="$IF:" '$1==i{print $2}' /proc/net/dev; }
while :; do
  n=$(ls $D/snapshots/*/ 2>/dev/null | grep -c '^model-.*safetensors$')
  [ "$n" = 148 ] && ! pgrep -x hf >/dev/null && { echo "$(date +%T) complete: 148 shards" >> $LOG; exit 0; }
  if ! pgrep -x hf >/dev/null; then
    echo "$(date +%T) starting downloader ($n/148 shards done)" >> $LOG
    setsid hf download tencent/Hy4-flash-FP8 --max-workers 16 --exclude "assets/*" --exclude "finetune/*" >> $LOG 2>&1 &
    sleep 60
  fi
  a=$(rx); sleep 60; b=$(rx)
  if (( (b-a)/60 < MIN_BPS )) && pgrep -x hf >/dev/null; then
    echo "$(date +%T) stalled ($(( (b-a)/60/1000000 )) MB/s): restarting" >> $LOG
    pkill -x hf; sleep 3
  fi
done
