# GPU Sanitizer 厂商证据：版本化对照与历史核验记录

> **标准化证据字段的唯一机器可读来源：** [vendors_sources.json](vendors_sources.json) 的 `vendor_profiles` 与各 `sources[].id`。本页仅是简短中文索引及历史修正记录，不再并行维护第二张完整版能力矩阵。技术路线仍按[三种空间参考配置](../core-design-comparison.md)分类，不因 Intel/MUSA 有不同工具功能就增加基础空间算法。检查日期依每条 Profile 的 `evidence_checked_at`；旧笔记的 2026-10-20 不等于所有官方页面今天再次发布。

## 1. 版本基线及纠正点

| 成熟产品 / 角色 | 已核验的版本化来源 | 关键平台条件 | 需要特别保留的限制 | 证据和详细实现 |
|---|---|---|---|---|
| NVIDIA Compute Sanitizer / 核心参考 NV-R、NV-C | 2026.3 Release Notes、CUDA 13.4 NVCC、CUDA 13.1+ 编译期能力 | 受支持 CUDA GPU + 同版本工具链 | 仅 memcheck 有编译期模式；同模块函数级 Hybrid 不能假设补齐 | `NV-MANUAL` / `NV-RELEASE` / `NV-NVCC`；[S01 厂商指南](../S01-vendor-implementation-guide.md) |
| AMD ROCm GPU ASan / 核心参考 AMD-B2 | 版本固定 ROCm 7.2.4；LLNL 7.0.0 与预览 7.13.0 分开 | 官方部署要求支持的 `xnack+` / Linux HMM / 适配 Runtime | 未支持地址空间、有限 Redzone、Private/Stack 缺口；不可套用预览缺陷 | `AMD-ASAN-724`；[AMD 硬件适配](../amd-gpu-asan-hardware-fallback.md) |
| Intel oneAPI Device Sanitizers / 辅助体系参考 | DPC++/C++ 2026.0；UR 独立规范 | Linux + 官方列举的 Ponte Vecchio 单卡 | ASan Kernel 串行；大 Workgroup 数可能跳过部分 Local/Private 检查 | `INTEL-DEVICE` / `INTEL-LIMITS` / `INTEL-UR`；[Intel 专题](../intel-oneapi-device-sanitizers.md) |
| 摩尔线程 MUSA Compute Sanitizer / 公开行为参考 | v1.1.1 说明与现有指南（日期不完整） | 按其具体 Runtime/Driver/设备支持表 | 内部 Shadow/二进制插桩机制未披露；特殊访存覆盖不应猜测 | `MUSA-USER` / `MUSA-111`；[官方来源登记](vendors_sources.json) |

上表是简短导航，**不是第二份厂商详细支持声明**。对任一限制的精确表述，以 Profile 所列版本、对应官方来源 ID 和专题为准。资料引用不能把厂商已发布的功能、项目自行设计的方案与实际 GPU 检出记录混淆。

## 2. 能力比较（按文档实际范围）

统一核查维度已写入 `vendor_profiles`：`version_baseline`、`hardware_requirements`、`instrumentation`、`runtime_requirements`、`detector_scope`、`known_limitations`、`source_ids` 和 `verification_status`。产品能力差异由[核心方案分类](../core-design-comparison.md)和各专题解释；“未建立”表示目前没有可采信的公开证据，而不是反向证明产品完全不支持某功能。

## 3. NVIDIA：可直接用于架构讨论的事实

### 3.1 编译期功能与对象边界

**历史关键修正：** 编译期 Memcheck 的 Original-Object Base-and-Bounds 与默认 NV-R 当前地址有效性并非同等级保证；来源推导失败必须降级。`nvcc` 版本与 Compute Sanitizer 配套，论文 cuCatch 的性能数字不属于商业产品性能证明。具体算法、Hybrid/JIT 限制与命令仅由 [S01 技术指南](../S01-vendor-implementation-guide.md) 和 `NV-*` 官方来源维护。

### 3.2 内存与性能边界

Runtime 跟踪与设备插桩可能增加 Host/GPU 元数据及寄存器资源，工具导致的 Launch 丢失或串行化会改变覆盖结论；不能以一次 CLEAN 证明完整检测。版本化限制归 `NV-MANUAL` / `NV-RELEASE`。

### 3.3 公共 API 可借鉴的接口分层（非自研现状声明）

官方 Callback / Patching / Memory API 是可核实的公开接口分类，并非内部完整 Runtime Rewriter 开源。自研 UMD/KMD 的唯一实施说明参见 [组件指南](../real-gpu-components-implementation.md)，不要从 Patch API 推断 NVIDIA 内部的私有索引结构。

### 3.4 规范链接

[NVIDIA 手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)、[Sanitizer API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html)、[版本化证据 ID](vendors_sources.json)。

## 4. AMD 与 LLNL：要把编译、运行库、系统能力一起说清

**历史关键修正：** AMD 官方 GPU ASan 的 `xnack+`/HMM 环境要求与自研软件 Shadow 的硬件前提不能混为一谈；LLNL 的 `rocmcc/7.0.0` 现场 Known Issues 不能代表 ROCm 7.2.4 所有系统。完整 XNACK、HMM、Shadow 三种映射取舍只维护在 [AMD 硬件适配专题](../amd-gpu-asan-hardware-fallback.md)，版本来源为 `AMD-ASAN-724`、`AMD-LLNL`。

## 5. MUSA：公开样本的价值与边界

官方指南只能证明公开的 Global OOB、API Error、独立 Leak 检查等功能，**不能推出**采用与 NVIDIA/AMD 相同的私有插桩、Shadow 或来源分析。详情以 `MUSA-USER` / `MUSA-111` 为准。

## 6. Intel：覆盖丰富，但配置与检查退化必须一起列

**历史关键修正：** Intel 2026.0 的设备 ASan/MSan/TSan 是成熟 Compiler + Unified Runtime 体系，但 GPU 平台、SYCL/OpenMP Runtime、并发串行及可跳过检查的范围均需附带版本条件。完整能力与命令仅维护在 [Intel 技术专题](../intel-oneapi-device-sanitizers.md)，不能从独立 UR 规范的 *planned* 表述倒推出所有产品版本均不可使用。

## 7. 对最终文稿的证据使用建议

调查结论只分为 **官方公开并版本化的事实 / 本项目工程推断与设计 / 已实现源码 / 真实硬件验收** 四类。产品“支持”需要同时注明版本、设备条件、已检查空间、被跳过范围和实际执行证据；单靠静态 CI 或厂商文档不能构成目标 GPU 的 `GPU_DETECTED`。

## 8. 关键来源导航

一手来源、独立 Runtime 规范、现场指南、研究论文等全部记录在 [vendors_sources.json](vendors_sources.json)。新增厂商资料应先更新 `sources[].id` 及 `vendor_profiles`，再修改专题或主稿摘要，避免产生另一份无法追踪的支持矩阵。
