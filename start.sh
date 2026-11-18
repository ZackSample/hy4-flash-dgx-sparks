#!/usr/bin/env bash
# Tencent Hy4 flash (FP8) across both DGX Sparks with Tencent's vLLM build, tensor-parallel 2.
#   rank 0 here (API on :8888), rank 1 on the second Spark (reads the weights from here over NFS on the cable).
# The four Engram tables (127 GB, BF16) stay in the checkpoint files: patches/engram.py reads each step's rows
# from disk (HY4_ENGRAM_DISK=1), so each box holds only its half of the 160 GB of FP8 weights.
# Settings: environment or ./local.sh. ./stop.sh takes it down. DRY_RUN=1 prints the docker commands.
set -euo pipefail
cd "$(dirname "$0")"
[ -f local.sh ] && . ./local.sh

IMAGE=${IMAGE:-vllm/vllm-openai:hy4-flash}
NAME=${NAME:-hy4-flash}
PORT=${PORT:-8888}
SERVED=${SERVED:-hy4-flash}
HEAD_ADDR=${HEAD_ADDR:-10.100.16.2}
WORKER=${WORKER:-$USER@10.100.16.1}                # ssh target of the second Spark
WORKER_ADDR=${WORKER_ADDR:-10.100.16.1}
MASTER_PORT=${MASTER_PORT:-29561}
LINK_IF=${LINK_IF:-enp1s0f0np0}                   # the QSFP interface joining the two Sparks (same name on both)
IB_HCA=${IB_HCA:-rocep1s0f0,roceP2p1s0f0}         # its RoCE devices (ibv_devices)
IB_GID=${IB_GID:-3}                               # RoCE v2 IPv4 GID index (show_gids)
REPO_DIR=${REPO_DIR:-${HF_HOME:-$HOME/.cache/huggingface}/hub/models--tencent--Hy4-flash-FP8}
WORKER_VOLUME=${WORKER_VOLUME:-hy4-hf}            # docker NFS volume on the second Spark -> REPO_DIR here
CONTEXT=${CONTEXT:-131072}                        # tokens per request
PARALLEL=${PARALLEL:-2}                           # requests at once
GPU_UTIL=${GPU_UTIL:-0.80}                        # share of each box's memory for weights + KV cache
DFLY=${DFLY:-1}                                   # DFly2 drafts (speculative decoding)
LOAD_FORMAT=${LOAD_FORMAT:-}                      # e.g. instanttensor
# CUDA graphs: the default (one full graph per step) is the fast path. The Engram rows are read from disk before
# each step (patches/engram.py), so nothing inside the graph touches the host.
BREAKABLE=${BREAKABLE:-0}
COMPILATION=${COMPILATION:-'{}'}
EXTRA_ARGS=${EXTRA_ARGS:-}
WAIT=${WAIT:-2400}
DRY=${DRY_RUN:-0}

log() { echo "[hy4-flash] $*"; }
die() { echo "[hy4-flash] ERROR: $*" >&2; exit 1; }
w() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$WORKER" "$@"; }

