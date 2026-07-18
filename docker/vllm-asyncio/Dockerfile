FROM vllm/vllm-openai@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089

# vLLM 0.25.1 starts the single-process HTTP API with uvloop.run(). On the
# pinned Python 3.12/uvloop 0.22.1 runtime that path can corrupt the API process
# during uvloop timer cleanup. Keep the complete serving stack unchanged and
# replace only the top-level event-loop runner used by `vllm serve`.
RUN set -eu; \
    target=/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/cli/serve.py; \
    printf '%s  %s\n' \
      'abaa0233f6e00ac8acbd528c3cc6a63d6e5ee09e5baedffd922d961eefe91af8' \
      "$target" | sha256sum -c -; \
    test "$(grep -Fxc 'import argparse' "$target")" -eq 1; \
    test "$(grep -Fxc '            uvloop.run(run_server(args))' "$target")" -eq 1; \
    sed -i '/^import argparse$/a import asyncio' "$target"; \
    sed -i \
      's/^            uvloop\.run(run_server(args))$/            asyncio.run(run_server(args))/' \
      "$target"; \
    test "$(grep -Fxc 'import asyncio' "$target")" -eq 1; \
    test "$(grep -Fxc '            asyncio.run(run_server(args))' "$target")" -eq 1; \
    test "$(grep -Fxc '            uvloop.run(run_server(args))' "$target")" -eq 0; \
    python3 -c \
      'import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))' \
      "$target"
