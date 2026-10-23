# 文档导航：从决策到源码实现

> 推荐作为 GPU Sanitizer 技术文档的**统一阅读入口**。仓库目前处于研究、厂商工具样例和静态 CI 阶段；自研实体 GPU Checker 尚未实现，当前 `GPU_NOT_RUN`。三种核心空间参考配置、两层空间判定语义的唯一规范请以 [核心分类表](core-design-comparison.md) 为准。

## 读什么：按你的问题选择，而不是从头读完全部文档

| 想解决的问题 | 首选文档 | 文档责任 |
|---|---|---|
| 现在究竟有几种大方案？如何选择？ | [核心分类表](core-design-comparison.md) | **唯一分类依据**；NV-R / AMD-B2 / NV-C 以及 B/C/D/E/F 的正确关系 |
| S01 的 NVIDIA/AMD 成熟检测机制如何比较？ | [S01 厂商实施指南](S01-vendor-implementation-guide.md) | 仅维护 NV-R/NV-C/AMD-B2 机制、版本前提及选型，不重复通用组件 ABI |
| 没有 XNACK/HMM 的 Shadow 怎么部署？ | [AMD-B2 硬件适配指南](amd-gpu-asan-hardware-fallback.md) | **唯一硬件说明**：XNACK/HMM、三种物理 Shadow 取舍、Sparse GPUVA、官方与自研限制 |
| Intel 的 GPU Sanitizers 有哪些成熟实现？ | [Intel oneAPI 设备端 Sanitizers](intel-oneapi-device-sanitizers.md) | **Intel 版本化能力与限制的专题来源**，不创建第四种空间算法 |
| 如何改 Compiler/UMD/KMD/Device Runtime？ | [真实 GPU 组件实施指南](real-gpu-components-implementation.md) | **唯一通用实施代码**：ABI、Safe Shadow Lookup、LLVM/UMD/KMD、报告与失败终态 |
| 需要综合查阅论文、算法与 S01～S28 原理？ | [23章主研究报告](llvm-gpu-sanitizer-research.md) | **研究汇编/深入材料**；§18仅保留28个案例摘要，详细规范由各 Sxx README 维护 |
| 如何按版本对照 NVIDIA、AMD、Intel、MUSA？ | [厂商版本化索引](research-notes/vendors.md)、[结构化证据 Profile](research-notes/vendors_sources.json) | **唯一厂商事实登记**：硬件条件、功能范围、重要限制、证据 ID 和核验状态 |
| 28 个案例如何编译、执行和统计？ | [S01～S28 样例说明](../samples/README.md)、[S01 唯一案例规范](../samples/S01/README.md) | 各 Sxx README 维护唯一详细场景，manifest 维护厂商结果预期 |
| NVIDIA 官方代码从哪里引入？ | [官方独立回归组](../tests/upstream/nvidia/README.md) | 上游版本、许可、NV-* 案例；不混入 S01～S28 |
| CI 依赖什么硬件？验证状态如何？ | [GPU CI 说明](gpu-actions-ci.md)、[实体部署审计](hardware-deployability-audit.md) | 设备/Runner 前提，区分 GPU 实测和 CPU 静态校验 |
| 如何核验 Markdown 链接与章节锚点？ | [链接校验脚本](../scripts/validate_markdown_links.py)、[文档审计](../scripts/validate_docs.py) | CPU 静态检查文件、同文件与跨文件的 GitHub 标题锚点、旧版 HTML 锚点；不检查外部网页可用性 |
| 原始架构判断、LLVM 规范证据在哪里？ | [架构研究历史笔记](research-notes/architecture.md) | 保留 12 节历史决策、R1～R9 来源与勘误，不重复维护组件代码或预算 | 
| 阶段任务、预算、修订记录 | [进度](research-progress.md)、[预算](estimates-v1.8.json)、[修订记录](revision-history.md)、[静态审计快照](validation-report.json) | 各有用途；旧预算数值是历史估计，不代表已完成开发 |

