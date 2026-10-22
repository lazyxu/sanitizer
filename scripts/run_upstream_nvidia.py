#!/usr/bin/env python3
"""Pinned NVIDIA upstream regressions. Vendor/custom diagnostics and GPU evidence stay distinct.

This runner intentionally does not turn NVIDIA reports into custom-checker results.
CPU-only validate/compile modes never claim GPU detection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "upstream" / "nvidia"
MANIFEST = UPSTREAM / "manifest.json"
OUTPUT = ROOT / "results" / "upstream" / "nvidia"
SOURCE_ID = re.compile(r"NV-(?:MEM|INIT|RACE|SYNC)-[0-9]{2}\Z")
GIT_SHA = re.compile(r"[a-f0-9]{40}\Z")
SUMMARY_ERROR = re.compile(r"(?:ERROR SUMMARY:\s*[1-9]\d*\s+errors|RACECHECK SUMMARY:.*[1-9]\d*)", re.I)
GPU_REPORT_PREFIX = re.compile(r"(?m)^\s*={6,}\s+\S")


def git_blob_sha(data: bytes) -> str:
    """Match the Git blob identity published by NVIDIA, including original newlines."""
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def safe_upstream_path(value: str) -> Path:
    name = PurePosixPath(value)
    if (not value or name.is_absolute() or ".." in name.parts
            or "\\" in value or str(name) != value or "." in name.parts):
        raise ValueError("unsafe vendored path: " + repr(value))
    return UPSTREAM / value


def validate_manifest() -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not GIT_SHA.fullmatch(data.get("upstream_commit", "")):
        raise ValueError("invalid upstream manifest/version/pin")
    if data.get("origin") != "https://github.com/NVIDIA/compute-sanitizer-samples":
        raise ValueError("unknown upstream origin")
    if data.get("license") != "BSD-3-Clause":
        raise ValueError("unexpected upstream license")
    files = data.get("vendored_files")
    if not isinstance(files, list) or not files:
        raise ValueError("empty vendored file inventory")
    seen = set()
    for item in files:
        name = item["path"]
        if name in seen:
            raise ValueError("duplicate vendored file: " + name)
        seen.add(name)
        sha = item["git_blob_sha"]
        if not GIT_SHA.fullmatch(sha):
            raise ValueError("invalid upstream blob SHA: " + name)
        path = safe_upstream_path(name)
        if not path.is_file() or git_blob_sha(path.read_bytes()) != sha:
            raise ValueError("missing or modified upstream source/license: " + name)
    if "LICENSE" not in seen or not (UPSTREAM / "LICENSE").read_text().startswith("Copyright (c)"):
        raise ValueError("original NVIDIA LICENSE is required")
    case_ids = set()
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("empty upstream case list")
    for item in cases:
        case = item["id"]
        if not SOURCE_ID.fullmatch(case) or case in case_ids:
            raise ValueError("invalid/duplicate NV case: " + case)
        case_ids.add(case)
        source = item["source"]
        if source not in seen or not source.endswith(".cu"):
            raise ValueError("case source is not pinned: " + case)
        if item["tool"] not in {"memcheck", "racecheck", "initcheck", "synccheck"}:
            raise ValueError("unknown vendor tool: " + case)
        if item["expected"] not in {"detect", "conditional"}:
            raise ValueError("unexpected detector guarantee: " + case)
        if not isinstance(item["dangerous"], bool):
            raise ValueError("dangerous must be boolean: " + case)
        if not isinstance(item["vendor_options"], list) or not all(
                isinstance(x, str) and x.startswith("-") or x in {"kernel", "analysis", "hazard", "full"}
                for x in item["vendor_options"]):
            raise ValueError("invalid vendor options: " + case)
        if not isinstance(item["compile_flags"], list) or any(
                x not in {"-O0", "-O2", "-lineinfo"} for x in item["compile_flags"]):
            raise ValueError("unexpected compiler flags: " + case)
        patterns = item["required_diagnostics"]
        if not isinstance(patterns, list) or not patterns:
            raise ValueError("missing required diagnostic patterns: " + case)
        for pattern in patterns:
            re.compile(pattern)
    return data


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace",
                           timeout=timeout, check=False)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as exc:
        data = exc.stdout or b""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return 124, data + "\nTIMEOUT: process terminated by upstream regression runner\n"
    except OSError as exc:
        return 127, "TOOL_LAUNCH_FAILED: " + str(exc)


def collect_environment(compile_only: bool) -> dict:
    env = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
           "cuda_arch": os.getenv("CUDA_ARCH", ""), "gpu_visible": False,
           "gpu_query": "NOT_RUN", "nvcc_version": "UNAVAILABLE",
           "compute_sanitizer_version": "UNAVAILABLE"}
    if shutil.which("nvcc"):
        _, env["nvcc_version"] = run(["nvcc", "--version"], timeout=15)
    if shutil.which("compute-sanitizer"):
        _, env["compute_sanitizer_version"] = run(["compute-sanitizer", "--version"], timeout=15)
    # Checking the GPU is a prerequisite only, not proof any kernel executed.
    if not compile_only and shutil.which("nvidia-smi"):
        rc, log = run(["nvidia-smi", "-L"], timeout=15)
        env["gpu_query"] = log[:8000]
        env["gpu_visible"] = rc == 0 and bool(log.strip()) and "GPU " in log
    return env


def classify_vendor(rc: int, log: str, required_patterns: list[str]) -> str:
    """Require NVIDIA-formatted GPU diagnostics, never just a nonzero exit."""
    if rc == 124:
        return "TIMEOUT"
    if rc == 127:
        return "TOOL_LAUNCH_FAILED"
    if not GPU_REPORT_PREFIX.search(log):
        return "RUNTIME_ERROR_NO_GPU_REPORT" if rc else "NOT_DETECTED"
    matches = [bool(re.search(x, log, flags=re.I)) for x in required_patterns]
    if all(matches):
        return "DETECTED_GPU"
    if any(matches):
        return "PARTIAL_GPU_DIAGNOSTIC"
    if SUMMARY_ERROR.search(log) or rc:
        return "MISMATCHED_GPU_DIAGNOSTIC"
    return "NOT_DETECTED"


def classify_custom(rc: int, log: str, pattern: str) -> str:
    if rc == 124:
        return "TIMEOUT"
    if rc == 127:
        return "TOOL_LAUNCH_FAILED"
    if re.search(pattern, log, flags=re.I):
        # Custom reporter is NOT the NVIDIA oracle; hardware proof is separate.
        return "CUSTOM_DIAGNOSTIC_REPORTED"
    return "CUSTOM_TOOL_ERROR_NO_DIAGNOSTIC" if rc else "NOT_DETECTED"


def compare(vendor: str, custom: str) -> str:
    if custom in {"NOT_CONFIGURED", "NOT_REQUESTED", "SKIPPED_DANGEROUS", "COMPILED_ONLY"}:
        return "NOT_COMPARABLE"
    if vendor in {"NOT_REQUESTED", "PREREQUISITE_NOT_MET", "BUILD_FAILED",
                  "SKIPPED_DANGEROUS", "COMPILED_ONLY"}:
        return "NOT_COMPARABLE"
    v = vendor == "DETECTED_GPU"
    c = custom == "CUSTOM_DIAGNOSTIC_REPORTED"
    if v and c:
        return "BOTH_REPORT_DIAGNOSTICS"
    if v and custom == "NOT_DETECTED":
        return "VENDOR_ONLY"
    if c and vendor == "NOT_DETECTED":
        return "CUSTOM_ONLY_VENDOR_NOT_DETECTED"
    if vendor == "NOT_DETECTED" and custom == "NOT_DETECTED":
        return "BOTH_NOT_DETECTED"
    return "INCONCLUSIVE"


def custom_argv(spec: str, binary: Path, case_id: str) -> list[str]:
    tokens = shlex.split(spec)
    if not tokens or not any("{binary}" in item for item in tokens):
        raise ValueError("--custom-command must contain {binary}")
    return [item.replace("{binary}", str(binary)).replace("{case}", case_id)
            for item in tokens]


def compile_case(case: dict, build_cache: dict) -> tuple[Path | None, str, str]:
    source = UPSTREAM / case["source"]
    flags = tuple(case["compile_flags"])
    key = (str(source), flags, os.getenv("CUDA_ARCH", ""))
    if key in build_cache:
        return build_cache[key]
    binary_dir = ROOT / "build" / "upstream" / "nvidia"
    binary_dir.mkdir(parents=True, exist_ok=True)
    binary = binary_dir / (source.parent.name.lower() + "-" + source.stem)
    cmd = ["nvcc", "-std=c++17", *flags]
    if os.getenv("CUDA_ARCH"):
        cmd.append("-arch=" + os.environ["CUDA_ARCH"])
    cmd.extend([str(source), "-o", str(binary)])
    rc, log = run(cmd, timeout=240)
    log_path = OUTPUT / "build" / (source.parent.name.lower() + "-" + source.stem + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log, encoding="utf-8")
    result = ((binary if rc == 0 and binary.exists() else None),
              str(log_path.relative_to(ROOT)), "COMPILED" if rc == 0 and binary.exists() else "BUILD_FAILED")
    build_cache[key] = result
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Pinned NVIDIA Compute Sanitizer upstream cases")
    parser.add_argument("--validate-only", action="store_true", help="CPU-only pin/license/manifest verification")
    parser.add_argument("--compile-only", action="store_true", help="Compile sources, do not execute on GPU")
    parser.add_argument("--case", default="all", help="NV-MEM-01 etc, or all")
    parser.add_argument("--mode", choices=["vendor", "custom", "both"], default="both")
    parser.add_argument("--allow-dangerous", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Expected vendor detections/build errors fail CI")
    parser.add_argument("--strict-custom", action="store_true", help="Require configured custom diagnostics too")
    parser.add_argument("--custom-command", help="Trusted executable and {binary} placeholder; no shell")
    parser.add_argument("--custom-diagnostic-regex", help="Required when custom command is set")
    args = parser.parse_args()
    try:
        data = validate_manifest()
        if bool(args.custom_command) != bool(args.custom_diagnostic_regex):
            parser.error("--custom-command and --custom-diagnostic-regex must be supplied together")
        if args.strict_custom and not args.custom_command:
            parser.error("--strict-custom requires --custom-command")
        if args.mode == "custom" and not args.custom_command:
            parser.error("--mode custom requires --custom-command")
        if args.strict_custom and args.mode == "vendor":
            parser.error("--strict-custom cannot be used in vendor-only mode")
        if args.custom_command:
            custom_argv(args.custom_command, Path("/tmp/placeholder"), "NV-MEM-01")
            re.compile(args.custom_diagnostic_regex)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, re.error) as exc:
        print("UPSTREAM_MANIFEST_ERROR:", exc, file=sys.stderr)
        return 2
    if args.validate_only:
        print(f"UPSTREAM_SOURCE_PASS: {len(data['vendored_files'])} NVIDIA files; "
              f"{len(data['cases'])} cases; pinned {data['upstream_commit']}. GPU_NOT_RUN")
        return 0

    selected = data["cases"] if args.case == "all" else [
        x for x in data["cases"] if x["id"] == args.case]
    if not selected:
        parser.error("unknown upstream case")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    env = collect_environment(args.compile_only)
    (OUTPUT / "environment.json").write_text(json.dumps(env, indent=2, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
    summary = {
        "schema_version": 1, "suite": "nvidia-upstream", "upstream_commit": data["upstream_commit"],
        "main_suite": "samples/S01-S28 (separate; unchanged)", "environment_path": str(
            (OUTPUT / "environment.json").relative_to(ROOT)), "cases": []
    }
    failures = 0
    build_cache = {}
    for case in selected:
        cid = case["id"]
        record = {
            "id": cid, "source": case["source"], "tool": case["tool"],
            "expected": case["expected"], "related_main_cases": case["related"],
            "build": {"status": "NOT_RUN", "log": None},
            "vendor_diagnostic": {"status": "NOT_REQUESTED", "exit_code": None, "log": None},
            "custom_diagnostic": {"status": "NOT_CONFIGURED" if not args.custom_command else "NOT_REQUESTED",
                                  "exit_code": None, "log": None},
            "gpu_evidence": {"status": "GPU_NOT_RUN", "gpu_visible": env["gpu_visible"],
                             "vendor_tool_invoked": False, "custom_tool_invoked": False},
            "comparison": "NOT_COMPARABLE"
        }
        summary["cases"].append(record)
        if not shutil.which("nvcc"):
            record["build"]["status"] = "MISSING_NVCC"
            failures += 1
            continue
        binary, build_log, build_status = compile_case(case, build_cache)
        record["build"] = {"status": build_status, "log": build_log}
        if binary is None:
            failures += 1
            continue
        record["gpu_evidence"]["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        if args.compile_only:
            record["vendor_diagnostic"]["status"] = "COMPILED_ONLY"
            record["custom_diagnostic"]["status"] = "COMPILED_ONLY"
            continue
        if case["dangerous"] and not args.allow_dangerous:
            record["vendor_diagnostic"]["status"] = "SKIPPED_DANGEROUS"
            record["custom_diagnostic"]["status"] = "SKIPPED_DANGEROUS"
            continue
        if not env["gpu_visible"]:
            record["vendor_diagnostic"]["status"] = "PREREQUISITE_NOT_MET"
            record["custom_diagnostic"]["status"] = "PREREQUISITE_NOT_MET"
            failures += 1
            continue
        case_dir = OUTPUT / cid
        case_dir.mkdir(parents=True, exist_ok=True)
        if args.mode in {"vendor", "both"}:
            if not shutil.which("compute-sanitizer"):
                record["vendor_diagnostic"]["status"] = "PREREQUISITE_NOT_MET"
                failures += 1
            else:
                cmd = ["compute-sanitizer", "--tool", case["tool"], "--error-exitcode=86",
                       *case["vendor_options"], str(binary)]
                rc, log = run(cmd, timeout=40 if case["dangerous"] else 120)
                logfile = case_dir / "vendor.log"
                logfile.write_text(log, encoding="utf-8")
                status = classify_vendor(rc, log, case["required_diagnostics"])
                record["vendor_diagnostic"] = {
                    "status": status, "exit_code": rc, "log": str(logfile.relative_to(ROOT)),
                    "tool": "NVIDIA Compute Sanitizer", "tool_options": case["vendor_options"]}
                record["gpu_evidence"]["vendor_tool_invoked"] = True
                if status == "DETECTED_GPU":
                    record["gpu_evidence"]["status"] = "NVIDIA_DEVICE_DIAGNOSTIC_OBSERVED"
                else:
                    record["gpu_evidence"]["status"] = "GPU_VISIBLE_LAUNCH_ATTEMPTED"
                if args.strict and (status in {"TIMEOUT", "TOOL_LAUNCH_FAILED",
                         "RUNTIME_ERROR_NO_GPU_REPORT", "MISMATCHED_GPU_DIAGNOSTIC",
                         "PARTIAL_GPU_DIAGNOSTIC"} or
                        (case["expected"] == "detect" and status != "DETECTED_GPU")):
                    failures += 1
        if args.mode in {"custom", "both"} and args.custom_command:
            cmd = custom_argv(args.custom_command, binary, cid)
            rc, log = run(cmd, timeout=40 if case["dangerous"] else 120)
            logfile = case_dir / "custom.log"
            logfile.write_text(log, encoding="utf-8")
            status = classify_custom(rc, log, args.custom_diagnostic_regex)
            record["custom_diagnostic"] = {
                "status": status, "exit_code": rc, "log": str(logfile.relative_to(ROOT)),
                "executable": cmd[0], "arguments_redacted": True}
            record["gpu_evidence"]["custom_tool_invoked"] = True
            if record["gpu_evidence"]["status"] == "GPU_NOT_RUN":
                record["gpu_evidence"]["status"] = "CUSTOM_DIAGNOSTIC_CLAIMED" if (
                    status == "CUSTOM_DIAGNOSTIC_REPORTED") else "GPU_VISIBLE_LAUNCH_ATTEMPTED"
            if args.strict_custom and status != "CUSTOM_DIAGNOSTIC_REPORTED":
                failures += 1
        record["comparison"] = compare(record["vendor_diagnostic"]["status"],
                                       record["custom_diagnostic"]["status"])
        print(f"{cid}: build={record['build']['status']}; "
              f"vendor={record['vendor_diagnostic']['status']}; "
              f"custom={record['custom_diagnostic']['status']}; "
              f"gpu_evidence={record['gpu_evidence']['status']}", flush=True)

    if args.strict and not args.compile_only and not any(
            r["gpu_evidence"]["vendor_tool_invoked"] or
            r["gpu_evidence"]["custom_tool_invoked"] for r in summary["cases"]):
        print("NO_GPU_CASE_EXECUTED: strict mode requires at least one tool invocation", file=sys.stderr)
        failures += 1
    target = OUTPUT / "summary.json"
    target.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"UPSTREAM_REGRESSION_RESULT: {target.relative_to(ROOT)}; "
          f"cases={len(summary['cases'])}; failures={failures}; "
          "NVIDIA reports never count as custom-checker results")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
