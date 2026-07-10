#!/bin/sh
set -eu

umask 027

runtime_root="$(realpath -m -- "${JARVIS_HOME:-/runtime}")"
model_root="$(realpath -m -- "${JARVIS_MODEL_ROOT:-${runtime_root}/data/models}")"
home_root="$(realpath -m -- "${HOME:-/home/jarvis}")"

case "$runtime_root" in
  /runtime|/runtime/*) ;;
  *)
    echo "Refusing unsafe JARVIS_HOME outside /runtime: $runtime_root" >&2
    exit 64
    ;;
esac

case "$model_root" in
  "$runtime_root"|"$runtime_root"/*) ;;
  *)
    echo "Refusing unsafe JARVIS_MODEL_ROOT outside JARVIS_HOME: $model_root" >&2
    exit 64
    ;;
esac

if [ "$home_root" != "/home/jarvis" ]; then
  echo "Refusing unexpected HOME: $home_root" >&2
  exit 64
fi

runtime_path() {
  resolved="$(realpath -m -- "$1")"
  case "$resolved" in
    "$runtime_root"|"$runtime_root"/*) printf '%s\n' "$resolved" ;;
    *)
      echo "Refusing runtime path outside JARVIS_HOME: $resolved" >&2
      exit 64
      ;;
  esac
}

grant_parent_access() {
  current="$1"
  while :; do
    chown jarvis:jarvis -- "$current"
    [ "$current" = "$runtime_root" ] && break
    current="$(dirname -- "$current")"
  done
}

if [ "$(id -u)" = "0" ]; then
  mkdir -p -- "$home_root"
  chown jarvis:jarvis -- "$home_root"

  for path in \
    "${runtime_root}/data/jarvis-gpt" \
    "${runtime_root}/cache/jarvis-gpt" \
    "${runtime_root}/logs/jarvis-gpt" \
    "${runtime_root}/docker/jarvis-gpt"
  do
    mkdir -p -- "$path"
    path="$(runtime_path "$path")"
    if ! gosu jarvis test -w "$path"; then
      chown -R jarvis:jarvis -- "$path"
    fi
    grant_parent_access "$path"
    if ! gosu jarvis test -w "$path"; then
      echo "Runtime path is not writable by jarvis: $path" >&2
      exit 73
    fi
  done

  mkdir -p -- "$model_root"
  model_root="$(runtime_path "$model_root")"
  if ! gosu jarvis test -w "$model_root"; then
    chown jarvis:jarvis -- "$model_root"
  fi
  grant_parent_access "$model_root"
  if ! gosu jarvis test -w "$model_root"; then
    echo "Model root is not writable by jarvis: $model_root" >&2
    exit 73
  fi
  exec gosu jarvis "$@"
fi

exec "$@"
