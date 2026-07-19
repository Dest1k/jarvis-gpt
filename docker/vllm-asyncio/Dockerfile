FROM vllm/vllm-openai:v0.25.1@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089

# vLLM 0.25.1 starts the single-process HTTP API with uvloop.run(). On the
# pinned Python 3.12/uvloop 0.22.1 runtime that path can corrupt the API process
# during uvloop timer cleanup. Keep the complete serving stack unchanged and
# replace only the required import and top-level event-loop runner. The patcher
# verifies the complete upstream file hash before producing deterministic bytes.
COPY docker/vllm-asyncio/patch_serve.py /usr/local/share/jarvis/patch_vllm_serve.py
RUN ["python3", "/usr/local/share/jarvis/patch_vllm_serve.py", "/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/cli/serve.py"]
