#!/usr/bin/env python3
"""静态文档与生产组件契约审计。CPU CI不代表真实GPU已检出。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []

def require(predicate: bool, message: str) -> None:
    if not predicate:
        errors.append(message)

def read(path: str) -> str:
    filename = ROOT / path
    if not filename.is_file():
        errors.append(f"缺失文件: {path}")
        return ""
    return filename.read_text(encoding="utf-8")

def json_file(path: str) -> dict:
    data = read(path)
    try:
        return json.loads(data)
    except ValueError:
        errors.append(f"无效JSON: {path}")
        return {}

def range_sum(*ranges: list[int]) -> list[int]:
    return [sum(r[0] for r in ranges), sum(r[1] for r in ranges)]

def check_markdown(path: str, content: str) -> None:
    lines = content.splitlines()
    fence = None
    table_width = None
    for lineno, line in enumerate(lines, 1):
        trimmed = line.lstrip()
        marker = re.match(r"^(~{3,}|\x60{3,})", trimmed)
        if marker:
            kind = marker.group(1)[0]
            if fence is None:
                fence = kind
            elif fence == kind:
                fence = None
            table_width = None
            continue
        if fence is not None:
            continue
        if trimmed.startswith("|") and trimmed.rstrip().endswith("|"):
            columns = len(re.split(r"(?<!\\)\|", trimmed)) - 2
            if table_width is None:
                table_width = columns
            elif columns != table_width:
                errors.append(f"{path}:{lineno} 表格列数不一致({columns}!={table_width})")
        else:
            table_width = None
    require(fence is None, f"{path}: 未闭合Markdown代码块")

ROUTES = ("B", "C", "D", "E", "F")
main = read("docs/llvm-gpu-sanitizer-research.md")
guide = read("docs/S01-vendor-implementation-guide.md")
spec = read("docs/real-gpu-component-implementation.md")
readme = read("README.md")
audit = read("docs/hardware-deployability-audit.md")
budget = json_file("docs/estimates-v1.7.json")
report = json_file("docs/validation-report.json")
manifest = json_file("samples/manifest.json")

require(set(budget.get("routes", [])) == set(ROUTES), "预算产品路线不等于B/C/D/E/F")
require(set(budget.get("base_totals", {})) == set(ROUTES), "预算总量仅可为B/C/D/E/F")
require(set(report.get("route_status", {})) == set(ROUTES), "进度状态必须覆盖五条产品路线")
require(report.get("hardware_detection_logs") == 0, "尚无证据时不得宣称硬件检出")
require("HARDWARE_NOT_VERIFIED" in str(report.get("status", "")), "进度报告需标记硬件未验收")
require("真实GPU" in spec and "Runtime Binary Rewriter" in spec, "实施规范需说明设备执行和UMD重写")
require("## 9. 真实GPU产品部署准入" in main, "主稿应保留真机准入")
require("Compiler" in guide and "KMD" in guide and "UMD" in guide, "S01必须有生产组件职责")
require("## 2. 方案与职责" in audit, "审计需要真实组件职责")
require("docs/real-gpu-component-implementation.md" in readme, "README需链接逐组件方案")
for component in ("Compiler", "Device Runtime", "Host Runtime", "Runtime Binary Rewriter",
                  "KMD", "Firmware", "Hardware", "Reporter", "真机测试"):
    require(component in spec, f"实施规范缺少组件: {component}")

P = budget.get("P", {})
incs = budget.get("increments_above_P", {})
totals = budget.get("base_totals", {})
try:
    p_core, p_test = P["production_total"]["core"], P["production_total"]["test"]
    require(p_core == range_sum(P["Compiler"]["core"], P["UMD_and_tools"]["core"]), "P核心预算不一致")
    require(p_test == range_sum(P["Compiler"]["test"], P["UMD_and_tools"]["test"]), "P测试预算不一致")
    for route in ROUTES:
        delta = incs[route]
        total = totals[route]
        if route == "C":
            require(delta["Compiler_source_edit"]["core"] == [0, 0],
                    "纯C Runtime不应要求修改生产Compiler源码")
            parts = (delta["UMD_binary_rewriter_and_device_patch"],)
        else:
            parts = (delta["Compiler"], delta["UMD"])
        core = range_sum(p_core, *(part["core"] for part in parts))
        tests = range_sum(p_test, *(part["test"] for part in parts))
        require(total["core"] == core, f"{route}: core不是P+增量")
        require(total["test"] == tests, f"{route}: test不是P+增量")
        require(total["total_core_plus_test"] == range_sum(core, tests),
                f"{route}: 合计预算算术错误")
except (KeyError, IndexError, TypeError) as exc:
    errors.append(f"预算字段缺失: {exc}")

for old in ("docs/estimates-v1.2.json", "docs/estimates-v1.3.json",
            "docs/estimates-v1.6.1-runtime-C.json"):
    require(not (ROOT / old).exists(), f"旧预算未清理: {old}")

cases = manifest.get("cases", [])
ids = [f"S{i:02d}" for i in range(1, 29)]
require([item.get("id") for item in cases] == ids, "manifest需包含连续28组S01～S28")
for case in ids:
    sample = read(f"samples/{case}/README.md")
    require("## 真实GPU生产职责" in sample, f"{case}未改为真实GPU组件职责")
    require("KMD" in sample and "Compiler" in sample, f"{case}缺少生产组件职责")
    for filename in ("cuda.cu", "amd.hip.cpp"):
        require((ROOT / "samples" / case / filename).is_file(), f"缺失{case}/{filename}")
    require(f'<a id="case-{case.lower()}"></a>' in main,
            f"主稿缺少{case}锚点")

require("### 18.29 场景与真实GPU组件的对应关系" in main, "缺少真实GPU案例汇总")
mapping = main.split("### 18.29 场景与真实GPU组件的对应关系")[-1].split(
    "### 18.30 真实GPU工具样例")[0]
for case in ids:
    require(f"[{case}](#case-{case.lower()})" in mapping, f"主稿场景表缺失{case}")

# 拼接禁用关键词，避免校验脚本自身含旧表述。
obsolete_terms = ("c" + "model", "q" + "emu")
obsolete_phrases = ("模型" + "只负责验证", "模型" + "仅作验证",
                    "模型" + "预验证", "两类" + "模型辅助验证")
ignored_dirs = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "results", "dist"}
text_suffixes = {".md", ".json", ".py", ".yml", ".yaml", ".sh", ".cu", ".cpp", ".h", ".txt"}
for target in ROOT.rglob("*"):
    if not target.is_file():
        continue
    rel = target.relative_to(ROOT)
    if any(part in ignored_dirs for part in rel.parts):
        continue
    if target.suffix.lower() not in text_suffixes and not target.name.startswith("Dockerfile"):
        continue
    try:
        body = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for term in obsolete_terms:
        if term.casefold() in body.casefold():
            errors.append(f"{rel}: 遗留废弃平台关键词")
    for phrase in obsolete_phrases:
        if phrase in body:
            errors.append(f"{rel}: 遗留废弃验证项目表述")
    if target.suffix.lower() == ".md":
        check_markdown(str(rel), body)

if errors:
    for e in errors:
        print("DOC_AUDIT_FAILURE:", e, file=sys.stderr)
    sys.exit(1)
print("DOC_AUDIT_PASS: B/C/D/E/F真机路线、28个案例、结构与预算一致。")
print("CPU静态校验不等于自研GPU已实现或真实设备检出。")
