# GPU Sanitizer 研究与实施进度（2026-10-23 · 文档 v2.8）

> **状态边界：** 仓库已有研究、厂商一手证据、CUDA/HIP 对照样例与 CPU 静态 CI，但**自研目标 GPU 的 Checker、设备 Runtime 与相应 Compiler/UMD/KMD 产品实现尚未交付**。不能由文档、厂商工具或静态测试推断自研目标已检出错误。真实生产硬件验收按用户决定跳过（未验收），状态保留 `GPU_NOT_RUN`。

## 当前方案分类

现行唯一依据是 [三种空间检查参考配置](core-design-comparison.md)：**NV-R**（NVIDIA 运行期 Binary 插桩、当前地址有效性）、**AMD-B2**（LLVM GPU Shadow/Poison）、**NV-C**（编译期 Original-Object Bounds，来源可证明时）。其中 NV-R 与 AMD-B2 主要提供当前地址有效性；NV-C 增强原对象保证。B/C/D/E/F 是工程责任包，B1、D2、E 和 F1/F2 不构成第四种已证明的独立基础空间算法。

## 文档整理进度

| 文档阶段 | 已完成的工作 | 权威入口 |
|---|---|---|
| v2.2 · Intel 与导航 | Intel 2026.0 ASan/MSan/TSan 专题和统一阅读入口 | [Intel 专题](intel-oneapi-device-sanitizers.md)、[导航](README.md) |
| v2.3 · 28 个案例 | 主稿 §18 仅保留摘要及双向链接，详细规范归 Sxx README | [样例索引](../samples/README.md)、[主研究](llvm-gpu-sanitizer-research.md) |
| v2.4 · S01 厂商指南 | 厂商机制和通用组件代码分别单源维护 | [S01 指南](S01-vendor-implementation-guide.md)、[组件指南](real-gpu-components-implementation.md) |
| v2.5 · AMD/XNACK | 官方硬件条件与三种 Shadow 映射取舍归 AMD 专题；Safe Lookup 归设备组件 | [AMD 适配](amd-gpu-asan-hardware-fallback.md) |
| v2.6 · 厂商证据 | 统一版本、硬件、检测范围、限制和来源 ID；主稿移除重复命令 | [结构化证据](research-notes/vendors_sources.json) |
| v2.7 · 架构与预算 | 主稿保留架构决策，组件代码/ABI 单源；历史 R1～R9 与 kLOC 表分离 | [组件指南](real-gpu-components-implementation.md)、[预算](estimates-v1.8.json) |
| v2.8 · 链接与状态 | 恢复第 16、20 章稳定锚点，加入全部 Markdown 本地路径与章节片段校验，更新本进度 | [链接检查器](../scripts/validate_markdown_links.py)、[审计快照](validation-report.json) |

## 自研目标 GPU 的实际开发状态

| 工作项 | 当前状态 | 备注 |
|---|---|---|
| LLVM Device Checker、B2 Shadow/Poison、报告/失败闭环 | **未实现** | 第一批产品目标：Global Load/Store 及 S01 |
| D1 原对象来源与 D2 Allocation ID/Epoch | **未实现** | 不应由当前地址命中冒充对象/时间安全 |
| C Runtime Binary Rewriter / E Hybrid | **未实现** | 只有无法重编译模块存在真实需求时才立项 |
| F1/F2 Tag/Bounds 硬件 | **条件性、未验收** | 无目标 ISA/RTL 能力不得标已支持 |
| 28 组 CUDA/HIP 对照样例 | 源文件齐全 | 用于厂商工具对照；不证明自研 Checker |
| 厂商工具与自研 GPU 性能/检测率 | **未实测自研目标** | `GPU_NOT_RUN`；生产验收按用户决定跳过（未验收） |
| CPU 文档与样例结构检查 | 已配置 | 真实是否通过应以对应 Commit 的 Actions 记录为准 |

## 工程预算与实施优先级

**数字唯一来源仍为 [v1.8 软件预算 JSON](estimates-v1.8.json)**。v2.8 是文档结构版本，**不是新的 kLOC 估算**：公共 P 只能计算一次，C 纯 Runtime 可以不修改生产 Compiler；KMD 缺口和 F1/F2 新硬件不隐含在同一默认成本里。

推荐工程顺序：公共对象、Queue/Fence 与报告底座 → AMD-B2 的真实 GPU S01 Checker → NV-C/D1 原对象 Bounds → 按需 D2 时间身份 → 有明确二进制覆盖需求时的 C/E → 基于真实瓶颈评估 F。

## CPU 校验与证据边界

从仓库根目录运行 `python3 scripts/validate_docs.py`、`python3 scripts/validate_layout.py`、`python3 scripts/validate_markdown_links.py`，以及 `python3 -m unittest discover -s tests -v`。Markdown 校验检查本地文件与 GitHub 式章节锚点，**不验证外部 URL 的实时可用性**；上述检查不执行 GPU Kernel，不生成设备检测率、性能或生产验收证据。
