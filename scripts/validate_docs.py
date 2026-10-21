#!/usr/bin/env python3
"""文档防回归检查：生产Sanitizer必须在真实GPU执行。

该脚本只检查仓库文本与预算索引一致性，绝不运行或声称GPU检测。
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []

def require(test: bool, explanation: str) -> None:
    if not test:
        errors.append(explanation)

def text(path: str) -> str:
    target = ROOT / path
    if not target.is_file():
        errors.append(f"缺失文件：{path}")
        return ""
    return target.read_text(encoding="utf-8")

main = text("docs/llvm-gpu-sanitizer-research.md")
guide = text("docs/S01-vendor-implementation-guide.md")
archive = text("docs/research-notes/architecture.md")
readme = text("README.md")
audit = text("docs/hardware-deployability-audit.md")
manifest_text = text("samples/manifest.json")
try:
    manifest = json.loads(manifest_text)
except json.JSONDecodeError:
    errors.append("samples/manifest.json非法JSON")
    manifest = {"cases": []}

# 1. 任何当前生产方案不得仅驻留模拟器。
require("## 9. 真实GPU部署准入与模型验证边界" in main,
        "主稿第9章必须明确真实硬件准入")
require("真实GPU" in audit and "QEMU" in audit,
        "全仓库硬件准入审计应存在")
require("## 9. 路线 A" not in main, "不得恢复模型侧产品路线")
require("A 是 cmodel" not in main and "不把六条路线都写成无条件支持" not in main,
        "不得在场景总述恢复已废弃的六路线/A模型产品分类")
require("SanVerdict before_gpu_memory_effect(" not in main,
        "模拟器验证观察函数不能输出生产安全判定")
require("void trace_gpu_execution_for_validation(" in main,
        "第7.7.6应使用不改变GPU内存语义的验证观察接口")
require("M1可以直接在Q2运行S01正反例" not in main,
        "M1必须先交付真实GPU检测产物，再由QEMU运行相同产物")
require("模型能检" not in main,
        "第18.29场景表不得暗示独立模型检测算产品能力")
require("### 3.1 方案M" not in guide and "方案M：" not in guide,
        "不得恢复S01模型原生检测候选方案")
require("| S01-0 模型基线 |" not in guide,
        "S01 P0必须是实际GPU设备检查原型，不能仅有模型")
require("S01-0 **真实GPU设备代码原型**" in guide,
        "S01指南必须以真实GPU检查代码作为第一实施阶段")

# 2. 厂商运行时与编译期边界，不能反向修改Compiler依赖。
require("生产Compiler源码无需修改" in main or "生产Compiler源码可以为0" in main,
        "C纯Runtime不应强制要求修改Compiler")
require("生产编译器" in guide and "不要求修改" in guide,
        "NVIDIA-R需明确生产编译器零修改")
require("LLVM" in main and "Shadow" in main,
        "B编译期设备Shadow应保留生产方案")

# 3. 所有结构化预算只包含可实际部署到GPU的产品路线。
for p in ("docs/estimates-v1.2.json", "docs/estimates-v1.3.json",
          "docs/estimates-v1.7.json"):
    try:
        data = json.loads(text(p))
    except json.JSONDecodeError:
        errors.append(f"预算JSON无法解析：{p}")
        continue
    allowed = {"B", "C", "D", "E", "F"}
    for bucket in ("baseline_old_five_buckets", "regrouped_base_and_route_increments",
                   "base_totals", "conditional_KMD"):
        obj = data.get(bucket, {})
        if isinstance(obj, dict):
            keys = set(obj) if bucket == "base_totals" else set(obj)
            require("A" not in keys, f"{p}中仍保留模型-only的产品A键: {bucket}")
    require(set(data.get("base_totals", {})) == allowed,
            f"{p}产品总量必须恰为B/C/D/E/F")
    q = data.get("QEMU", {})
    for sub in ("Q2_route_unique_bridge_increments", "Q2_extra_by_route",
                "baseline_plus_Q2_by_route_existing_KMD"):
        require("A" not in q.get(sub, {}), f"{p}的QEMU.{sub}仍含A产品路线")
    require("A" not in data.get("conditional_KMD", {}).get(
            "route_possible_packages_and_all_missing_envelopes", {}),
            f"{p}的条件KMD预算仍含A路线")

# 额外检查报告自身不得将过时产品路线标为成功，防止预算主表虽改、验证摘要仍矛盾。
try:
    report = json.loads(text("docs/validation-report.json"))
except json.JSONDecodeError:
    errors.append("docs/validation-report.json无效JSON")
    report = {}
require(set(report.get("route_totals_core_test_combined", {})) == {"B","C","D","E","F"},
        "验证报告产品预算必须仅保留B/C/D/E/F")
require(report.get("checks", {}).get("all_six_routes_have_compiler_KMD_UMD_and_cmodel_QEMU_tables") != "pass",
        "验证报告不得继续把已撤销的六路线标记为pass")
require("six" not in str(report.get("checks", {}).get("baseline_cost_totals", "")).lower(),
        "旧六路线估算不能充作当前产品验证")

require("| A cmodel |" not in archive and "### A：cmodel" not in archive,
        "归档架构不得仍把模型检查列为产品方案或独立章节")
require("| P＋A |" not in main and "P＋A |" not in archive,
        "现行主稿/归档不得保留模型产品预算行")

# 4. 同步28个场景与样例中的正确三组件/QEMU表。
require(len(manifest.get("cases", [])) == 28, "manifest必须有28场景")
raw = main.split("### 18.29 场景与三组件、双平台的对应关系")
mapping = raw[1].split("## 19. 验证基准")[0] if len(raw) > 1 else ""
rows = {}
for row in mapping.splitlines():
    match = re.match(r"^\|\s*\[(S\d{2})\]\(#case-s\d{2}\)", row)
    if match:
        parts = [v.strip() for v in row.strip().split("|")[1:-1]]
        if len(parts) == 4:
            rows[match.group(1)] = parts

require(len(rows) == 28, "主稿§18.29必须覆盖28组平台职责")

for i in range(1, 29):
    case = f"S{i:02d}"
    md = text(f"samples/{case}/README.md")
    require("## 生产方案与两类模型辅助验证职责" in md,
            f"{case}缺少真机Checker/验证分工")
    require("真实GPU生产Checker" in md,
            f"{case}没有实际GPU Checker")
    require("模型" in md and "验证" in md,
            f"{case}应说明模型仅验证")
    require("QEMU" in md and "KMD" in md, f"{case}缺少平台和驱动说明")
    require(not re.search(r"\bA0\b|\bA2\b|方案M|路线 A 的", md),
            f"{case}遗留模型独立检测路线")
    if case in rows:
        require(rows[case][1] in md and rows[case][2] in md and rows[case][3] in md,
                f"{case}的平台职责必须准确来自主稿§18.29，不能误用场景能力编号")
    for name in ("cuda.cu", "amd.hip.cpp"):
        require((ROOT / "samples" / case / name).is_file(),
                f"{case}的{name}不存在")

# 5. 模型与硬件、CPU与GPU的验收状态不可混淆。
require("HARDWARE_NOT_VERIFIED" in audit, "审计必须记录GPU未完成验收")
require("GPU CI" in readme or "GPU Actions" in readme,
        "项目README必须继续给出真实GPU CI入口")

if errors:
    for problem in errors:
        print("DOC_AUDIT_FAILURE:", problem, file=sys.stderr)
    sys.exit(1)

print("DOC_AUDIT_PASS: B/C/D/E/F为真机候选；模拟器仅验证；28案例平台映射与预算无已撤销产品路线。")
print("这是文本/引用/结构审计；没有在GPU执行Sanitizer，也没有测试目标KMD/UMD。")
