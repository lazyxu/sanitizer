#!/usr/bin/env bash
# Docker内的一键编译＋GPU Sanitizer调用。默认跑全部28例的good/bad。
set -euo pipefail
verbose=0
[[ "${SAN_VERBOSE:-0}" == "1" ]] && verbose=1
args_without_verbose=()
for arg in "$@"; do
  case "$arg" in
    -v|--verbose) verbose=1 ;;
    *) args_without_verbose+=("$arg") ;;
  esac
done
set -- "${args_without_verbose[@]}"
if ((verbose)); then
  export SAN_VERBOSE=1
  PS4='+ [run-all] '
  set -x
fi
verbose_args=()
if ((verbose)); then verbose_args=(--verbose); fi
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "usage: $0 [cuda|rocm] [-v|--verbose] [run_samples.py options]"
  exit 0
fi
backend="${1:-${SAN_BACKEND:-cuda}}"
if [[ $# -gt 0 ]]; then shift; fi
exec python3 scripts/run_samples.py --backend "$backend" --case all --mode both "${verbose_args[@]}" "$@"