SNAP=$(ls -d "$REPO_DIR"/snapshots/*/ 2>/dev/null | head -1) || true
[ -n "${SNAP:-}" ] || die "no snapshot under $REPO_DIR"
REV=$(basename "$SNAP")
IN=/models/Hy4-flash-FP8                           # where the repo sits inside both containers
MODEL=$IN/snapshots/$REV
have=$(ls "$SNAP" | grep -c '^model-.*safetensors$' || true)
[ "$have" = 148 ] || die "checkpoint incomplete: $have of 148 shards in $SNAP"

PDIR=/usr/local/lib/python3.12/dist-packages/vllm/models
# files in patches/ laid over the image's copies: name=path under vllm/models
PATCHES=(engram.py=hy_v4_flash/nvidia/engram.py attention.py=hy_v4_flash/nvidia/attention.py sm120.py=hy_v4_flash/nvidia/sm120.py hy4_attention.py=hy_v4/nvidia/attention.py)
common=(--gpus all --ipc=host --network host --shm-size 16g --device /dev/infiniband --cap-add IPC_LOCK
  --ulimit memlock=-1 --ulimit stack=67108864
  -e HF_HUB_OFFLINE=1 -e HY4_ENGRAM_DISK=1 -e VLLM_USE_BREAKABLE_CUDAGRAPH="$BREAKABLE"
  -e GLOO_SOCKET_IFNAME="$LINK_IF" -e NCCL_SOCKET_IFNAME="$LINK_IF" -e NCCL_IB_HCA="$IB_HCA" -e NCCL_IB_GID_INDEX="$IB_GID")
serve=("$MODEL" --served-model-name "$SERVED"
  --tensor-parallel-size 2 --nnodes 2 --master-addr "$HEAD_ADDR" --master-port "$MASTER_PORT"
  --kv-cache-dtype fp8_ds_mla
  --max-model-len "$CONTEXT" --max-num-seqs "$PARALLEL" --gpu-memory-utilization "$GPU_UTIL"
  --limit-mm-per-prompt '{"image":4,"video":0}'
  --tool-call-parser hy_v4 --reasoning-parser hy_v4 --enable-auto-tool-choice
  --compilation-config "$COMPILATION")
(( DFLY )) && serve+=(--speculative-config '{"num_speculative_tokens":5,"method":"dfly"}')
[ -n "$LOAD_FORMAT" ] && serve+=(--load-format "$LOAD_FORMAT")
# shellcheck disable=SC2206
[ -n "$EXTRA_ARGS" ] && serve+=($EXTRA_ARGS)

q() { printf '%q ' "$@"; }
rank1="docker run -d --name $NAME $(q "${common[@]}") -e VLLM_HOST_IP=$WORKER_ADDR \
 -v $WORKER_VOLUME:$IN:ro -e HY4_ENGRAM_LOCAL_DIR=/engram -v \$HOME/models/hy4-flash-engram:/engram -v \$HOME/.cache/hy4-flash-vllm:/root/.cache/vllm $(for p in "${PATCHES[@]}"; do printf -- '-v $HOME/projects/hy4-flash-sparks/patches/%s:%s/%s:ro ' "${p%%=*}" "$PDIR" "${p#*=}"; done) \
 $IMAGE $(q "${serve[@]}") --node-rank 1 --headless"
rank0=(docker run -d --name "$NAME" "${common[@]}" -e VLLM_HOST_IP="$HEAD_ADDR"
  -v "$REPO_DIR:$IN:ro" -v "$HOME/.cache/hy4-flash-vllm:/root/.cache/vllm" $(for p in "${PATCHES[@]}"; do printf -- '-v %s/patches/%s:%s/%s:ro ' "$PWD" "${p%%=*}" "$PDIR" "${p#*=}"; done)
  "$IMAGE" "${serve[@]}" --node-rank 0 --host 0.0.0.0 --port "$PORT")

if (( DRY )); then
  echo "rank 1 on $WORKER:"; echo "  $rank1"; echo "rank 0 here:"; echo "  $(q "${rank0[@]}")"; exit 0
fi

if curl -s -o /dev/null --max-time 2 "localhost:$PORT/v1/models" && docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  log "$NAME is already up on :$PORT. ./stop.sh first to restart it."; exit 0
fi
ss -ltn "sport = :$PORT" | grep -q LISTEN && die "port $PORT is already in use: $(ss -ltnp "sport = :$PORT" | tail -n +2)"
w true || die "cannot reach $WORKER"
w "mkdir -p projects/hy4-flash-sparks/patches"
for p in "${PATCHES[@]}"; do scp -q "patches/${p%%=*}" "$WORKER:projects/hy4-flash-sparks/patches/${p%%=*}"; done
mkdir -p logs ~/.cache/hy4-flash-vllm; w "mkdir -p ~/.cache/hy4-flash-vllm ~/models/hy4-flash-engram"   # kernel autotune cache, kept between starts
for where in here worker; do
  if [ $where = here ]; then
    docker logs "$NAME" > "logs/last-rank0.log" 2>&1 || true; docker rm -f "$NAME" >/dev/null 2>&1 || true
  else
    w "docker logs $NAME 2>&1" > "logs/last-rank1.log" 2>/dev/null || true; w "docker rm -f $NAME >/dev/null 2>&1 || true"
  fi
done
sudo -n /usr/local/sbin/drop-caches >/dev/null 2>&1 || true
w 'sudo -n /usr/local/sbin/drop-caches >/dev/null 2>&1 || true'

log "Hy4 flash FP8 ($REV) · TP2 over $HEAD_ADDR <-> $WORKER_ADDR · $PARALLEL x $CONTEXT tokens · port $PORT"
log "rank 0 here"; "${rank0[@]}" >/dev/null
log "rank 1 on $WORKER"; w "$rank1" >/dev/null

log "loading (the 160 GB of weights take a while; the second Spark reads them over the cable)"
t0=$(date +%s)
while :; do
  if curl -s --max-time 3 "localhost:$PORT/v1/models" | grep -q "$SERVED"; then break; fi
  docker ps --format '{{.Names}}' | grep -qx "$NAME" || { docker logs --tail 40 "$NAME" 2>&1; die "rank 0 exited while loading"; }
  w "docker ps --format '{{.Names}}' | grep -qx $NAME" || { w "docker logs --tail 40 $NAME" 2>&1; die "rank 1 exited while loading"; }
  (( $(date +%s) - t0 > WAIT )) && die "not ready after $WAIT s (docker logs $NAME)"
  sleep 5
done
log "ready in $(( $(date +%s) - t0 )) s: http://$(hostname):$PORT/v1 (model: $SERVED)"
