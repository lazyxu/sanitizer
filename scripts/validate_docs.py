#!/usr/bin/env python3
"""只检查当前产品范围、真实设备准入、结构与预算，不运行GPU内核。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERRORS: list[str] = []
ROUTES = {"B", "C", "D", "E", "F"}
# 拼接保留词，防止旧候选名称出现在当前文档本身。
OBSOLETE = ("c" + "model", "qe" + "mu")
DELETED_BUDGETS = (
    "estimates-v1." + "2.json",
    "estimates-v1." + "3.json",
    "estimates-v1." + "6.1-runtime-C.json",
)

def require(value: bool, message: str) -> None:
    if not value:
        ERRORS.append(message)

def read(path: str) -> str:
    target = ROOT / path
    if not target.is_file():
        ERRORS.append(f"文件缺失：{path}")
        return ""
    return target.read_text(encoding="utf-8")

main = read("docs/llvm-gpu-sanitizer-research.md")
guide = read("docs/S01-vendor-implementation-guide.md")
spec = read("docs/real-gpu-component-implementation.md")
root_readme = read("README.md")
audit = read("docs/hardware-deployability-audit.md")
require("## 9. 真实GPU" in main, "主稿第9章需要真实GPU准入")
require("Runtime Binary Rewriter" in spec and "Compiler" in spec and "KMD" in spec,
        "真实GPU逐组件规范未覆盖必需组件")
require("纯Runtime" in spec and ("Compiler" in guide and "UMD" in guide),
        "纯运行时C不得强制修改生产Compiler")
require("真实GPU" in audit and "F1/F2" in audit, "真实GPU硬件审计缺失")
require("## 真实GPU" in root_readme or "真实GPU" in root_readme,
        "README需要真实GPU准入声明")

try:
    manifest = json.loads(read("samples/manifest.json"))
except (ValueError, TypeError):
    manifest = {"cases": []}
    ERRORS.append("样例manifest格式错误")
require(len(manifest.get("cases", [])) == 28, "manifest必须有28个场景")
for i in range(1, 29):
    case = f"S{i:02d}"
    description = read(f"samples/{case}/README.md")
    require(f"S{i:02d}" in description, f"{case} README编号缺失")
    require("## 真实GPU生产职责" in description, f"{case}真实GPU生产职责未列出")
    require("## 验收需要留下什么证据" in description, f"{case}硬件证据章节缺失")
    require((ROOT / "samples" / case / "cuda.cu").is_file(),
            f"{case}缺CUDA代码")
    require((ROOT / "samples" / case / "amd.hip.cpp").is_file(),
            f"{case}缺HIP代码")
    require(f"[{case}](#case-s{i:02d})" in main,
            f"主稿没有{case}索引与对应测试")

try:
    budget = json.loads(read("docs/estimates-v1.7.json"))
except (ValueError, TypeError):
    budget = {}
    ERRORS.append("预算JSON格式错误")
require(set(budget.get("routes", [])) == ROUTES, "只允许B/C/D/E/F五条产品路线")
totals = budget.get("base_totals", {})
require(set(totals) == ROUTES, "必须分别汇总B/C/D/E/F")
for route, data in totals.items():
    if route not in ROUTES:
        continue
    core = data.get("core", [])
    test = data.get("test", [])
    combined = data.get("total_core_plus_test", [])
    require(len(core) == len(test) == len(combined) == 2,
            f"{route}预算区间长度错误")
    if len(core) == len(test) == len(combined) == 2:
        require([core[0] + test[0], core[1] + test[1]] == combined,
                f"{route}核心/测试加总错误")
require(totals.get("C", {}).get("compiler_source_edit") == [0, 0],
        "C纯Runtime应有零生产Compiler源码新增")

for legacy in DELETED_BUDGETS:
    require(not (ROOT / "docs" / legacy).exists(), f"旧预算仍存在：{legacy}")

# 全仓库现行文本禁止遗留非产品实施文字；无需修改既有Git历史。
for dirpath in (ROOT / "docs", ROOT / "samples", ROOT / "scripts"):
    for p in dirpath.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".md", ".json", ".py", ".yml", ".yaml"):
            data = p.read_text(encoding="utf-8", errors="replace").lower()
            for phrase in OBSOLETE:
                require(phrase not in data,
                        f"旧非产品平台描述仍存在：{p.relative_to(ROOT)}")
for p in (ROOT / "README.md", ROOT / "docs" / "revision-history.md"):
    data = p.read_text(encoding="utf-8").lower()
    for phrase in OBSOLETE:
        require(phrase not in data, f"旧描述仍存在：{p.relative_to(ROOT)}")

# 静态文本审计绝不宣称已运行真机。
report = json.loads(read("docs/validation-report.json"))
require(report.get("hardware_executions") == 0,
        "未运行真机时不得填入设备检测次数")
require("HARDWARE_NOT_VERIFIED" in report.get("status", ""),
        "未获得目标GPU日志必须保留未验收状态")

if ERRORS:
    for error in ERRORS:
        print("DOC_CHECK_FAILED:", error, file=sys.stderr)
    sys.exit(1)
print("DOC_STRUCTURE_PASS: 5条真实GPU候选、28组源文件、生产组件预算与旧描述清理一致")
print("HARDWARE_NOT_VERIFIED: 未执行目标GPU产品检查器；此项静态测试不构成真机验收")
