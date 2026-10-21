# 文稿修订记录

## v1.8 · 2026-10-21
1. 产品技术路线仅保留B、C、D、E、F，全部要求真实GPU执行指令或实体GPU硬件检查。
2. 删除不需要的专用验证平台规划、章节、预算、里程碑及样例职责描述。
3. 新增[逐组件真实GPU实施规范](real-gpu-component-implementation.md)，包含Compiler、Device Runtime、UMD、Runtime Binary Rewriter、KMD、Firmware、硬件、Reporter和真机QA伪代码。
4. 预算只对生产实现及按缺口追加的驱动/固件/RTL计费；不把已经废弃的辅助费用混入总量。
5. 防回归检查只接受真实GPU部署与明确的NOT_RUN/INCOMPLETE状态。

## v1.7与之前 · 2026-10-21
早期研究曾包含不符合当前产品范围的候选和辅助平台规划。本版本已经在现行工作树中清理，旧提交仍可由Git版本历史自行追溯，但不是当前研发规范。

与前期一致的有效结论：纯Runtime二进制重写由UMD负责，生产Compiler源代码可零新增；已有KMD的映射、提交、故障和终止契约充分时无需额外KMD代码。S01～S28场景、厂商公开技术调研和设备真实验收原则继续有效。
