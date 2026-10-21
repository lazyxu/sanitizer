# 修订记录

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
