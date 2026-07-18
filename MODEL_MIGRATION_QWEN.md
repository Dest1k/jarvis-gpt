# Local-brain migration → Qwen3.5-VL (unsloth/Qwen3.6-35B-A3B-NVFP4)

Everything is wired; this is the runbook to **launch** the migration. gemma4-turbo stays
the certified default until the Qwen profile is validated live, so nothing changes until
you serve `--profile qwen36-vl`.

The target repo is a **vision-language** model (`Qwen3_5MoeForConditionalGeneration`,
image + video), MoE 35B total / ~3B active, NVFP4 (compressed-tensors) + fp8 KV — fully
resident on the 32 GB 5090, no CPU offload.

## 1. Download the checkpoint (custom downloader, not huggingface_hub)

Multithreaded + segmented + resumable (докачка), verifies SHA256 + size, console
progress. Token is read from `hf_token.txt`. Run from `backend/` (use `--trust-env` if HF
is only reachable through the VPN — the live smoke needed it):

```powershell
cd D:\jarvis-gpt\backend
uv run python -m jarvis_gpt.model_downloader `
  unsloth/Qwen3.6-35B-A3B-NVFP4 `
  D:\jarvis\data\models\qwen3.6-35b-a3b-nvfp4 `
  --token-file D:\jarvis-gpt\hf_token.txt --trust-env
```

Interrupt any time and re-run the same command — it resumes from the byte it stopped at
and re-verifies. `D:\jarvis\data\models` is `model_root`, mounted into the vLLM container
at `/models`, so the profile's `model_dir_name` (`qwen3.6-35b-a3b-nvfp4`) lines up
automatically. ~690 GB free on D: — plenty for the ~25 GB pull.

## 2. Serve it

```powershell
# Build the digest-pinned vLLM 0.25.1 derivative. It changes only the HTTP API's
# top-level runner from uvloop.run() to asyncio.run().
docker build --pull=false `
  -f D:\jarvis-gpt\docker\vllm-asyncio\Dockerfile `
  -t jarvis/vllm-openai:v0.25.1-asyncio-e4f88a8 `
  D:\jarvis-gpt

# stop the current gemma dispatcher, bring the Qwen one up with this profile's env
py -3.11 D:\jarvis-gpt\jarvis.py dispatcher-down
py -3.11 D:\jarvis-gpt\jarvis.py --profile qwen36-vl dispatcher-up
# then the API on the same profile
py -3.11 D:\jarvis-gpt\jarvis.py --profile qwen36-vl serve
```

The compose command is templated from the profile: `--max-model-len 32768
--gpu-memory-utilization 0.90 --kv-cache-dtype fp8 --max-num-seqs 16 --skip-mm-profiling
--mm-processor-cache-gb 4`. Quantization (NVFP4/compressed-tensors) is auto-detected from
the model's `config.json`, so no explicit quant flag is needed.

## 3. Validate

```powershell
py -3.11 D:\jarvis-gpt\jarvis.py --profile qwen36-vl dispatcher-status
py -3.11 D:\jarvis-gpt\jarvis.py --profile qwen36-vl llm-health
py -3.11 D:\jarvis-gpt\jarvis.py --profile qwen36-vl chat "коротко представься"
```

Watch for repeated-token degeneration and OOM in the container logs.

## Tuning knobs (edit the `qwen36-vl` profile in `backend/src/jarvis_gpt/config.py`)

- **Enable reasoning / tool parsing** once base serving is confirmed — set on the
  profile's `VllmExtraArgs`: `reasoning_parser="qwen3"`, `tool_call_parser="hermes"`,
  `enable_auto_tool_choice=True`. (Left OFF by default: an unsupported parser makes vLLM
  fail to start, and Jarvis already coerces raw tool-call dialects itself, so the model
  serves fine without them.)
- **Multimodal bounds**: `limit_mm_per_prompt="image=2,video=1"` to cap inputs per prompt.
- **VRAM**: if the container OOMs, lower `gpu_memory_utilization` (0.90 → 0.85); if there
  is headroom, raise it. `max_num_seqs` trades throughput for memory.
- Restart the dispatcher after any profile edit (no `--reload`).

## Key risks / notes

- **vLLM runtime**: Qwen uses the local
  `jarvis/vllm-openai:v0.25.1-asyncio-e4f88a8` derivative. Its base is pinned by digest to
  vLLM 0.25.1 and its build fails closed unless the upstream `serve.py` SHA256 matches.
  Rebuild the image explicitly after Docker data loss; do not substitute a floating base.
- **Using vision**: this profile makes the model *serve* with vision; wiring image/video
  from the chat composer through the agent to the model is a separate follow-up.
- **Runtime-image rollback**: stop the stack, set
  `$env:JARVIS_VLLM_IMAGE="vllm/vllm-openai:v0.25.1"`, then start `qwen36-vl` again.
  The model and all serving arguments stay unchanged while only the event-loop patch is
  removed. Full profile rollback remains `dispatcher-down` followed by
  `--profile gemma4-turbo`; gemma is untouched and stays the certified default.
