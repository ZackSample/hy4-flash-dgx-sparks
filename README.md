# Hy4 flash FP8 on two DGX Sparks

Tencent's [Hy4 flash](https://huggingface.co/tencent/Hy4-flash) (220B parameters, about 12B active per token, text
and images in) served across two DGX Sparks from Tencent's own FP8 checkpoint
([tencent/Hy4-flash-FP8](https://huggingface.co/tencent/Hy4-flash-FP8), 268 GB) and Tencent's vLLM image
(`vllm/vllm-openai:hy4-flash`), tensor-parallel 2 over the QSFP link.

Tencent's recipe is for 8 data-centre GPUs. Out of the box the image does not start on a Spark, and the checkpoint
does not fit in 2 x 128 GB. The four files in `patches/` are what make it run. It works, with vision, tool calls,
the DFly2 drafter and 100k-token prompts, but it is a first boot and not a tuned one. **The point of this repo is
for people to make it faster**; the list of what is known to be slow is at the bottom.

Measured on 2026-11-18, one day after the model came out.

| | |
|---|---|
| Decode, prose | 15 tok/s |
| Decode, code | 30 tok/s |
| Decode, while thinking | 23 tok/s |
| Prefill | 810 to 1,050 tok/s (108k-token prompt in 104 s) |
| Time to first token, 16k prompt already in cache | 1.5 s |
| Two requests at once | 21 tok/s in total |
| Weights in memory | 77 GB on each Spark |
| KV cache at `GPU_UTIL=0.80` | about 445k tokens with the drafter, 547k without |
| Start | about 10 minutes |

## What the patches do

The files are mounted over the image's copies under `vllm/models/`. The image is vLLM `0.1.dev25463+g77766db1f`.
The `.diff` files show each change against the image's file; `sm120.py` is new.

| file | laid over | why |
|---|---|---|
| `engram.py` | `hy_v4_flash/nvidia/engram.py` | **Engram tables on disk.** The four n-gram tables are 127 GB of BF16 (shards 1 to 4) that vLLM pins in host RAM, which on a Spark is also the GPU's memory. With `HY4_ENGRAM_DISK=1` they stay in the checkpoint files. Before each step, the rows that step needs are read with `pread` and copied into fixed GPU buffers (`HY4DiskEngramState.prepare_inputs`), so the captured CUDA graph only does a GPU copy. Each rank reads only its own heads. A rank given `HY4_ENGRAM_LOCAL_DIR` writes its rows to a local file once (63 GB) and reads from there, which is what the second Spark does in place of reading over NFS. |
| `attention.py`, `sm120.py` | `hy_v4_flash/nvidia/` | **Attention on GB10.** Hy4 flash is wired to FlashMLA, which exists only for Hopper and data-centre Blackwell. The Spark's GB10 is sm_121. Attention goes to FlashInfer's SM120 sparse-MLA kernel, which already takes Hy4's per-head sinks. The sliding-window layers run through the same kernel as index lists over whole `fp8_ds_mla` KV rows, with no page eviction, so the KV cache costs more than Tencent's figures. |
| `hy4_attention.py` | `hy_v4/nvidia/attention.py` | The same kernel choice for the DFly2 draft model, which refuses to start without a backend that applies sinks. |

## Set up

Two DGX Sparks joined by a QSFP cable with RoCE working, passwordless ssh from the head to the second one, Docker
with the NVIDIA runtime on both. The scripts assume the cable addresses `10.100.16.2` (head) and `10.100.16.1`.

1. **Image on both Sparks.** `docker pull vllm/vllm-openai:hy4-flash` on each, or pull once and
   `docker save vllm/vllm-openai:hy4-flash | ssh <second> docker load`.
2. **Weights on the head.** `./fetch.sh` (needs the `hf` CLI) downloads the FP8 checkpoint and restarts the
   download when it stalls. 268 GB.
3. **Share the weights to the second Spark** read-only over the cable. On the head, add to `/etc/exports` and run
   `sudo exportfs -ra`:

   ```
   <HF_HOME>/hub/models--tencent--Hy4-flash-FP8 10.100.16.1(ro,no_subtree_check)
   ```

   On the second Spark:

   ```bash
   docker volume create --driver local --opt type=nfs --opt o=addr=10.100.16.2,nfsvers=4.2,ro,nconnect=8,rsize=1048576,wsize=1048576,hard,timeo=600 --opt device=:<HF_HOME>/hub/models--tencent--Hy4-flash-FP8 hy4-hf
   ```

4. **Settings.** Defaults are at the top of `start.sh`. Put what differs in `local.sh` next to it, for example:

   ```bash
   WORKER=me@10.100.16.1          # ssh target of the second Spark
   HF_HOME=/data/huggingface      # if the weights are not under ~/.cache/huggingface
   LINK_IF=enp1s0f0np0            # the QSFP interface; IB_HCA and IB_GID are its RoCE devices and GID index
   ```

## Run it

```bash
./start.sh     # rank 0 here, rank 1 on the second Spark; waits until the API answers
./stop.sh
./err.sh       # root cause of a failed start (./err.sh 1 for the second Spark)
DRY_RUN=1 ./start.sh   # print the two docker commands and do nothing
```

The API is OpenAI-compatible on port 8888, model name `hy4-flash`. Both Sparks must be nearly empty. The first start
is slower: the second Spark writes its 63 GB Engram file and both autotune kernels (cached afterwards in
`~/.cache/hy4-flash-vllm`).

Other settings: `CONTEXT` (tokens per request, default 131072), `PARALLEL` (requests at once, default 2),
`GPU_UTIL` (0.80), `DFLY` (1 = DFly2 drafter on, 5 draft tokens), `PORT`.

Thinking is set per request: `"chat_template_kwargs": {"reasoning_effort": "no_think"}` or `"high"` (the default).
At `high` it can think past a 6,000-token limit on problems it solves in a few hundred tokens with thinking off.

## Things that cost a boot each to learn

- `GLOO_SOCKET_IFNAME` must name the cable interface, or the ranks tell each other to connect to 127.0.0.1.
- `VLLM_USE_RUST_FRONTEND=1` (in Tencent's recipe) gives up while the engine is still autotuning kernels.
- The SM120 kernel needs `--kv-cache-dtype fp8_ds_mla`, and sliding-window MLA refuses that KV type, hence the
  index-list route for the window layers.
- A host read inside the model's forward fails CUDA graph capture. Piecewise or breakable graphs get past that but
  run the attention indexer eagerly at every layer (about 8 tok/s). Reading before the step keeps the full graph.
- Reading rows through the checkpoint's mmap is about 12 times slower than `pread` with read-ahead off.

## Benchmark

`bench/bench.py` (standard library only) measures speed and runs a small ability set: reasoning, coding against
hidden tests in a sandboxed container, strict JSON, tool calls, vision, long context, instruction following.
`bench/compare.py` builds the table. `bench/results/compare.md` has this setup against GLM 5.3 Flash (EXL3 4bpw on
TensorFold, same two Sparks) and Claude Haiku 5.5 (answers written offline). The set is easy: all three are near
the ceiling, so read it as a smoke test, not a ranking. For scale, GLM on TensorFold decodes at 52 to 72 tok/s on
the same hardware.

## Where the time goes, and what to try

A profile of rank 0 after these patches: the disk read is about a tenth of a step; the rest is GPU work, the
drafter, and the cross-box all-reduce.

- **The cross-box link is untuned.** vLLM uses stock NCCL over RoCE. Hy4 does an all-reduce per layer plus one for
  each Engram layer.
- **A second request barely adds throughput** (21 tok/s for two against 15 for one). Not investigated.
- **Expert parallel is untried.** Another recipe reports it working with this image (see below).
- **InstantTensor loading is untried** (`LOAD_FORMAT=instanttensor`); its loader would need the Engram shards kept on
  the CPU path. Loading the weights is the smaller part of the 10-minute start; kernel warm-up is the larger.
- **The Engram tables could be FP8 with a scale per row**, halving the disk and the second Spark's copy. vLLM's
  loader has the code path for it.
- **The window layers keep whole KV rows.** A real sliding-window path on sm_12x would give back KV memory.
- `start.sh`'s wait loop once reported a missing container while both were fine.

## Related

[atakann/dgx-spark-recipes](https://github.com/atakann/dgx-spark-recipes) (`llm/hy4-flash-nvfp4-vllm-tp2`) runs an
NVFP4 build of the same model on the same image with a similar attention change, and reports 36 to 38 tok/s on
code with the Engram tables kept in memory as FP8. If you want the fastest Hy4 flash on two Sparks today, look
there first. This repo is the route that runs Tencent's unmodified FP8 weights.

## Licence

The files in `patches/` are modified copies of vLLM source files (Apache-2.0, copyright the vLLM contributors),
taken from Tencent's image. Everything else here is under the same licence. The model has its own:
[Tencent Hy License](https://huggingface.co/tencent/Hy4-flash/blob/main/LICENSE).
