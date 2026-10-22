# GPU Sanitizer 研究与实施进度（2026-10-22 · v2.0）

## 当前分类与事实

**现行唯一分类**：[2家主要成熟厂商与3种核心基础空间参考配置](core-design-comparison.md)：

① **NV-R**：NVIDIA默认Runtime Binary Instrumentation＋当前地址有效性；② **AMD-B2**：LLVM设备Shadow/Redzone；③ **NV-C**：NVIDIA编译期Original-Object Bounds。NV-R与AMD-B2都主要检查当前地址，而NV-C/D1保留原对象来源。Intel/MUSA保留功能/Runtime和报告参照，不据此增加第四种已证实的基础算法。

B/C/D/E/F仍是**五个工程预算与产品交付责任标签，而不是五种独立算法**：B1（原I）是B的当前Live区间表变体，B2是首选；C是Runtime重写，D1/D2是原对象/时间身份，E是集成，F1/F2依赖真正硬件。F0/G只是辅助GPU页保护。

**已完成的是设计文档、28×2厂家样例源码、静态审计和工具入口；目标自研GPU代码尚未实现、真机GPU_NOT_RUN。**

| 事项 | 状态 | 证据或下一步骤 |
|---|---|---|
| 两家主要厂商/三配置来源、能力对比、是否完全相同 | **文档完成** | [核心分类和能力对照](core-design-comparison.md) |
| S01 NV-R/AMD-B2/NV-C逐组件差异 | 文档完成 | [S01指南](S01-vendor-implementation-guide.md)；[无XNACK AMD式自研实现](amd-gpu-asan-hardware-fallback.md) |
| B/C/D/E/F工程包和F0辅助准入 | 文档完成 | [主研究](llvm-gpu-sanitizer-research.md)、[组件指南](real-gpu-components-implementation.md)、[硬件审计](hardware-deployability-audit.md) |
| 工程量估算 | 算术与结构审计完成；**非实测** | [原v1.8数字、v2.0分类元信息](estimates-v1.8.json)；按B/C/D/E/F工程包预算且不重复计算公共P |
| 28组CUDA/HIP错误与合法例 | 源码与README已提交 | [样例](../samples/README.md)；各GPU平台按声明工具分别运行，不能一概预期28×2均检出 |
| LLVM目标GPU设备Sanitizer Pass/Device Runtime | **未实施** | 先实现AMD-B2 Global Load/Store设备真实Shadow检查 |
| UMD Allocator/Shadow/Report与可靠发布/失败 | **未实施** | GPU可读metadata BO、异步Poison、Coverage/TOOL_FAILURE契约 |
| KMD GPUVA/BO/fence/abort可靠性 | **未验收** | 核验现有真实接口，只有缺口时新增驱动实现 |
| D1原对象Bounds与D2指针Epoch传播 | **未实施** | 分别以S04、S09/S25验收，不能将B1误写成D1 |
| C Binary Rewriter与E模块混合覆盖 | **未实施** | 只在不可重编译/JIT实际需求明确时开发；NVIDIA Hybrid不能默认函数级补洞 |
| F1/F2硬件协同 | **条件性、未实施** | 没有ISA/RTL/芯片时UNSUPPORTED；F0仅辅助页fault实验 |
| 目标自研GPU S01正反例、吞吐及显存 | **GPU_NOT_RUN** | 最终GPU ISA、错误PC/lane、真实设备报告与同条件基线 |
| 厂商NVIDIA/AMD GPU CI | 工作流存在，未取得实卡结果 | 需有实体GPU Runner；CPU静态CI不属于设备检出 |

## 建议实施顺序

公共对象/队列/报告 P → **AMD-B2 Shadow的S01真实GPU闭环** → **NV-C式D1原对象Bounds** → 对确有时间安全目标增加**D2** → 有闭源Binary覆盖需要才实现**NV-R式C与E集成** → 有真机性能证据才研究F1/F2。B1只作无Shadow时的当前地址实现变体。

## 校验范围

`python3 scripts/validate_docs.py` 校验**2厂商、3配置、5工程包**的分类、来源表、预算恒等式、23章/28场景、GPU_NOT_RUN、文档链接与历史路线名称；`python3 scripts/validate_layout.py` 校验CUDA/HIP样例结构。CI不编译目标自研GPU Kernel，不测真实性能，不提供产品安全验收。

