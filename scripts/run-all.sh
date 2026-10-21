#!/usr/bin/env bash
# Docker内的一键编译＋GPU Sanitizer调用。默认跑全部28例的good/bad。
set -euo pipefail
backend="${1:-${SAN_BACKEND:-cuda}}"
if [[ $# -gt 0 ]]; then shift; fi
exec python3 scripts/run_samples.py --backend "$backend" --case all --mode both "$@"
