# 文档导航：从会议选型到实现验收

**v2.9 · 2026-11-02。** 自研目标GPU仍为GPU_NOT_RUN。每个主题只维护一份详细依据；导航不另创方案计数或实现承诺。

## 会议阅读顺序

| 顺序 | 内容 | 评审应形成的结果 |
|---|---|---|
| 1 | [核心比较](core-design-comparison.md) | 理解六条软件路线、三种成熟参考配置、两层空间保证 |
| 2 | [主报告§1及§10～12](llvm-gpu-sanitizer-research.md#software-route-details) | 选择首轮路线、元数据方式和可接受盲区 |
| 3 | [主报告§16](llvm-gpu-sanitizer-research.md#component-estimates)与[预算JSON](estimates-v1.8.json) | 划分公共/独有/条件任务，识别尚无独立报价项 |
| 4 | [主报告§19](llvm-gpu-sanitizer-research.md#validation)与[§22待决项](llvm-gpu-sanitizer-research.md#platform-parameters) | 确认正反例、退出证据、平台事实和责任人 |
| 5 | 有技术争议时查下面对应专题 | 以原始证据和单一ABI解决分歧 |

## 按问题选择唯一详细来源

| 问题 | 文档 | 维护职责 |
|---|---|---|
| 方案计数、六路线能力/性能/工作量横比 | [core-design-comparison.md](core-design-comparison.md) | 分类与选择摘要；完整算法论证回主报告 |
| 算法、共同契约、路线实现、预算和验收 | [主研究报告](llvm-gpu-sanitizer-research.md) | 综合会议正文；保留23章稳定编号，按阅读阶段组织目录 |
| 可执行设备检查如何接线 | [组件指南](real-gpu-components-implementation.md) | 唯一ABI/伪代码、Mask、Pointer身份、Free发布、Guard和终态 |
| NVIDIA NV-R/NV-C、AMD产品如何运行 | [S01厂商指南](S01-vendor-implementation-guide.md) | 版本条件和命令，区分厂商机制与自研适配 |
| 无XNACK时Shadow怎样安全部署 | [AMD硬件适配](amd-gpu-asan-hardware-fallback.md) | XNACK/HMM、三种映射取舍和元数据驻留，不重复通用设备代码 |
| Intel ASan/MSan/TSan如何定位 | [Intel专题](intel-oneapi-device-sanitizers.md) | 2026.2能力、SYCL/OpenMP、UR协作及串行/跳过限制 |
| 论文数字与原文差异 | [论文取证](research-notes/papers.md) | cuCatch/CuSafe机制与统计范围；其他研究仅补充背景 |
| 厂商事实对应哪个版本/来源 | [厂商索引](research-notes/vendors.md)、[vendors_sources.json](research-notes/vendors_sources.json) | 版本、核验日期、硬件/Runtime条件、来源ID |
| 28例如何触发、运行和判断 | [样例索引](../samples/README.md)及各Sxx README | 唯一详细案例；manifest负责可执行的厂商预期 |
| 上游代码与许可 | [NVIDIA独立回归组](../tests/upstream/nvidia/README.md) | 固定来源和NV-*编号，不与28例混计 |
| Runner、执行状态与设备准入 | [GPU CI](gpu-actions-ci.md)、[部署审计](hardware-deployability-audit.md) | 配置、真实证据和失败分类 |
| 历史推理、进度、修改 | [架构历史](research-notes/architecture.md)、[research-progress.md](research-progress.md)、[修订记录](revision-history.md) | 历史非第二份当前ABI；进度区分文档与产品实现 |
| 本地链接及结构校验 | [validate_markdown_links.py](../scripts/validate_markdown_links.py)、[validate_docs.py](../scripts/validate_docs.py)、[快照](validation-report.json) | CPU检查与主稿绑定，不证明GPU功能/性能 |

## 一致性规则

分类冲突查核心比较；厂商条件查版本化原始来源；实现冲突查组件指南；案例详情查对应Sxx README和manifest；预算查未改数值的v1.8 JSON；运行结果查对应Commit/设备日志。来源未知、未支持和工具失败必须显式保留。

v2.9已将重复预算总表、验证层级、厂商命令、历史修订/检索过程合并或删除；新增技术细节直接并入原有路线章节。文档的完备性不升级自研设备状态。
