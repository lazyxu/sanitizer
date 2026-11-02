# GPU Sanitizer 厂商证据：版本化对照与历史核验记录

> **标准化证据字段的唯一机器可读来源：** [vendors_sources.json](vendors_sources.json)的`vendor_profiles`与`sources[].id`。本页是导航及修正记录；基础空间分类仍为2家主要厂商、3种参考配置、2层语义。2026-11-02复核NVIDIA、AMD和Intel；MUSA保留此前核验日期。**文档核验日期、文档版本和产品发布日期分别登记**，不因重读网页就更新产品发布日期。

## 1. 版本基线及纠正点

| 成熟产品 / 角色 | 已核验的版本化来源 | 关键平台条件 | 需要特别保留的限制 | 证据和详细实现 |
|---|---|---|---|---|
| NVIDIA Compute Sanitizer / 核心参考 NV-R、NV-C | 2026.3 Release Notes、CUDA 13.4 NVCC、CUDA 13.1+ 编译期能力 | 受支持 CUDA GPU + 同版本工具链 | 仅 memcheck 有编译期模式；同模块函数级 Hybrid 不能假设补齐 | `NV-MANUAL` / `NV-RELEASE` / `NV-NVCC`；[S01 厂商指南](../S01-vendor-implementation-guide.md) |
| AMD ROCm GPU ASan / 核心参考 AMD-B2 | 7.2.4固定机制；10.1.0发行/安装要求 | 官方要求`xnack+`/HMM/匹配Runtime；10.1预构建包仅`gfx942/gfx950`及二者组合 | 7.2.4的Private/Stack、有限Redzone等缺口不自动等于10.1全部已知问题 | `AMD-ASAN-724` / `AMD-ASAN-1010`；[AMD专题](../amd-gpu-asan-hardware-fallback.md) |
| Intel oneAPI Device Sanitizers / B2类产品参考 | DPC++/C++ 2026.2；2026.0历史证据保留 | Linux + 官方列举的Ponte Vecchio单卡 | ASan强制串行；大Workgroup数可能跳过Local/Private检查 | `INTEL-DEVICE-2026-2` / `INTEL-LIMITS-2026-2`；[Intel专题](../intel-oneapi-device-sanitizers.md) |
| 摩尔线程 MUSA Compute Sanitizer / 公开行为参考 | v1.1.1 说明与现有指南（日期不完整） | 按其具体 Runtime/Driver/设备支持表 | 内部 Shadow/二进制插桩机制未披露；特殊访存覆盖不应猜测 | `MUSA-USER` / `MUSA-111`；[官方来源登记](vendors_sources.json) |

上表是简短导航，**不是第二份厂商详细支持声明**。对任一限制的精确表述，以 Profile 所列版本、对应官方来源 ID 和专题为准。资料引用不能把厂商已发布的功能、项目自行设计的方案与实际 GPU 检出记录混淆。

## 2. 能力比较（按文档实际范围）

统一核查维度已写入 `vendor_profiles`：`version_baseline`、`hardware_requirements`、`instrumentation`、`runtime_requirements`、`detector_scope`、`known_limitations`、`source_ids` 和 `verification_status`。产品能力差异由[核心方案分类](../core-design-comparison.md)和各专题解释；“未建立”表示目前没有可采信的公开证据，而不是反向证明产品完全不支持某功能。

## 3. NVIDIA：可直接用于架构讨论的事实

### 3.1 编译期功能与对象边界

**历史关键修正：** NV-C是NVCC/PTXAS集成的Base-and-Bounds，不能写成公开可移植的LLVM Pass；自研LLVM D1是实施选择。NV-C来源推导失败退回常规范围检查，NV-R仍主要检查当前地址。论文cuCatch的性能数字不属于商业产品保证。完整Hybrid/JIT/HMM及Shared限制见[S01指南](../S01-vendor-implementation-guide.md)和`NV-*`来源。

### 3.2 内存与性能边界

Runtime 跟踪与设备插桩可能增加 Host/GPU 元数据及寄存器资源，工具导致的 Launch 丢失或串行化会改变覆盖结论；不能以一次 CLEAN 证明完整检测。版本化限制归 `NV-MANUAL` / `NV-RELEASE`。

### 3.3 公共 API 可借鉴的接口分层（非自研现状声明）

官方 Callback / Patching / Memory API 是可核实的公开接口分类，并非内部完整 Runtime Rewriter 开源。自研 UMD/KMD 的唯一实施说明参见 [组件指南](../real-gpu-components-implementation.md)，不要从 Patch API 推断 NVIDIA 内部的私有索引结构。

### 3.4 规范链接

[NVIDIA 手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)、[Sanitizer API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html)、[版本化证据 ID](vendors_sources.json)。

## 4. AMD 与 LLNL：要把编译、运行库、系统能力一起说清

**版本修正：** [10.1.0发布历史](https://rocm.docs.amd.com/en/docs-10.1.0/release/versions.html)已列2026-10-05发布；7.2.4页面中的旧Preview横幅不能继续用来判断“当前生产版”。[10.1 ASAN安装页](https://rocm.docs.amd.com/en/docs-10.1.0/install/asan.html)是新增发行包证据，7.2.4保留作机制基线。LLNL 7.0.0现场问题和7.13预览缺陷分别存档，不跨版本外推。XNACK/HMM与无XNACK三种Shadow映射取舍仍仅在[AMD专题](../amd-gpu-asan-hardware-fallback.md)维护。

## 5. MUSA：公开样本的价值与边界

官方指南只能证明公开的 Global OOB、API Error、独立 Leak 检查等功能，**不能推出**采用与 NVIDIA/AMD 相同的私有插桩、Shadow 或来源分析。详情以 `MUSA-USER` / `MUSA-111` 为准。

## 6. Intel：覆盖丰富，但配置与检查退化必须一起列

**2026.2复核：** [官方发布说明](https://www.intel.com/content/www/us/en/developer/articles/release-notes/oneapi-dpcpp/2026.html)确认2026.2.0于2026-10-22发布，设备指南仍保留平台、串行化和跳过检查的限制。完整命令仅在[Intel专题](../intel-oneapi-device-sanitizers.md)维护。UR规范的*planned*及Runtime自动启层说明，与Intel产品示例的显式OpenMP层配置分别引用，不合成通用后端承诺。

## 7. 对最终文稿的证据使用建议

调查结论只分为 **官方公开并版本化的事实 / 本项目工程推断与设计 / 已实现源码 / 真实硬件验收** 四类。产品“支持”需要同时注明版本、设备条件、已检查空间、被跳过范围和实际执行证据；单靠静态 CI 或厂商文档不能构成目标 GPU 的 `GPU_DETECTED`。

## 8. 关键来源导航

一手来源、独立 Runtime 规范、现场指南、研究论文等全部记录在 [vendors_sources.json](vendors_sources.json)。新增厂商资料应先更新 `sources[].id` 及 `vendor_profiles`，再修改专题或主稿摘要，避免产生另一份无法追踪的支持矩阵。
