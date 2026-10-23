# 文档导航：从决策到源码实现

> 推荐作为 GPU Sanitizer 技术文档的**统一阅读入口**。仓库目前处于研究、厂商工具样例和静态 CI 阶段；自研实体 GPU Checker 尚未实现，当前 `GPU_NOT_RUN`。三种核心空间参考配置、两层空间判定语义的唯一规范请以 [核心分类表](core-design-comparison.md) 为准。

## 读什么：按你的问题选择，而不是从头读完全部文档

| 想解决的问题 | 首选文档 | 文档责任 |
|---|---|---|
| 现在究竟有几种大方案？如何选择？ | [核心分类表](core-design-comparison.md) | **唯一分类依据**；NV-R / AMD-B2 / NV-C 以及 B/C/D/E/F 的正确关系 |
| S01 如何分别用三种方式检测？ | [S01 厂商实施指南](S01-vendor-implementation-guide.md) | 深入例子和相关伪代码（非所有案例通用规范） |
| 没有 XNACK/HMM 的 Shadow 怎么部署？ | [AMD-B2 硬件替代指南](amd-gpu-asan-hardware-fallback.md) | Shadow 映射、生命周期与可恢复缺页边界 |
| Intel 的 GPU Sanitizers 有哪些成熟实现？ | [Intel oneAPI 设备端 Sanitizers](intel-oneapi-device-sanitizers.md) | **Intel 版本化能力与限制的专题来源**，不创建第四种空间算法 |
| 如何改 Compiler/UMD/KMD/Device Runtime？ | [真实 GPU 组件实施指南](real-gpu-components-implementation.md) | 工程接口、执行点、失败终态与职责分工 |
| 需要综合查阅论文、算法与 S01～S28 原理？ | [23章主研究报告](llvm-gpu-sanitizer-research.md) | **研究汇编/深入材料**；§18仅保留28个案例摘要，详细规范由各 Sxx README 维护 |
| 各厂商原始证据在哪里？ | [厂商核验笔记](research-notes/vendors.md)、[来源登记](research-notes/vendors_sources.json) | 原始证据登记与历史调查；新增专题以各自版本化文档为准 |
| 28 个案例如何编译、执行和统计？ | [S01～S28 样例说明](../samples/README.md) | 自研目标的待验证案例，保留合法对照与错误版本 |
| NVIDIA 官方代码从哪里引入？ | [官方独立回归组](../tests/upstream/nvidia/README.md) | 上游版本、许可、NV-* 案例；不混入 S01～S28 |
| CI 依赖什么硬件？验证状态如何？ | [GPU CI 说明](gpu-actions-ci.md)、[实体部署审计](hardware-deployability-audit.md) | 设备/Runner 前提，区分 GPU 实测和 CPU 静态校验 |
| 阶段任务、预算、修订记录 | [进度](research-progress.md)、[预算](estimates-v1.8.json)、[修订记录](revision-history.md)、[静态审计快照](validation-report.json) | 各有用途；旧预算数值是历史估计，不代表已完成开发 |

## 规范优先级与重复内容处理原则

1. **场景详情冲突：** 每个 Sxx 的详细场景、代码对照与验收只以 `samples/Sxx/README.md` 为准；`samples/manifest.json` 为厂商工具和结果预期的可执行依据，主稿 §18 仅保留索引。
2. **分类冲突：** 以 `core-design-comparison.md` 为准；绝不将 Intel/MUSA 功能表解释成第四种基础空间算法。
3. **厂商版本冲突：** 以具体版本的官方文档和专题页为准；如 Intel 2026.0 本页，同时保留原始 URL 和已知支持范围。
4. **实现责任冲突：** 以组件实施指南和已实现源码为准；研究报告中的概念伪代码不得自动当作已交付功能。
5. **测试状态冲突：** 以特定 Commit、真实 GPU 原始执行日志、CI 证据为准；普通 CPU 静态 CI 不等于 GPU 检测通过。
6. **历史材料：** `research-notes/` 用于记录取证过程，避免与新专题页并行维护第二份完整实现说明。

## 文档整理建议（按优先级；不在本次直接删改历史资料）

| 优先级 | 具体行动 | 预期结果 |
|---|---|---|
| P0 | 保留本页为统一入口；各主题仅保留一个权威详细页，其他位置放 1～3 句摘要和链接 | 新读者不再在多个大文件间猜测应读哪份 |
| P0 | 严格分开 **厂商能力声明 / 自研设计 / 已实现代码 / 真机验收** 四种证据状态 | 不把文档或 NVIDIA 测试成功写成自研 GPU 已实现 |
| P1 | 把 [主研究报告](llvm-gpu-sanitizer-research.md) 的案例细节与算法综述逐步整理为参考附录，先加交叉引用再迁移 | 降低主报告重复，不大批量改动历史章节编号 |
| P1 | 后续将过长的 [S01 专题](S01-vendor-implementation-guide.md) 改为 S01 案例为主；通用 Compiler/KMD/UMD 合约指向组件指南 | 避免 S01 指南成为第二份全项目架构报告 |
| P1 | 对 Intel、AMD、NVIDIA 采用统一证据字段：厂商/版本/硬件要求/检查范围/未覆盖/编译命令/运行要求/官方来源 | 跨厂商比较不再混淆设备限制与算法能力 |
| P2 | `research-notes/` 仅保留工作笔记和一手证据清单，正式结论统一链接到专题页 | 减少历史观点与当前决定相互覆盖 |
| P2 | 评估将历史估算、修订记录、审计输出移入归档区；移动前先更新所有链接和校验脚本 | 降低入口噪声，保留 Git 历史与审计可追溯性 |

**本页是导航与整理规则，不是新增 GPU 功能或已完成全部文档重构的声明。**
