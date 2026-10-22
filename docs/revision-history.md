# 修订记录

## v2.0 · 2026-10-22：以“2家厂商、3参考配置、2层空间谓词”统一全部技术口径

- 新增[唯一分类与能力比较](core-design-comparison.md)：成熟主要参考厂商是NVIDIA和AMD；核心基础空间检查**参考配置**只有**①NV-R**（运行时Binary+当前地址）、**②AMD-B2**（LLVM Shadow/Redzone）、**③NV-C**（编译期Original-Object Bounds）。NV-R与AMD-B2共享当前地址可访问性语义；NV-C提供来源可知时的原对象边界。这是**3种工程/厂商配置，不是3种数学上互异的检查谓词**。
- 全面调整[S01厂商指南](S01-vendor-implementation-guide.md)、[主研究](llvm-gpu-sanitizer-research.md)、[逐组件指南](real-gpu-components-implementation.md)、[硬件审计](hardware-deployability-audit.md)、[AMD专题](amd-gpu-asan-hardware-fallback.md)、[研究笔记](research-notes/)、[README](../README.md)、[GPU CI文档](gpu-actions-ci.md)、[样例说明](../samples/README.md)和[预算](estimates-v1.8.json)。S01删除重复路线分类表，能力矩阵以唯一入口为准。逐份扫描S01～S28案例README未发现需修改的独立算法数量冲突，因此保留原案例技术内容。
- B/C/D/E/F仅表示5个工程交付/预算责任包：B1旧I不算新算法，C是NV-R式插桩交付、D1是NV-C式原对象Bounds、D2是时间安全增强、E是模块集成、F需要真实F1/F2芯片能力。G/F0继续仅作辅助GPU页fault，不算完整S01。
- Intel oneAPI与MUSA的官方**功能和报告**继续保留在资料对照，但没有足够的内部设计公开证据让它们各自独立成为另一套基础空间检查算法；不得推定其与NV/AMD内部完全相同。
- 代码量数字不改动，仅在历史`estimates-v1.8.json`添加v2.0分类结构，强调不是五种独立算法；文档/CI做防回归，**无目标自研GPU真机结果，GPU_NOT_RUN**。

> 以下为历史修订说明，保留审计轨迹。**v1.8历史的“五路线”只指工程责任包，不再是现行五种独立算法分类。**

## v1.10 · 2026-10-21：无XNACK实施路径与“完整方案”准入重新划分

- [S01厂商指南](S01-vendor-implementation-guide.md)现在**直接**列出：ROCm 7.2.4官方`xnack+` GPU ASan的硬件依赖；自研GPU在没有XNACK/HMM时，通过预映射Shadow、分段Shadow、GPU Live Allocation表及受控D1原对象参数比较实现真正设备检查的方案、组件职责和失败边界。`xnack-`编译正常不能算GPU ASan插桩成功。
- 原**I**精确定义为**B1**：LLVM插桩的GPU端**当前Live Allocation区间表**，不携带原指针来源；不能与NVIDIA新编译期Original-Object Bounds或D1混同。首版默认B2 Shadow；B1为无Shadow的实现变体，插桩时机与检查语义正交。
- 原**G**（GPU MMU Guard Page / F0）只保留为辅助页级保护、KMD真实GPU故障链路实验，不列独立完整S01产品候选；原T/F1、H/F2分别要求真实芯片Tag/Bounds，缺硬件时不可列现成方案；E是B/D/C的混合集成而非新的检查算法。Redzone、padding、Quarantine、Shadow映射也不额外立独立产品。
- 同步第9/10/14/16章产品准入和F预算硬件前提、README/架构/组件/AMD文档、Guard Page样例说明和静态文档回归；预计代码量**不变**，变更的是方案/能力归类与芯片实现前提。
- 只完成文档与静态验证；没有目标自研GPU代码/实卡Sanitizer检测率或性能证明。

## v1.8 · 2026-10-21：实体GPU五路线与全组件修改清单

- 主报告只保留真实硬件可生效的B/C/D/E/F，正式产品准入、28场景对应关系、硬件验收和交付阶段全部按实体GPU重写。
- 新增[逐组件实现指南](real-gpu-components-implementation.md)，包含Compiler/LLVM、GPU Device Runtime、UMD DebugAllocator/Shadow、UMD Runtime Binary Rewriter、Host Runtime/Loader、KMD、Firmware、RTL/MMU及CLI/CI的**关键代码/伪代码**和跨路线总结表。
- [S01厂商实施指南](S01-vendor-implementation-guide.md)继续分清NVIDIA默认运行时二进制插桩、编译期Base/Bounds、AMD LLVM GPU ASan及自主硬件方案，不推断未知厂商内部细节。
- 重算[生产软件预算 v1.8](estimates-v1.8.json)：从工程量中剔除所有非生产平台开销，只分Compiler/KMD/UMD及实际必要的Firmware/RTL条件；五条基础规划区间为B 25–48、C 31–60、D 46–89、E 69–132、F 23–46 kLOC核心（手写测试另计），**均非实测**。
- 纯Runtime C的新增生产Compiler源码可以是0，独立UMD机器码重写器负责设备Patch；已有KMD契约充分时新增源码可为0，但必须验证实际功能。
- F0仅实际GPU页保护；F1/F2必须有真实芯片硬件与配套固件驱动，缺少支持则拒绝。
- 更新S01～S28 README、根README、研究进度、硬件部署审计和文档校验脚本。历史源码修订可通过Git历史查阅；当前仓库文档不再保留与现行实施范围无关的预算文件。

**本轮只修改研究文档/示例说明与校验材料，不宣称目标自研GPU Compiler、UMD、KMD或RTL已开发或成功测试。**

## v1.7及以前：厂商机制、核心研究与示例基础

- 初版明确LLVM GPU的访存插桩、allocation/Shadow元数据、生存期、原对象来源与时间身份，比较NVIDIA、AMD、Intel及相关论文。
- 建立23章研究报告及S01～S28的CUDA/HIP完整代码、合法反例和逐项支持范围。
- 建立CPU静态CI和可选真实GPU Runner的Docker与Actions工作流。
- 建立KMD GPUVA/BO、queue fence、fault/context abort的条件契约，以及设备报告与模块覆盖清单。
- 所有历史工程量仅为设计估算；正式现状以v1.8组件指南和预算为准。
