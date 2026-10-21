#!/usr/bin/env bash
# 一键准备真实GPU sanitizer的Docker环境：
# ./scripts/setup-docker.sh cuda --test-s01
# ROCM_ARCH=gfx90a:xnack+ HSA_XNACK=1 ./scripts/setup-docker.sh rocm --test-s01
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="${1:-cuda}"
action="${2:---enter}"
case "$backend" in
  cuda) dockerfile="$root/docker/Dockerfile.cuda"; image="gpu-sanitizer-samples:cuda";;
  rocm) dockerfile="$root/docker/Dockerfile.rocm"; image="gpu-sanitizer-samples:rocm";;
  *) echo "用法：$0 cuda|rocm [--build-only|--enter|--test-s01]" >&2; exit 2;;
esac
command -v docker >/dev/null || { echo "需要Linux GPU宿主机Docker" >&2; exit 3; }
docker build -f "$dockerfile" -t "$image" "$root"
if [[ "$action" == "--build-only" ]]; then
  echo "Docker image=$image (仅构建，尚未运行GPU检测)"
  exit 0
fi
[[ "$(uname -s)" == "Linux" ]] || { echo "需要Linux GPU宿主机或正确配置的虚拟化GPU透传" >&2; exit 4; }
args=(run --rm -i -v "$root:/workspace" -w /workspace -e "SAN_BACKEND=$backend" -e "ROCM_ARCH=${ROCM_ARCH:-}" -e "HSA_XNACK=${HSA_XNACK:-}" -e "CUDA_ARCH=${CUDA_ARCH:-}" --ipc=host)
if [[ -t 0 && -t 1 ]]; then args+=(-t); fi
if [[ "$backend" == "cuda" ]]; then
  args+=(--gpus all)
else
  [[ -e /dev/kfd && -d /dev/dri ]] || { echo "宿主没有/dev/kfd或/dev/dri" >&2; exit 5; }
  args+=(--device=/dev/kfd --device=/dev/dri --group-add video --cap-add SYS_PTRACE)
fi
case "$action" in
  --test-s01)
    docker "${args[@]}" "$image" python3 scripts/run_samples.py --backend "$backend" --case S01 --mode both;;
  --enter)
    docker "${args[@]}" "$image" bash;;
  *) echo "无效选项: $action" >&2; exit 2;;
esac
