#!/usr/bin/env bash
# Safe, repeatable Docker cleanup for the Jarvis box.
#
# Removes cruft — dangling images, stopped containers, build cache, unreferenced
# volumes, and any tagged image that is NOT in the derived keep-set — while protecting
# the images the project actually needs:
#   (a) images used by any container (running or stopped),
#   (b) the vLLM serving images declared by the runtime profiles in config.py
#       (so it stays in sync automatically as profiles change),
#   (c) the compose-built stack images, and
#   (d) a small explicit list of build-base images.
#
# Dry-run by DEFAULT — it only prints what it would remove. Pass --apply to delete.
#
# Usage (run it yourself via the `!` prefix; `!` runs in this session):
#   ! bash D:/jarvis-gpt/scripts/docker_cleanup.sh            # preview
#   ! bash D:/jarvis-gpt/scripts/docker_cleanup.sh --apply    # actually clean
#
# Safety: if the profile-image lookup fails, NO vllm/* image is ever removed.
set -uo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

REPO_SRC="D:/jarvis-gpt/backend/src"

# Build-base images worth keeping so `docker compose build` stays offline-fast.
EXTRA_KEEP=("python:3.12-slim" "alpine:latest")

declare -A KEEP
config_ok=1

# (a) images referenced by any container (running or stopped) — docker refuses to
#     `rmi` these anyway, but list them so they show up under KEEP for clarity.
while IFS= read -r img; do [ -n "$img" ] && KEEP["$img"]=1; done \
  < <(docker ps -a --format '{{.Image}}' 2>/dev/null | tr -d '\r')

# (b) vLLM images declared by the project's runtime profiles.
prof_imgs="$(py -3.11 -c "import sys; sys.path.insert(0, r'$REPO_SRC'); from jarvis_gpt.config import PROFILES; print(chr(10).join(sorted({p.vllm_image for p in PROFILES.values()})))" 2>/dev/null | tr -d '\r')"
if [ -z "$prof_imgs" ]; then
  config_ok=0
  echo "WARN: could not read profile images from config.py — protecting ALL vllm/* images."
fi
while IFS= read -r img; do [ -n "$img" ] && KEEP["$img"]=1; done <<< "$prof_imgs"

# (c) compose-built stack images.
KEEP["jarvis-gpt-backend:latest"]=1
KEEP["jarvis-gpt-frontend:latest"]=1

# (d) build bases.
for img in "${EXTRA_KEEP[@]}"; do KEEP["$img"]=1; done

echo "== KEEP (protected) =="
for k in "${!KEEP[@]}"; do echo "  $k"; done | sort

# Compute the tagged images to remove.
REMOVE=()
while IFS= read -r img; do
  [ -z "$img" ] && continue
  [ "$img" = "<none>:<none>" ] && continue
  # Never remove a vLLM image when the profile lookup failed.
  if [ "$config_ok" -eq 0 ] && [[ "$img" == vllm/vllm-openai:* ]]; then continue; fi
  [ -n "${KEEP[$img]:-}" ] && continue
  REMOVE+=("$img")
done < <(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | tr -d '\r' | sort -u)

echo ""
echo "== REMOVE (unused) =="
if [ ${#REMOVE[@]} -eq 0 ]; then echo "  (none)"; else printf '  %s\n' "${REMOVE[@]}"; fi

echo ""
echo "== Current disk usage =="
docker system df 2>/dev/null

if [ "$APPLY" -ne 1 ]; then
  echo ""
  echo "[dry-run] Re-run with --apply to remove the above and prune"
  echo "          dangling images + build cache + stopped containers + unused volumes."
  exit 0
fi

echo ""
echo "== APPLYING =="
if [ ${#REMOVE[@]} -gt 0 ]; then docker rmi "${REMOVE[@]}" || true; fi
docker container prune -f || true
docker image prune -f || true
docker builder prune -f || true
docker volume prune -af || true

echo ""
echo "== AFTER =="
docker system df 2>/dev/null
