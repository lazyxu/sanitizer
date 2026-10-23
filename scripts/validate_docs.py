#!/usr/bin/env python3
"""GPU Sanitizer文档、案例、生产预算一致性审计。

这只检查仓库结构，不会执行GPU kernel，也不意味着目标GPU功能已实现。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
_parser = argparse.ArgumentParser(description="CPU-only technical documentation audit")
_parser.add_argument("-v", "--verbose", action="store_true",
                     help="打印审计读取的文件路径（无外部命令）")
_args = _parser.parse_args()

def inspect(path: Path) -> None:
    if _args.verbose:
        print("+ inspect " + str(path.relative_to(ROOT)), file=sys.stderr, flush=True)
FAILURES: list[str] = []


def check(ok: bool, why: str) -> None:
    if not ok:
        FAILURES.append(why)


def read(path: str) -> str:
    p = ROOT / path
    inspect(p)
    if not p.is_file():
        FAILURES.append(f"缺失文件：{path}")
        return ""
    return p.read_text(encoding="utf-8")


def load_json(path: str) -> dict:
    data = read(path)
    try:
        obj = json.loads(data)
        check(isinstance(obj, dict), f"{path}顶层必须为object")
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError as e:
        FAILURES.append(f"无效JSON {path}: {e}")
        return {}


def add_range(*pairs: list[int]) -> list[int]:
    return [sum(x[0] for x in pairs), sum(x[1] for x in pairs)]


def match_routes(k: str, wanted: set[str]) -> None:
    check(set(k) == wanted, f"候选路线必须只有{sorted(wanted)}，实得{sorted(set(k))}")


# 连字符串也防止让旧平台描述重新进入当前文稿。
legacy_words = (
    "cm" + "odel",
    "qe" + "mu",
    "模拟" + "器",
    "仿" + "真器",
)
text_suffixes = {".md", ".json", ".py", ".sh", ".yaml", ".yml", ".cu", ".cpp", ".hip", ".h", ".txt"}
for p in ROOT.rglob("*"):
    if not p.is_file() or p.suffix not in text_suffixes or ".git" in p.parts:
        continue
    inspect(p)
    try:
        body = p.read_text(encoding="utf-8")
    except UnicodeError:
        FAILURES.append(f"无法按UTF-8读取：{p.relative_to(ROOT)}")
        continue
    # 排除本脚本定义的拆分词字面量；本脚本不能使用这些完整敏感名称。
    for needle in legacy_words:
        if needle.lower() in body.lower():
            FAILURES.append(f"残留已撤销平台说明：{p.relative_to(ROOT)}，关键词 {needle}")
            break
    check(not re.search(r"(?:路线|方案)\s*A\b|第六(?:条|种)方案", body),
          f"{p.relative_to(ROOT)}中不得再出现额外产品路线")


# 旧产品概念不能只删除名称、继续保留同义的设备检查实现职责。
# 谨慎避开S04案例中“对象A/对象B”的合法文字，防止误删原对象Bounds示例。
retired_meaning_patterns = [
    re.compile(r"(?:方案|路线)\s*A(?=\W|[\u4e00-\u9fff])"),
    re.compile(r"独立\s*A\s*(?:校验|验证|检测|检查)"),
    re.compile(r"(?<![A-Za-z0-9_])A\s*(?:在allocation时|按\x60launch|按动态warp|同时模拟|同时记录对象|把异步传输|使用global|记录capture|按资源描述符|须连同Host访问|在同步指令执行|须额外保留token)"),
    re.compile(r"模型(?:来源追踪只能辅助|重放可测试|只负责验证|只能对照|能解释库指令|若只有VA表)"),
    re.compile("(?:" + "|".join((
        "GPU ISA" + "模型的访存hook", "A" + "小用例oracle",
        "UMD/" + "模型", "模型事件" + "重放产品方案"
    )) + ")"),
]
# 验证审计规则本身：必须抓出废弃实现，同时保留真正的对象A/B边界案例。
for example in ("独立" + "A校验", "路线" + "A的旧条目",
                "A" + "同时模拟命令DAG",
                "模型" + "来源追踪只能辅助验证",
                "A" + "按动态warp", "A" + "记录capture节点"):
    check(any(pat.search(example) for pat in retired_meaning_patterns),
          "语义防回归规则不再覆盖旧方案示例: " + example)
for example in ("对象 A 的派生指针落入 B", "GPU软件记录对象 A 的原始base/size"):
    check(not any(pat.search(example) for pat in retired_meaning_patterns),
          "语义防回归不能误删S04对象A/B边界语义")
for p in ROOT.rglob("*"):
    if not p.is_file() or p.suffix not in text_suffixes or ".git" in p.parts:
        continue
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeError:
        continue
    for pat in retired_meaning_patterns:
        hit = pat.search(content)
        if hit:
            FAILURES.append(f"遗留旧方案实现描述：{p.relative_to(ROOT)}，内容片段 {hit.group(0)}")
            break

main = read("docs/llvm-gpu-sanitizer-research.md")
guide = read("docs/S01-vendor-implementation-guide.md")
implant = read("docs/real-gpu-components-implementation.md")
audit = read("docs/hardware-deployability-audit.md")
root_readme = read("README.md")
budget = load_json("docs/estimates-v1.8.json")
report = load_json("docs/validation-report.json")
manifest = load_json("samples/manifest.json")

canonical = read("docs/core-design-comparison.md")
# 现行分类仍为2厂商、3参考配置、2级空间语义；删掉废弃选项后不重计工程包。
check("**2**" in canonical and "**3**" in canonical and "**2层**" in canonical,
      "分类须明确2厂商/3参考配置/2种空间保证层级")
for mode, signal in (
    ("NV-R", "Runtime Binary"),
    ("AMD-B2", "Shadow/Redzone"),
    ("NV-C", "Original-Object"),
):
    check(mode in canonical and signal in canonical,
          "核心空间参考配置缺失或算法不符：" + mode)
check("NVIDIA" in canonical and "AMD" in canonical
      and "Intel" in canonical and "MUSA" in canonical,
      "主要厂商与额外功能参考须正确分层")
check("NV-R与AMD-B2" in canonical and "当前地址" in canonical,
      "NV-R与AMD-B2的当前地址有效性语义不得误标为原对象Bounds")
check("GPU_NOT_RUN" in canonical and "D2" in canonical,
      "不得隐藏目标GPU缺失验收与D2时间身份边界")
for filename in (
    "README.md", "docs/S01-vendor-implementation-guide.md",
    "docs/llvm-gpu-sanitizer-research.md",
    "docs/amd-gpu-asan-hardware-fallback.md",
    "docs/hardware-deployability-audit.md",
    "docs/real-gpu-components-implementation.md",
    "docs/research-progress.md", "docs/revision-history.md",
    "docs/research-notes/architecture.md",
    "docs/research-notes/papers.md",
    "docs/research-notes/vendors.md",
    "docs/gpu-actions-ci.md", "samples/README.md",
):
    check("core-design-comparison.md" in read(filename),
          "相关资料必须引用唯一三配置核心设计入口：" + filename)
references = budget.get("reference_configurations", {})
check(set(references) == {"NV-R", "AMD-B2", "NV-C"},
      "预算只能标明三种核心参考配置")
check(budget.get("reference_vendor_count") == 2
      and budget.get("reference_configuration_count") == 3
      and len(budget.get("independent_spatial_predicate_levels", [])) == 2,
      "2厂商、3配置、2层空间判断口径必须一致")
check(budget.get("product_source_of_truth") == "docs/core-design-comparison.md"
      and "not five independent" in budget.get("delivery_budget_labels_note", "").lower(),
      "B/C/D/E/F须是工程预算包而非五套独立Checker算法")
check(all(references.get(key, {}).get("exact_vendor_copy") is False
          for key in ("NV-R", "AMD-B2", "NV-C")),
      "不得声称完整复刻NVIDIA/AMD私有内部实现")
taxonomy = budget.get("route_taxonomy", {})
check(taxonomy.get("B1", "").lower().find("current-live") >= 0
      and "integration" in taxonomy.get("E", "").lower(),
      "B1只是当前Live变体，E是集成策略")
check("F1" in budget.get("eligibility", "") and "F2" in budget.get("eligibility", "")
      and "F1" in budget.get("hardware_delivery", {})
      and "F2" in budget.get("hardware_delivery", {}),
      "条件性硬件候选必须具有真实Tag/Bounds实施前提")
check("### 2.4 无 XNACK" in guide and "B2_SHADOW" in guide
      and "B1_CURRENT_LIVE_INTERVAL" in guide
      and "D1_ORIGINAL_OBJECT_BOUNDS" in guide,
      "无XNACK路径必须保留B2/B1/D1真实GPU软件检查说明")
check("### 3.1 B1（原I）" in guide
      and "### 3.2 条件性芯片能力" in guide
      and "### 3.3 E：" in guide,
      "S01只允许B1实现、真实Tag/Bounds条件和E集成")
check("### 3.1 B1（原I）" in guide and "原对象" in guide
      and "当前 GPUVA" in guide and "NV-C/D1" in guide,
      "B1地址表必须区别于D1原对象Bounds")
check("I是B1" in guide and "E是混合交付策略" in guide,
      "旧I与E的归属不能回退")
check("**2**" in canonical and "对象" in canonical,
      "删除旧路线不得误删合法对象边界示例")

# 禁止已撤销的辅助路线、独立工具调参/缓存目录和对应实验重新入库。
# 故意拆开敏感字面量，避免静态源自检错误；技术上必须保留AMD-B2
# 核心Shadow/Redzone算法说明，但不能把它再当独立候选行。
removed_text = [
    re.compile(r"(?<![A-Za-z0-9_])F" + "0(?![A-Za-z0-9_])", re.I),
    re.compile("guard" + r"[\s_-]*" + "page", re.I),
    re.compile("quaran" + "tine", re.I),
    re.compile(r"(?<![A-Za-z])" + "padding" + r"(?![A-Za-z])", re.I),
    re.compile("分段" + r"[\s\S]{0,6}" + "Shadow", re.I),
    re.compile("多段" + r"[\s\S]{0,6}" + "shadow", re.I),
    re.compile("多级" + r"[\s\S]{0,6}" + "稀疏" + r"[\s\S]{0,5}" + "shadow", re.I),
]
current_docs = ([ROOT / "README.md"] +
                list((ROOT / "docs").rglob("*.md")) +
                list((ROOT / "docs").rglob("*.json")) +
                list((ROOT / "samples").rglob("README.md")))
for doc in current_docs:
    body = doc.read_text(encoding="utf-8")
    for pat in removed_text:
        match = pat.search(body)
        if match:
            FAILURES.append(f"文档残留已撤销路线/附属策略：{doc.relative_to(ROOT)} -> {match.group(0)}")
            break
check(not (ROOT / "samples" / "experiments").exists(),
      "已废弃的独立GPU实验目录不应存在")
check(all("auxiliary" not in str(x).lower() for x in budget.get("nonstandalone_features", [])),
      "预算不得重新列入已删除的辅助检查产品")
# 本项不删除目前实际必要的GPUVA/BO和Poison逻辑，不把额外
# 设备内存映射信息自动变成独立Checker。

amd_guide = read("docs/amd-gpu-asan-hardware-fallback.md")
for required in (
    "XNACK", "HMM", "无XNACK", "Compiler",
    "LLVM", "Device Runtime", "UMD", "KMD", "Firmware", "RTL",
    "Shadow", "Interval", "GPU", "TOOL_FAILURE", "S01",
):
    check(required.lower() in amd_guide.lower(),
          "AMD指南未解释硬件缺失路径或组件: " + required)
check(("无 HMM" in amd_guide or "无HMM" in amd_guide)
      and ("缺少专用 Shadow 硬件指令" in amd_guide or "无硬件" in amd_guide),
      "AMD指南必须解释缺少HMM及Shadow硬件指令的可部署软件路径")
# XNACK与Shadow三种映射取舍必须保留，避免把GPU缺页恢复误说成Shadow合法性判断。
for required in (
    "### 2.1 XNACK 的具体功能",
    "### 2.2 没有 XNACK",
    "可恢复缺页",
    "GPUVA 虚拟地址预留",
    "① 全量物理 Shadow 预分配",
    "② 随 `gpuMalloc` 分配 Shadow",
    "③ 按需物理 Shadow + 安全的两级 Shadow 索引",
    "未建立 Shadow 映射",
    "XNACK 也不能自动判断 Shadow 的合法性",
):
    check(required in amd_guide,
          "AMD缺XNACK指南遗漏关键功能或三项Shadow映射取舍: " + required)
check("三种核心取舍" in guide and "可恢复 GPU Page Fault" in guide
      and "gpuMalloc" in guide and "amd-gpu-asan-hardware-fallback.md" in guide,
      "S01指南需引用XNACK含义及三项Shadow映射取舍")
check("不新增第四种" in canonical,
      "核心分类不能把XNACK Shadow映射策略另算新算法")

# AMD硬件专题只保留Xnack/HMM与Shadow映射的硬件特有内容，通用设备代码须只有一个权威位置。
amd_hardware_chapters = [int(n) for n in re.findall(r"^## (\d+)\. ", amd_guide, re.M)]
check(amd_hardware_chapters == list(range(1, 13)),
      "AMD硬件适配指南必须保留1～12节与原有章节锚点")
for required in (
    "### 2.1 XNACK 的具体功能",
    "### 2.2 没有 XNACK 且没有等效缺页重试机制",
    "### 2.3 无 XNACK 下的 Shadow 页有效性前置检查",
    "Page Fault 不代表每次都新分配物理内存",
    "HMM（Linux Heterogeneous Memory Management）不是 XNACK",
    "① 全量物理 Shadow 预分配",
    "② 随 `gpuMalloc` 分配 Shadow",
    "③ 按需物理 Shadow + 安全的两级 Shadow 索引",
    "GPUVA 虚拟地址预留", "稀疏 GPUVA",
    "safeShadowLookupNoFault",
    "## 12. CI、S01样例、无XNACK实验及验收次序",
    "-fsanitize=address -shared-libsan",
    "real-gpu-components-implementation.md",
):
    check(required in amd_guide,
          "AMD硬件专题不能丢失XNACK/HMM/Shadow三取舍、映射安全与官方运行方式: " + required)
for deprecated_duplicate in (
    "SanMode chooseSanitizer(",
    "for (auto& insn : device_fn.instructions())",
    "__device__ SanVerdict check_access(",
    "Allocation san_alloc(",
    "SubmitResult submit_checked(",
    "DeviceResult on_san_error(",
    "Result aggregate_gpu_result(",
):
    check(deprecated_duplicate not in amd_guide,
          "AMD硬件专题重复维护通用实现代码: " + deprecated_duplicate)
check(implant.count("safeShadowLookupNoFault(") == 1
      and "REGISTERED_BUT_NOT_MAPPED" in implant
      and "UNKNOWN_ADDRESS_SPACE" in implant
      and "GPU Global Shadow 的安全查找契约" in implant,
      "设备运行库必须统一处理未映射Shadow、未知地址及工具元数据失败")
check("export ROCM_ARCH=gfx90a" not in guide
      and "-shared-libsan" in guide
      and "amd-gpu-asan-hardware-fallback.md" in guide,
      "S01需引用AMD的唯一官方构建命令，不能再复制完整脚本")
check(report.get("hardware_guide_dedup", {}).get("hardware_source")
      == "docs/amd-gpu-asan-hardware-fallback.md"
      and report.get("hardware_guide_dedup", {}).get("implementation_source")
      == "docs/real-gpu-components-implementation.md",
      "硬件适配与通用Device Checker源码应在审计中分属唯一文档")

# 文档索引与 Intel 正式专题的覆盖防回归，不把厂商能力归为第四种基础空间算法。
docs_index = read("docs/README.md")
intel_guide = read("docs/intel-oneapi-device-sanitizers.md")
vendor_registry = load_json("docs/research-notes/vendors_sources.json")
vendor_notes = read("docs/research-notes/vendors.md")

# 第四阶段：厂商一手证据用统一 Profile 表达，不能只改静态 Markdown 表格。
profiles = vendor_registry.get("vendor_profiles", [])
expected_profiles = (
    "nvidia-compute-sanitizer",
    "amd-rocm-gpu-asan",
    "intel-oneapi-device-sanitizers",
    "moore-threads-musa-compute-sanitizer",
)
check(vendor_registry.get("schema_version") == 2
      and isinstance(profiles, list)
      and [p.get("profile_id") for p in profiles if isinstance(p, dict)]
          == list(expected_profiles),
      "厂商证据必须有固定顺序、唯一的四个版本化 Profile（不增加第四种算法）")
source_records = vendor_registry.get("sources", [])
source_record_ids = [x.get("id") for x in source_records if isinstance(x, dict)]
check(len(source_record_ids) == len(set(source_record_ids))
      and len(source_record_ids) >= 16,
      "厂商官方来源 ID 必须唯一并保留已有的官方来源")
required_profile_strings = (
    "profile_id", "vendor", "product", "version_baseline",
    "evidence_checked_at", "canonical_detail", "verification_status"
)
required_profile_lists = (
    "hardware_requirements", "instrumentation", "runtime_requirements",
    "detector_scope", "known_limitations", "source_ids"
)
for profile in profiles:
    if not isinstance(profile, dict):
        check(False, "厂商 Profile 记录必须为object")
        continue
    vendor_name = profile.get("vendor", "<unknown>")
    for field in required_profile_strings:
        check(isinstance(profile.get(field), str)
              and bool(profile.get(field).strip()),
              f"{vendor_name} 缺少非空版本化证据字段 {field}")
    for field in required_profile_lists:
        check(isinstance(profile.get(field), list)
              and bool(profile.get(field))
              and all(isinstance(t, str) and t.strip()
                      for t in profile.get(field, [])),
              f"{vendor_name} 缺少结构化列表字段 {field}")
    check(set(profile.get("source_ids", [])) <= set(source_record_ids),
          f"{vendor_name} Profile 来源未登记在 sources[].id")
    check(str(profile.get("verification_status", "")).find("USER_TARGET_GPU_NOT_RUN") >= 0,
          f"{vendor_name} 厂商公开能力不可冒充自研GPU已运行")
    detail = str(profile.get("canonical_detail", ""))
    check(detail.startswith("docs/") and (ROOT / detail).is_file(),
          f"{vendor_name} 指向不存在的唯一厂商详细资料：{detail}")
    check(bool(re.fullmatch(r"20\d\d-\d\d-\d\d",
                            str(profile.get("evidence_checked_at", "")))),
          f"{vendor_name} 缺少精确来源核验日期")
expected_routes = {
    "nvidia-compute-sanitizer": ["NV-R", "NV-C"],
    "amd-rocm-gpu-asan": ["AMD-B2"],
    "intel-oneapi-device-sanitizers": [],
    "moore-threads-musa-compute-sanitizer": [],
}
for profile in profiles:
    if not isinstance(profile, dict):
        continue
    identifier = profile.get("profile_id")
    check(profile.get("reference_configurations") == expected_routes.get(identifier),
          f"{identifier} 的成熟参考配置分类被错误增减")
check("AMD-LLNL" in source_record_ids
      and "AMD-PREVIEW-713" in source_record_ids
      and any("preview" in str(s.get("caution", "")).lower()
              for s in source_records if isinstance(s, dict)
              and s.get("id") == "AMD-PREVIEW-713"),
      "必须区分 AMD 稳定版本、LLNL 现场指南与 Technology Preview")
check("INTEL-UR" in source_record_ids
      and any("adapter" in str(s.get("caution", "")).lower()
              for s in source_records if isinstance(s, dict)
              and s.get("id") == "INTEL-UR"),
      "UR通用规范不能代替Intel设备产品版本的支持结论")
check(len(vendor_notes) < 5100
      and all(f"## {i}. " in vendor_notes for i in range(1, 9))
      and "vendors_sources.json" in vendor_notes,
      "历史厂商笔记必须为简短的8节导航/纠正说明，不得恢复完整重复表")
main_vendor_start = main.index("### 4.1 NVIDIA Compute Sanitizer")
main_vendor_end = main.index("### 4.5 LLVM 上游", main_vendor_start)
main_vendor_text = main[main_vendor_start:main_vendor_end]
check(len(main_vendor_text) < 6200
      and all(x in main_vendor_text for x in (
          "#### 4.1.1", "#### 4.1.2", "#### 4.1.3",
          "### 4.2 AMD", "### 4.3 Intel", "### 4.4 MUSA")),
      "主报告第4章厂商机制应有摘要与原有锚点，但不能恢复重复的完整厂商手册")
main_catalog = main.split("### 4.9 对每种产品机制最有价值的源码／官方证据入口")[1]
main_catalog = main_catalog.split("## 5. 核心论文", 1)[0]
check(len(main_catalog) < 2400 and "~~~bash" not in main_catalog
      and "vendors_sources.json" in main_catalog,
      "主报告 §4.9～4.10 不得复制可漂移的第二套版本表和编译命令")
check(report.get("vendor_evidence_normalization", {}).get("profiles") == 4
      and report.get("vendor_evidence_normalization", {}).get("source") ==
          "docs/research-notes/vendors_sources.json"
      and report.get("vendor_evidence_normalization", {}).get("target_gpu_status") ==
          "GPU_NOT_RUN",
      "v2.6 审计必须登记唯一厂商证据、四类Profile及未运行的目标GPU")
check(all("research-notes/vendors_sources.json" in x for x in
          (root_readme, canonical, main, intel_guide, amd_guide, guide, docs_index)),
      "根导航、核心分类、主稿、三厂商专题必须引用统一结构化证据登记")

for required in ("core-design-comparison.md", "intel-oneapi-device-sanitizers.md",
                 "llvm-gpu-sanitizer-research.md", "real-gpu-components-implementation.md",
                 "samples/README.md", "gpu-actions-ci.md", "vendors_sources.json"):
    check(required in docs_index, "统一文档导航缺少入口: " + required)
for required in ("ASan", "MSan", "TSan", "Ponte Vecchio", "SYCL", "OpenMP",
                 "Unified Runtime", "Level Zero", "2026.0", "-fsanitize=address",
                 "-fsanitize=memory", "-fsanitize=thread", "串行", "跳过", "GPU_NOT_RUN"):
    check(required in intel_guide, "Intel 技术专题缺少公开限制或实现入口: " + required)
vendor_ids = {s.get("id") for s in vendor_registry.get("sources", []) if isinstance(s, dict)}
check({"INTEL-DEVICE", "INTEL-LIMITS", "INTEL-UR"} <= vendor_ids,
      "Intel 专题应登记编译器功能、系统限制和 UR 的三项官方来源")
check("intel-oneapi-device-sanitizers.md" in root_readme
      and "intel-oneapi-device-sanitizers.md" in canonical
      and "intel-oneapi-device-sanitizers.md" in main
      and "intel-oneapi-device-sanitizers.md" in vendor_notes,
      "主入口、唯一分类、主研究和历史笔记必须引用同一个 Intel 专题")

check("amd-gpu-asan-hardware-fallback.md" in root_readme
      and "amd-gpu-asan-hardware-fallback.md" in guide
      and "amd-gpu-asan-hardware-fallback.md" in main,
      "README/S01/主稿必须共同链接AMD缺硬件路径")


# 当前文稿、实际GPU执行入口与全部生产责任主体。
chapters = [int(x) for x in re.findall(r"^## (\d+)\. ", main, re.M)]
check(chapters == list(range(1, 24)), "主稿章节应为1–23，不能缺章/重复")
check("## 9. 真实GPU部署准入" in main, "§9应要求实体GPU检测生效")
check("### 18.29 28个场景的真实GPU生产职责与验收映射" in main,
      "§18.29必须有28个真实GPU组件映射")
check("Compiler" in implant and "Firmware" in implant and "RTL" in implant
      and "KMD" in implant and "UMD" in implant and "GPU Device Runtime" in implant,
      "逐组件指南必须包含Compiler/Device Runtime/UMD/KMD/Firmware/RTL")
for num in range(2, 12):
    check(bool(re.search(rf"^## {num}\. ", implant, re.M)),
          f"逐组件指南缺少§{num}代码/汇总章节")
check("Runtime Binary Rewriter" in implant and "生产 Compiler" in implant,
      "纯C的独立UMD Rewriter与生产Compiler职责缺失")
check("S01-0 **真实GPU设备代码原型**" in guide,
      "S01首次必须交付可在GPU实际运行的设备代码")
# S01 厂商差异与通用组件彻底去重；防止再次将泛用 ABI、重写器与 LLVM Pass 复制到 S01。
check(len(guide) <= 21000,
      "S01 厂商机制专题过长：应把通用组件代码移到 real-gpu-components-implementation.md")
for required in (
    "### 1.2 路径 NVIDIA-R", "### 1.3 路径 NVIDIA-C",
    "### 2.2 1:8 Shadow", "### 2.4 无 XNACK",
    "### 3.1 B1（原I）", "### 3.3 E：",
    "-fdevice-sanitize=memcheck", "-shared-libsan",
    "compute-sanitizer --tool memcheck", "唯一详细案例规范",
    "real-gpu-components-implementation.md",
):
    check(required in guide, "S01去重不允许丢失厂商机制/工具命令/稳定小节: " + required)
for duplicated in (
    "struct SanAccessEvent {", "struct SanObject {",
    "struct SanReport {", "struct PointerMeta {",
    "PatchedModule patch_module(", "SanVerdict check_shadow(",
    "### S01最小验收矩阵",
):
    check(duplicated not in guide, "S01不得重复定义案例或通用组件源码: " + duplicated)
for required in (
    "### 1.1 Sanitizer 共用元数据与报告 ABI",
    "struct SanAccessEvent {", "struct SanObject {",
    "struct SanReport {", "struct PointerMeta {",
    "splitAndGuardMemoryOp(", "withinOriginalObject(",
    "B2_SHADOW", "B1_CURRENT_LIVE_INTERVAL", "D1_ORIGINAL_OBJECT_BOUNDS",
):
    check(required in implant, "通用组件指南缺失唯一代码定义或模式协商: " + required)
check("S01/README.md" in guide and "samples/manifest.json" in guide,
      "S01厂商指南必须把详细场景和厂商预期交给各自权威来源")
check(report.get("s01_guide_dedup", {}).get("implementation_source")
      == "docs/real-gpu-components-implementation.md"
      and report.get("s01_guide_dedup", {}).get("case_source")
      == "samples/S01/README.md",
      "文档审计必须声明S01厂商机制、通用组件、唯一案例各自归属")
check("GPU_NOT_RUN" in audit and "UNSUPPORTED" in audit,
      "硬件审计必须声明真实GPU未运行与不可用能力")
check("real-gpu-components-implementation.md" in root_readme,
      "README必须链接逐组件修改代码")

# 第18章只保留稳定摘要，S01～S28 独立README是唯一详细规范。
# 不允许将原来的28份完整伪代码和厂商结果矩阵重新复制到主稿。
headings = re.findall(r"^### 18\.(\d+)\s+S(\d{2})[：:]", main, re.M)
expected = [f"S{i:02d}" for i in range(1, 29)]
check(len(headings) == 28, "主稿必须恰好保留18.1～18.28的28个场景标题")
for i, (seq, suffix) in enumerate(headings, start=1):
    check(int(seq) == i and int(suffix) == i, f"主稿场景标题或顺序异常：S{i:02d}")

check("### 18.29 28个场景的真实GPU生产职责与验收映射" in main
      and "### 18.30 CUDA/AMD HIP 真实设备样例与运行入口" in main,
      "跨案例生产职责和设备样例入口必须保留")
check(len(manifest.get("cases", [])) == 28, "samples/manifest.json需要28案例")
check([x.get("id") for x in manifest.get("cases", [])] == expected,
      "manifest 的28案例顺序须与主稿一致")
sample_index = read("samples/README.md")
check("唯一详细技术说明" in sample_index and "manifest.json" in sample_index,
      "样例索引必须声明独立README及manifest为对应唯一规范")

start_group = main.find("### 18.29 28个场景的真实GPU生产职责与验收映射")
end_group = main.find("### 18.30 CUDA/AMD HIP 真实设备样例与运行入口", start_group)
group_section = main[start_group:end_group] if start_group >= 0 and end_group >= 0 else ""
group_links = re.findall(r"\[(S\d{2})\]\(#case-(s\d{2})\)", group_section)
check(len(group_links) == 28 and set(group_links) ==
      {(case, case.lower()) for case in expected},
      "§18.29必须通过按检查类别分组的链接恰好覆盖28例，不能重漏")
check("| 场景 | CUDA完整源码 |" not in main
      and "| 场景 | 真实GPU检查与验收关键点 |" not in main,
      "主稿不得恢复重复的28例源码矩阵及逐例职责矩阵")

for i, case in enumerate(expected, start=1):
    anchor = f'<a id="case-{case.lower()}"></a>'
    next_anchor = (f'<a id="case-s{i+1:02d}"></a>' if i < 28
                   else '<a id="scenario-platforms"></a>')
    st = main.find(anchor)
    en = main.find(next_anchor, st + len(anchor)) if st >= 0 else -1
    section = main[st:en] if st >= 0 and en >= 0 else ""
    check(main.count(anchor) == 1 and
          f"### 18.{i} {case}" in section,
          f"{case}的原主稿锚点与标题必须保留且唯一")
    check(len(section.splitlines()) <= 13 and "~~~" not in section,
          f"{case}主稿摘要过长或又出现重复代码，请维护到案例README")
    for suffix in ("README.md", "cuda.cu", "amd.hip.cpp"):
        check(f"../samples/{case}/{suffix}" in section,
              f"{case}主稿必须一对一链接 {suffix}")
    check(f"[{case}]({case}/README.md)" in sample_index,
          f"样例索引缺少 {case} 的独立详细规范入口")

    doc = read(f"samples/{case}/README.md")
    check(f"### 18.{i} {case}" in doc,
          f"{case}唯一详细规范缺少对应场景的完整技术说明")
    check("唯一详细技术与验收规范" in doc
          and f"../../docs/llvm-gpu-sanitizer-research.md#case-{case.lower()}" in doc,
          f"{case}必须声明自己是唯一详细规范并反链原有主稿锚点")
    check("以下章节直接对应[主调研文档" not in doc,
          f"{case}不能再把主稿摘要声明为正式详细验收规范")
    check("## 验收需要留下什么证据？" in doc and "GPU_NOT_RUN" in doc,
          f"{case}必须保留实卡验收口径与未运行状态")
    check("## 实体GPU生产组件职责" in doc or case == "S01",
          f"{case}必须包含真实设备Checker和UMD/KMD职责")
    for filename in ("README.md", "cuda.cu", "amd.hip.cpp"):
        check((ROOT / "samples" / case / filename).is_file(),
              f"{case}缺少源文件：{filename}")

check("S01/README.md" in guide and "唯一详细案例规范" in guide,
      "S01厂商指南须链接单一案例技术规范，不另行维护重复64B案例表")
check(report.get("scenario_documentation", {}).get("canonical_case_readmes") == 28,
      "文档审计快照需要登记28个唯一详细案例README及第18章瘦身状态")

# 只计算可交付生产软件，C纯Runtime的Compiler新增源码可以为零。
routes = budget.get("production", {}).get("routes", {})
check(set(routes) == {"B", "C", "D", "E", "F"},
      "预算产品方案只能是B/C/D/E/F")
check(budget.get("verification", {}).get("hardware_benchmark_status") in (None, "NOT_RUN"),
      "不得声明不存在的GPU硬件测量")
for key, route in routes.items():
    try:
        totals = route["productionTotals"]
        comps = [route["Compiler"], route["KMD"], route["UMD"]]
        for kind in ("core", "test"):
            expected_sum = add_range(*(v[kind] for v in comps))
            check(totals[kind] == expected_sum, f"{key} {kind}按生产组件加总不符")
        check(totals["combined"] == add_range(totals["core"], totals["test"]),
              f"{key}的核心＋测试不符")
    except (KeyError, TypeError) as e:
        FAILURES.append(f"{key}生产预算结构缺失: {e}")

check(routes.get("C", {}).get("Compiler", {}).get("core") == [0, 0],
      "纯Runtime C的生产Compiler源码预算必须为零")
split = budget.get("hybrid_E_dedup", {})
try:
    for kind in ("core", "test"):
        check(split["total"][kind] ==
              add_range(*(split[k][kind] for k in
                          ("P", "B_increment", "D_unique", "C_subset", "cross_mode"))),
              f"E跨模式去重公式{kind}不一致")
        check(split["total"][kind] == routes["E"]["productionTotals"][kind],
              f"E去重合计和总体预算{kind}不一致")
except (KeyError, TypeError) as e:
    FAILURES.append(f"E去重预算字段错误：{e}")
for k, obj in budget.get("additional_checkers", {}).items():
    try:
        for kind in ("core", "test"):
            check(obj["total"][kind] ==
                  add_range(obj["Compiler"][kind], obj["UMD"][kind]),
                  f"额外checker {k}/{kind}预算加总不符")
    except (KeyError, TypeError) as e:
        FAILURES.append(f"额外checker {k}字段缺失：{e}")

# 报告与当前主稿内容保持一致；仅称文档结构审计，不宣称GPU功能验收。
main_bytes = main.encode("utf-8")
blob_sha = hashlib.sha1(
    f"blob {len(main_bytes)}\0".encode("ascii") + main_bytes
).hexdigest()
check(report.get("main_document_blob_sha") == blob_sha,
      "审计报告引用的主稿SHA已过期")
check(report.get("main_line_count") == len(main.splitlines()),
      "审计报告引用的主稿行数已过期")
check(report.get("amd_hardware_fallback", {}).get("document")
      == "docs/amd-gpu-asan-hardware-fallback.md",
      "报告中缺少AMD硬件依赖及软件替代路径审计")
check(report.get("legacy_text_cleanup", {}).get("static_semantic_regression")
      == "scripts/validate_docs.py",
      "报告未登记历史方案语义防回归检查")
check(report.get("product_routes") == ["B", "C", "D", "E", "F"],
      "审计报告产品路线不一致")
check(report.get("physical_gpu_checker_result") == "GPU_NOT_RUN",
      "尚无自研目标GPU运行结果，不得标称已检出")
check(report.get("chapter_count") == len(chapters) and
      report.get("scenarios_count") == len(headings),
      "审计报告的章节或场景数量已过期")

# Markdown相对链接必须指向真实存在的仓库文件/目录；不依赖历史预算。
markdowns = [ROOT / "README.md"] + list((ROOT / "docs").rglob("*.md")) + list((ROOT / "samples").rglob("README.md"))
for doc in markdowns:
    inspect(doc)
    txt = doc.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)\n]+)\)", txt):
        target = target.split(' "', 1)[0]
        if target.startswith(("#", "/", "http:", "https:", "mailto:", "data:")):
            continue
        local = unquote(target.split("#", 1)[0])
        check((doc.parent / local).exists(),
              f"失效链接：{doc.relative_to(ROOT)} -> {target}")

if FAILURES:
    for error in FAILURES:
        print("DOC_AUDIT_FAILURE:", error, file=sys.stderr)
    sys.exit(1)

print("DOC_AUDIT_PASS: 三配置核心设计、已删除路线/附属策略、23章、28例、预算及链接均通过静态校验")
print("注意：此脚本未编译或运行自研GPU检查器，不产生真实性能或设备检出结论")
