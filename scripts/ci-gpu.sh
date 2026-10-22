#!/usr/bin/env bash
# GitHub Actions 与本地共用：在真正CUDA/ROCm GPU Docker里执行，禁止CPU-only替代。
# 用法：bash scripts/ci-gpu.sh cuda|rocm smoke|compile-all|safe-all
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="${1:-}"
suite="${2:-smoke}"
case "$backend" in
  cuda) dockerfile="$ROOT/docker/Dockerfile.cuda"; image="gpu-sanitizer-samples:cuda" ;;
  rocm) dockerfile="$ROOT/docker/Dockerfile.rocm"; image="gpu-sanitizer-samples:rocm" ;;
  *) echo "usage: $0 cuda|rocm smoke|compile-all|safe-all" >&2; exit 2 ;;
esac
case "$suite" in
  smoke|compile-all|safe-all|upstream-compile|upstream-safe) ;;
  *) echo "unsupported suite: $suite" >&2; exit 2 ;;
esac
command -v docker >/dev/null || { echo "Missing docker" >&2; exit 3; }
[[ "$(uname -s)" == "Linux" ]] || { echo "Real GPU container validation requires Linux runner" >&2; exit 3; }

mkdir -p "$ROOT/results/$backend"
metadata="$ROOT/results/$backend/runner-environment.txt"
{
  printf 'backend=%s\nsuite=%s\n' "$backend" "$suite"
  date -u +'%Y-%m-%dT%H:%M:%SZ'
  uname -srmo
  docker version --format 'docker={{.Server.Version}}'
} > "$metadata"

docker_args=(
  run --rm -v "$ROOT:/workspace" -w /workspace --ipc=host
  -e "SAN_BACKEND=$backend"
  -e "CUDA_ARCH=${CUDA_ARCH:-}"
  -e "ROCM_ARCH=${ROCM_ARCH:-}"
  -e "HSA_XNACK=${HSA_XNACK:-}"
)
if [[ "$backend" == cuda ]]; then
  command -v nvidia-smi >/dev/null || { echo "No NVIDIA driver/GPU" >&2; exit 4; }
  nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader | tee -a "$metadata"
  docker_args+=(--gpus all)
else
  [[ -e /dev/kfd && -d /dev/dri ]] || { echo "No AMD KFD/DRM GPU" >&2; exit 4; }
  [[ "${ROCM_ARCH:-}" == gfx*:xnack+ ]] || {
    echo "ROCM_ARCH must be supported GPU architecture with :xnack+" >&2; exit 4;
  }
  [[ "${HSA_XNACK:-}" == "1" ]] || {
    echo "ROCm GPU ASan requires supported XNACK and HSA_XNACK=1" >&2; exit 4;
  }
  printf 'rocm_arch=%s\n' "$ROCM_ARCH" >> "$metadata"
  docker_args+=(--device=/dev/kfd --device=/dev/dri)
fi

echo "Building real GPU toolchain container for backend=$backend"
docker build -f "$dockerfile" -t "$image" "$ROOT"

# 容器内再次检查设备可见性，避免将host探测或仅Docker构建当作GPU验收。
if [[ "$backend" == cuda ]]; then
  docker "${docker_args[@]}" "$image" bash -lc \
    'command -v nvcc; command -v compute-sanitizer; nvidia-smi -L'
else
  docker "${docker_args[@]}" "$image" bash -lc \
    'command -v hipcc; test -e /dev/kfd; command -v rocminfo; rocminfo >/workspace/results/rocm/rocminfo.txt'
fi

run_cases() {
  local id="$1"; shift
  docker "${docker_args[@]}" "$image" \
    python3 scripts/run_samples.py --backend "$backend" "$@" --strict
  # 每次runner都会更新summary.json，逐样例保存，避免S01/S13被S14覆盖。
  cp "$ROOT/results/$backend/summary.json" "$ROOT/results/$backend/summary-$id.json"
}
case "$suite" in
  smoke)
    run_cases S01 --case S01 --mode both
    if [[ "$backend" == cuda ]]; then
      run_cases S13 --case S13 --mode both
      run_cases S14 --case S14 --mode both
    fi
    ;;
  compile-all)
    run_cases compile-all --case all --compile-only
    ;;
  upstream-compile|upstream-safe)
    if [[ "$backend" != cuda ]]; then
      echo "NVIDIA upstream regression only supports CUDA; AMD main suite remains unchanged" >&2
      exit 2
    fi
    if [[ "$suite" == upstream-compile ]]; then
      docker "${docker_args[@]}" "$image" python3 scripts/run_upstream_nvidia.py --compile-only --strict
    else
      docker "${docker_args[@]}" "$image" python3 scripts/run_upstream_nvidia.py --case all --strict
    fi
    ;;
  safe-all)
    # 所有源代码编译；危险bad与不支持bad按manifest跳过，不可记为检测成功。
    run_cases safe-all --case all --mode both
    ;;
esac
echo "GPU validation complete: backend=$backend suite=$suite"
