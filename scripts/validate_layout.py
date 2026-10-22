#!/usr/bin/env python3
"""结构检查（只验证文件完整性；不冒充GPU编译、运行或Sanitizer结果）。"""
import argparse
import json
from pathlib import Path
import re
import sys
p=argparse.ArgumentParser(description="CPU-only S01-S28 source layout audit")
p.add_argument("-v", "--verbose", action="store_true",
               help="打印静态校验读取的文件路径（无外部命令）")
args=p.parse_args()
root=Path(__file__).resolve().parents[1]
def inspect(path):
    if args.verbose:
        print("+ inspect " + str(path.relative_to(root)), file=sys.stderr, flush=True)
inspect(root/"samples/manifest.json")
data=json.loads((root/"samples/manifest.json").read_text(encoding="utf-8"))
cases=data["cases"]
errors=[]
if len(cases)!=28:
    errors.append(f"预期28例，实际{len(cases)}")
for i,c in enumerate(cases,1):
    case=f"S{i:02d}"
    if c["id"]!=case:
        errors.append(f"顺序/ID错误：{c['id']} 应为{case}")
    for backend,filename,marker in [
        ("cuda","cuda.cu","#define GPU_BACKEND_CUDA 1"),
        ("rocm","amd.hip.cpp","#define GPU_BACKEND_ROCM 1")
    ]:
        spec=c[backend]
        if spec["expect"] not in ("detect","conditional","unsupported","api","clean"):
            errors.append(f"{case} {backend} expect非法")
        f=root/"samples"/case/filename
        inspect(f)
        if not f.exists():
            errors.append(f"缺失源文件：{f}")
            continue
        code=f.read_text(encoding="utf-8")
        for required in ("#include",marker,"__global__","int main(","--bad","gpuDeviceSynchronize"):
            if required not in code:
                errors.append(f"{case} {filename} 缺少：{required}")
        if not re.search(r"[一-龥]{2,}",code):
            errors.append(f"{case} {filename} 缺少中文技术注释")
        if backend=="cuda" and "-fsanitize=address" in code:
            errors.append(f"{case}: CUDA源码包含CPU ASan相关编译选项")
    f=root/"samples"/case/"README.md"
    inspect(f)
    if not f.exists():
        errors.append(f"{case} 缺少README")
    elif not (case in f.read_text(encoding="utf-8")):
        errors.append(f"{case} README编号不一致")
for path in ["docker/Dockerfile.cuda","docker/Dockerfile.rocm",
             "scripts/run_samples.py","scripts/setup-docker.sh"]:
    inspect(root/path)
    if not (root/path).exists():
        errors.append(f"缺失配套文件：{path}")
if errors:
    print("\n".join("STRUCTURE_ERROR: "+x for x in errors),file=sys.stderr)
    sys.exit(1)
print("STRUCTURE_PASS: 28个样例目录，各有完整CUDA/HIP源码与README；配套GPU工具链文件均存在。")
print("注意：本检查从未调用nvcc/hipcc/GPU sanitizer，不代表样例在目标设备上能编译或检出。")
