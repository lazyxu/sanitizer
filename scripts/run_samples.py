#!/usr/bin/env python3
"""GPU Sanitizer样例运行器。CUDA Compute Sanitizer / ROCm GPU ASan，拒绝CPU-only伪阳性。"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "samples" / "manifest.json"
# 官方报告的设备侧标识：非零 ERROR SUMMARY 本身仅提示Compute Sanitizer检测到错误。
NVIDIA_GPU = re.compile(
    r"(Invalid __global__|Invalid __shared__|Invalid __local__|"
    r"Potential (?:RAW|WAR|WAW)|Hazard (?:RAW|WAR|WAW)|Race reported|"
    r"Barrier error|Uninitialized __global__|Uninitialized __shared__|"
    r"Leaked [0-9]+ bytes)", re.I)
# AMD GPU ASan 与普通 CPU ASan 的最直接区分依据。
AMD_GPU = re.compile(r"ERROR:\s*AddressSanitizer:[^\n]*on amdgpu device\s+[0-9]+", re.I)

def run(cmd, timeout=120, env=None):
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env, check=False)
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired as exc:
        data = exc.stdout or b""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return 124, data + "\nTIMEOUT: 已中止执行\n"
    except OSError as exc:
        return 127, "TOOL_LAUNCH_FAILED: " + str(exc)

def preflight(backend, compile_only):
    required = ("nvcc", "compute-sanitizer") if backend == "cuda" else ("hipcc",)
    for executable in required:
        if not shutil.which(executable):
            return "MISSING_TOOLCHAIN: " + executable
    if backend == "rocm":
        arch = os.getenv("ROCM_ARCH", "")
        if not arch.endswith(":xnack+"):
            return ("UNSUPPORTED: 必须先按受支持GPU设置ROCM_ARCH=gfx...:xnack+，"
                    "不能用不支持设备插桩的目标替代")
        if not compile_only:
            if os.getenv("HSA_XNACK") != "1":
                return "UNSUPPORTED: AMD GPU ASan需要HSA_XNACK=1（请先确认硬件支持）"
            if not Path("/dev/kfd").exists():
                return "NO_AMD_GPU: 缺少/dev/kfd"
    elif not compile_only and not shutil.which("nvidia-smi"):
        return "NO_NVIDIA_GPU: 缺少nvidia-smi / NVIDIA Container Toolkit"
    return ""

def compile_case(item, backend, compiler_patch):
    case = item["id"]
    source = ROOT / "samples" / case / ("cuda.cu" if backend == "cuda" else "amd.hip.cpp")
    outdir = ROOT / "build" / backend
    outdir.mkdir(parents=True, exist_ok=True)
    binary = outdir / case
    if not source.exists():
        return None, "MISSING_SOURCE: " + str(source)
    if backend == "cuda":
        cmd = ["nvcc", "-std=c++17", "-O0", "-lineinfo"]
        if os.getenv("CUDA_ARCH"):
            cmd += ["-arch=" + os.environ["CUDA_ARCH"]]
        elif case == "S19":
            # S19必须编译成真实SM80+ cp.async，否则可能静默退化为同步copy。
            cmd += ["-arch=sm_80"]
        if compiler_patch and item[backend]["tool"] == "memcheck":
            cmd += ["-fdevice-sanitize=memcheck"]
    else:
        cmd = ["hipcc", "-x", "hip", "-std=c++17", "-O1", "-g",
               "-fsanitize=address", "-shared-libsan",
               "--offload-arch=" + os.environ["ROCM_ARCH"]]
    cmd += [str(source), "-o", str(binary)]
    rc, log = run(cmd, timeout=240)
    if rc:
        return None, f"BUILD_FAILED (rc={rc})\n{log}"
    if backend == "rocm":
        if re.search(r"ignoring.+-fsanitize=address|not.+instrumented", log, re.I):
            return None, "GPU_INSTRUMENTATION_SKIPPED:\n" + log
        if not shutil.which("ldd"):
            return None, "MISSING_LDD: 无法审查ASan运行库"
        _, linked = run(["ldd", str(binary)])
        if not re.search(r"libclang_rt\.asan.*\.so", linked):
            return None, "HOST_ASAN_RUNTIME_MISSING:\n" + linked
        if not re.search(r"libamdhip64\.so.*[/\\]asan[/\\]", linked):
            return None, ("GPU_INSTRUMENTED_RUNTIME_NOT_VERIFIED：libamdhip64不是/asan/版本"
                          "\n" + linked)
    return binary, log

def classify(backend, expected, mode, rc, log, case=None):
    if rc == 124:
        return "TIMEOUT"
    if rc == 77 or "SKIP:" in log:
        return "PREREQUISITE_NOT_MET"
    # GPU API错误需要独立分类，即使Compute Sanitizer最后统计非零ERROR SUMMARY。
    if expected == "api" and "GPU_API_ERROR:" in log:
        return "GPU_API_ERROR_NOT_GPU_KERNEL_SANITIZER"
    detected = bool((NVIDIA_GPU if backend == "cuda" else AMD_GPU).search(log))
    if detected:
        if mode == "good":
            return "FALSE_POSITIVE_OR_OTHER_ERROR"
        # Smoke必须检出指定的GPU错误类别，不能把一个无关泄漏当成S01越界。
        required = {
            ("cuda", "S01"): r"Invalid __global__",
            ("cuda", "S13"): r"Uninitialized __global__",
            ("cuda", "S14"): r"(Potential (?:RAW|WAR|WAW)|Hazard (?:RAW|WAR|WAW)|Race reported)",
            ("rocm", "S01"): r"heap-buffer-overflow[^\n]*on amdgpu device\s+\d+"
        }.get((backend, case))
        if required and not re.search(required, log, flags=re.I):
            return "MISMATCHED_GPU_DIAGNOSTIC"
        return "DETECTED_GPU"
    if backend == "rocm" and "ERROR: AddressSanitizer:" in log:
        return "CPU_ASAN_ONLY_NOT_GPU"
    if "GPU_API_ERROR:" in log:
        return "GPU_API_ERROR_NOT_GPU_KERNEL_SANITIZER"
    # 非零SUMMARY和指定sanitizer异常退出不能当成无错误：
    # 未识别的tool诊断需要人工归类，不能编造具体设备错误。
    has_nonzero_summary = bool(re.search(
        r"(?:ERROR SUMMARY:\s*[1-9]\d*\s+errors|"
        r"RACECHECK SUMMARY:.*\([1-9]\d*\s+errors?)",
        log, flags=re.I))
    if has_nonzero_summary or rc == 86:
        return "UNCLASSIFIED_SANITIZER_ERROR"
    if rc != 0:
        return "RUNTIME_ERROR_NO_GPU_REPORT"
    if mode == "good":
        return "CLEAN"
    return "CLEAN_AS_EXPECTED" if expected == "clean" else "NOT_DETECTED"

def main():
    p = argparse.ArgumentParser(description="GPU代码编译＋CUDA Compute Sanitizer或AMDGPU ASan")
    p.add_argument("--backend", choices=["cuda", "rocm"],
                   default=os.getenv("SAN_BACKEND", "cuda"))
    p.add_argument("--case", default="all", help="S01..S28 / all")
    p.add_argument("--mode", choices=["bad","good","both"], default="both")
    p.add_argument("--compile-only", action="store_true")
    p.add_argument("--nv-compile-memcheck", action="store_true",
                   help="使用nvcc -fdevice-sanitize=memcheck；不要和race/init/sync混同")
    p.add_argument("--allow-dangerous", action="store_true",
                   help="显式允许可能超时/设备reset的barrier与释放实验")
    p.add_argument("--attempt-unsupported", action="store_true",
                   help="尝试运行没有厂家检测保证的案例，不把未报告当成成功")
    p.add_argument("--strict", action="store_true",
                   help="对预期必检出但没有GPU报告、误报与工具错误返回失败")
    a = p.parse_args()
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = m["cases"] if a.case == "all" else [x for x in m["cases"] if x["id"] == a.case]
    if not cases:
        p.error("找不到指定案例")
    issue = preflight(a.backend, a.compile_only)
    if issue:
        print(issue, file=sys.stderr)
        return 3
    results=[]
    results_dir = ROOT / "results" / a.backend
    results_dir.mkdir(parents=True, exist_ok=True)
    for item in cases:
        case = item["id"]
        spec = item[a.backend]
        print(f"\n=== {case} {item['name']} / {a.backend} ===", flush=True)
        binpath, buildlog = compile_case(item, a.backend, a.nv_compile_memcheck)
        (results_dir / f"{case}.build.log").write_text(buildlog, encoding="utf-8")
        if binpath is None:
            print(buildlog[-1200:])
            results.append(dict(case=case,status="BUILD_OR_INSTRUMENTATION_FAILURE"))
            continue
        if a.compile_only:
            print("COMPILED_ONLY（尚未执行GPU检测）")
            results.append(dict(case=case,status="COMPILED_ONLY"))
            continue
        modes=["good","bad"] if a.mode == "both" else [a.mode]
        for mode in modes:
            if mode == "bad" and item.get("dangerous") and not a.allow_dangerous:
                result="SKIPPED_DANGEROUS"
            elif mode == "bad" and spec["expect"] == "unsupported" and not a.attempt_unsupported:
                result="UNSUPPORTED_BY_VENDOR"
            else:
                cmd = ([str(binpath),"--"+mode] if a.backend == "rocm" else
                       ["compute-sanitizer","--error-exitcode=86","--tool="+spec["tool"]]
                       + (["--leak-check=full"] if case == "S12" else [])
                       + [str(binpath),"--"+mode])
                rc, log = run(cmd, timeout=40 if item.get("dangerous") else 120,
                              env=os.environ.copy())
                (results_dir / f"{case}.{mode}.log").write_text(log, encoding="utf-8")
                result=classify(a.backend, spec["expect"], mode, rc, log, case=case)
            print(f"  {mode}: {result} （要求：{spec['expect']}；{spec['reason']}）")
            results.append(dict(case=case,mode=mode,expected=spec["expect"],status=result))
    (results_dir / "summary.json").write_text(
        json.dumps(results,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    totals={x:sum(r["status"]==x for r in results) for x in sorted({r["status"] for r in results})}
    print("\n检测状态:",json.dumps(totals,ensure_ascii=False))
    print("GPU_API_ERROR、CPU_ASAN_ONLY、UNSUPPORTED、NOT_DETECTED均不是设备检测成功。")
    if a.strict:
        fail={"BUILD_OR_INSTRUMENTATION_FAILURE","FALSE_POSITIVE_OR_OTHER_ERROR",
              "TIMEOUT","RUNTIME_ERROR_NO_GPU_REPORT","CPU_ASAN_ONLY_NOT_GPU",
              "UNCLASSIFIED_SANITIZER_ERROR", "MISMATCHED_GPU_DIAGNOSTIC"}
        if any(x["status"] in fail or
               (x.get("mode")=="bad" and x["expected"]=="detect" and
                x["status"]!="DETECTED_GPU") for x in results):
            return 1
    return 0

if __name__=="__main__":
    sys.exit(main())
