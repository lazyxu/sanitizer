# LLVM GPU Sanitizer 技术调研

本仓库保存面向自研GPU和LLVM后端的sanitizer技术调研与架构实施建议。生产软件按**编译器、KMD（Kernel-Mode Driver，内核态驱动）、UMD（User-Mode Driver，用户态驱动）**分工；功能级cmodel与基于QEMU的全系统虚拟平台分别分析。

**当前版本1.2：调研文稿已完成，实际sanitizer实现与目标硬件验证尚未开始。资料核验日期：2026-10-20。**

## 阅读入口

- [正式调研与实施方案](docs/llvm-gpu-sanitizer-research.md)：23章、39项参考来源，包含详细背景与目标、六条技术路线、组件契约、工程预算和28个检测场景。
- [项目背景](docs/llvm-gpu-sanitizer-research.md#background)与[建设目标](docs/llvm-gpu-sanitizer-research.md#goals)：建设动因、现有调试盲区、使用者、功能／质量目标、验收证据和优先级。
- [三组件支持契约](docs/llvm-gpu-sanitizer-research.md#component-contracts)：编译器、KMD、UMD分别生产哪些信息、依赖什么接口，以及现有支持与新增修改的区别。
- [功能级cmodel／QEMU能力边界](docs/llvm-gpu-sanitizer-research.md#model-platforms)与[全系统接入](docs/llvm-gpu-sanitizer-research.md#qemu-integration)：区分GPU执行、模型检查、Guest驱动路径和真机直通。
- 逐路线双平台分析：[A cmodel](docs/llvm-gpu-sanitizer-research.md#route-a-platforms)、[B shadow](docs/llvm-gpu-sanitizer-research.md#route-b-platforms)、[C binary](docs/llvm-gpu-sanitizer-research.md#route-c-platforms)、[D bounds／identity](docs/llvm-gpu-sanitizer-research.md#route-d-platforms)、[E hybrid](docs/llvm-gpu-sanitizer-research.md#route-e-platforms)、[F hardware](docs/llvm-gpu-sanitizer-research.md#route-f-platforms)。各路线正文也有三组件支持与修改表。
- [工程量估算](docs/llvm-gpu-sanitizer-research.md#component-estimates)与[结构化算术数据](docs/estimates-v1.2.json)：核心／手写测试分开；三组件、cmodel、条件KMD缺口及QEMU接入分别计算。
- [检测场景与实例分析](docs/llvm-gpu-sanitizer-research.md#detection-scenarios)：S01～S28逐例说明错误位置、检测方案、组件配合、修复／合法对照、报告与限制；[逐场景KMD和平台对应表](docs/llvm-gpu-sanitizer-research.md#scenario-platforms)补充每例的执行与观测条件。
- [验证基准与验收矩阵](docs/llvm-gpu-sanitizer-research.md#validation)、[研究进度](docs/research-progress.md)、[修订记录](docs/revision-history.md)、[文稿验证记录](docs/validation-report.json)。
- [归档研究笔记](docs/research-notes/)：架构、论文及友商资料的过程记录与来源链接。

## 1.2版主要更新

每条A～F路线分别给出编译器、KMD、UMD必需支持和新改代码的判断条件；D进一步分为D1对象边界与D2时间身份。设备检查库在GPU执行，归入编译器交付包；SDK/Host runtime、用户态driver和CLI按层实现，预算归入UMD责任包。该归属不要求改变现有代码仓库或运行边界。

功能级cmodel能否运行插桩代码，与是否实现独立检查oracle分开。QEMU按Q0无目标GPU、Q1协议设备桩、Q2接入目标GPU模型、Q3真机直通四种配置说明。只有Q2实际模型执行或Q3上的真实检查器，才能给出相应GPU kernel检测证据；Guest CPU的TCG插件不自动观察GPU内部访存。

代码量保留1.1版六路线基础参考包合计，重新组织三组件与模型分项；KMD新增映射、故障／完成、终止、代码及硬件状态契约按缺口另计。Q2一次性系统基础和逐路线接线分别估算，复用的cmodel检查引擎只计一次；Guest KMD适配也有明确归属。

新增Linux kernel与QEMU官方来源9项。原28个详细实例保留，并补上全部场景的KMD／双平台对应和分层验收，区分真实检测、故障注入、模型证据与真机证据。

## 文稿状态与后续实施

**正式判断以主稿为准。** 归档研究笔记保留1.0版调研过程，可能包含早期候选结论、旧章节编号或尚未统一的措辞。文稿检查验证结构、引用、场景映射和预算算术；没有编译或运行示例，没有执行用户GPU、cmodel、QEMU、KMD或UMD实现测试。

从[第22章平台参数](docs/llvm-gpu-sanitizer-research.md#platform-parameters)确认ISA、GPUVA、LLVM分支、三组件现有契约、功能模型能力及QEMU配置，再选择可验收原型。依据真实执行证据收敛覆盖、组件修改和预算，文献性能数字不代表自研实现性能。

仓库保存原创概括与公开来源链接，不包含第三方论文PDF或完整原文提取。后续继续工作时先读取master最新版本，通过正常提交保留历史。