## 规范优先级与重复内容处理原则

1. **场景详情冲突：** 每个 Sxx 的详细场景、代码对照与验收只以 `samples/Sxx/README.md` 为准；`samples/manifest.json` 为厂商工具和结果预期的可执行依据，主稿 §18 仅保留索引。
2. **分类冲突：** 以 `core-design-comparison.md` 为准；绝不将 Intel/MUSA 功能表解释成第四种基础空间算法。
3. **厂商版本冲突：** 先查 `research-notes/vendors_sources.json` 的规范化 `vendor_profiles` 及 `sources[].id`，再查看各厂商专题中的详细说明和命令；不同版本、Site 现场指南和 UR 规范不能混写。
4. **实现责任冲突：** 以[唯一组件实施指南](real-gpu-components-implementation.md)的结构体/伪代码和实际已实现源码为准；主研究第8～17章讲模型、研究判断和预算，历史[架构笔记](research-notes/architecture.md)仅保留证据，不再独立定义另一套 ABI。
5. **测试状态冲突：** 以特定 Commit、真实 GPU 原始执行日志、CI 证据为准；普通 CPU 静态 CI 不等于 GPU 检测通过。
6. **历史材料：** `research-notes/` 用于记录取证过程，避免与新专题页并行维护第二份完整实现说明。

## 文档整理建议（按优先级；不在本次直接删改历史资料）

| 优先级 | 具体行动 | 预期结果 |
|---|---|---|
| P0 | 保留本页为统一入口；各主题仅保留一个权威详细页，其他位置放 1～3 句摘要和链接 | 新读者不再在多个大文件间猜测应读哪份 |
| P0 | 严格分开 **厂商能力声明 / 自研设计 / 已实现代码 / 真机验收** 四种证据状态 | 不把文档或 NVIDIA 测试成功写成自研 GPU 已实现 |
| P0（已完成） | [主研究报告](llvm-gpu-sanitizer-research.md) §18 保留28例稳定摘要及链接，详细案例转各 Sxx README | 23章结构、28例锚点保留，避免同义案例正文重复维护 |
| P0（已完成） | [S01 厂商专题](S01-vendor-implementation-guide.md) 只保留机制对比与选型；通用 Compiler/KMD/UMD 实现及 ABI 归 [组件指南](real-gpu-components-implementation.md) | 厂商机制、S01 案例和通用组件分别单源维护 |
| P0（已完成） | [AMD 硬件专题](amd-gpu-asan-hardware-fallback.md) 只维护 XNACK/HMM、稀疏映射和官方运行配置；设备检查代码归 [组件指南](real-gpu-components-implementation.md) | 不再复制两套 Compiler/Device/UMD/KMD/Reporter 伪代码 |
| P0（已完成） | 对 NVIDIA、AMD、Intel 和 MUSA 的厂商事实采用统一 `vendor_profiles` 字段；主研究只保留技术结论，历史笔记缩为对照与修正 | 跨厂商版本、能力和设备条件均可追溯到来源 ID，且无需维护三张支持表 |
| P0（已完成） | [历史架构笔记](research-notes/architecture.md) 去除同义伪代码和 kLOC 表，主报告第8～17章仅留研究模型与决策，设备 ABI/实现归[组件指南](real-gpu-components-implementation.md) | 研究证据 R1～R9 与长期实现契约分开维护 |
| P0（已完成） | [主研究报告](llvm-gpu-sanitizer-research.md) 补齐第16、20章旧目录锚点；[自动化链接审计](../scripts/validate_markdown_links.py)覆盖本地文件、章节片段与历史显式ID | 文件存在但章节无法跳转的问题纳入 CI 防回归 |
| P2 | 评估将历史估算、修订记录、审计输出移入归档区；移动前先更新所有链接和校验脚本 | 降低入口噪声，保留 Git 历史与审计可追溯性 |

**本页是导航与整理规则，不是新增 GPU 功能或已完成全部文档重构的声明。**
